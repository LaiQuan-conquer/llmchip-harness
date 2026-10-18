// M2 acceptance through the kernel with the bundled plugins and stories260K:
// S0 -> G0 -> S1 (intake, L0) -> S2 (numeric spec, L2, accuracy) -> G1 ->
// S3 (chip spec) -> G2, then a changed numeric spec invalidates G1 and G2 and
// their evidence (AC-04 and the M2 criteria of the development plan).

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'
import { connect, startKernel } from './helpers.ts'

const MODEL = join(import.meta.dirname, 'fixtures/models/stories260K')

async function run(client: KernelClient, plugin: string, handler: string, inputs: Record<string, string>, params: Record<string, unknown> = {}): Promise<RunRecord> {
  const started = await client.call('runs.start', { plugin, handler, params, project: 'chip', inputs: Object.entries(inputs).map(([name, hash]) => ({ name, hash })) })
  for (;;) {
    const r = await client.call('runs.wait', { run: started.id, timeoutMs: 600_000 })
    if (r.state === 'finished') {
      expect(r, `${plugin}/${handler}: ${r.error}`).toMatchObject({ execution: 'exited_zero' })
      return r
    }
  }
}

const out = (r: RunRecord, name: string) => r.outputs.find((o) => o.name === name)!.hash

describe('M2: model to chip spec', () => {
  it('passes G0, G1 and G2 on bound snapshots, and a new numeric spec invalidates them', { timeout: 600_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    const bind = (slot: string, hash: string) => client.call('projects.bind', { project: 'chip', slot, hash })
    const judge = (check: string, r: RunRecord) => client.call('verdicts.evaluate', { check, run: r.id, project: 'chip' })
    const approve = (gate: string) => client.call('approvals.decide', { project: 'chip', gate, decision: 'approved', comment: 'test' })
    const data = (await client.call('plugins.list')).plugins.flatMap((p) => p.data)
    const dataHash = (id: string) => data.find((d) => d.id === id)!.hash
    await client.call('projects.create', { id: 'chip', title: 'stories260K on GF180MCU (comparison model)' })

    // S0 / G0: the estimate request is part of the snapshot, so the verdict judges exactly this request.
    const request = await client.upload(JSON.stringify({ model: { preset: 'stories260K' }, weightStorage: 'gf180mcu.stdcell-rom', weightBits: 4, targetAreaMm2: 8 }), {
      type: 'estimate.request',
      mediaType: 'application/json',
    })
    const charter = await client.upload('# CHARTER\nGoal: demonstrate S0-S3 on the comparison model.\n', { type: 'project.charter', mediaType: 'text/markdown' })
    await bind('charter', charter.hash)
    await bind('estimate-request', request.hash)
    await bind('process-profile', dataHash('gf180mcu'))
    await bind('impl-library', dataHash('library-180nm'))
    const estimate = await run(client, 'estimator', 'estimate', { request: request.hash, profile: dataHash('gf180mcu'), library: dataHash('library-180nm') })
    expect((await judge('estimate.feasibility', estimate)).verdict).toBe('pass')
    await approve('G0')

    // S1: model intake and the L0 reference.
    await client.call('stages.enter', { project: 'chip', stage: 'S1' })
    const weights = await client.upload(readFileSync(join(MODEL, 'stories260K.bin')), { type: 'model.weights.llama2c', label: 'public' })
    const tokenizer = await client.upload(readFileSync(join(MODEL, 'tok512.bin')), { type: 'model.tokenizer.llama2c', label: 'public' })
    const intake = await run(client, 'model-llama', 'intake', { weights: weights.hash, tokenizer: tokenizer.hash }, { license: 'MIT' })
    expect(intake.metrics).toMatchObject({ parameters: 260_032, layers: 5, dim: 64, vocab: 512 })
    const ir = out(intake, 'ir')
    const evalset = dataHash('evalset-tinystories')
    const l0 = await run(client, 'model-llama', 'reference', { ir, tokenizer: tokenizer.hash, evalset })
    expect(l0.metrics.prompts).toBe(20)

    // S2 / G1: numeric spec, L2 and its accuracy against L0 (AC-04).
    await client.call('stages.enter', { project: 'chip', stage: 'S2' })
    const quantize = (weightFormat: string) => run(client, 'numeric', 'quantize', { ir, tokenizer: tokenizer.hash, evalset }, { weightFormat })
    const q4 = await quantize('int4')
    for (const [slot, hash] of [['model-ir', ir], ['tokenizer', tokenizer.hash], ['eval-set', evalset], ['numeric-spec', out(q4, 'spec')], ['l2-weights', out(q4, 'weights')]] as const) {
      await bind(slot, hash)
    }
    const accuracy = await run(client, 'numeric', 'evaluate', { ir, tokenizer: tokenizer.hash, evalset, spec: out(q4, 'spec'), weights: out(q4, 'weights') })
    expect(accuracy.metrics).toMatchObject({ prompts_evaluated: 20, tokens_per_prompt: 64 })
    expect(accuracy.metrics.ppl_increase_pct as number).toBeLessThan(20)
    expect((await judge('accuracy.budget', accuracy)).verdict).toBe('pass')
    await expect(client.call('stages.enter', { project: 'chip', stage: 'S3' })).rejects.toMatchObject({ code: 'STAGE_ENTRY_BLOCKED' })
    const g1 = await approve('G1')
    await client.call('stages.enter', { project: 'chip', stage: 'S3' })

    // The L2 traces exist for later bit-exact comparison with RTL.
    const l2 = await run(client, 'numeric', 'infer', { spec: out(q4, 'spec'), weights: out(q4, 'weights'), tokenizer: tokenizer.hash, evalset })
    expect(l2.metrics.prompts).toBe(20)

    // S3 / G2: chip spec from the frozen numeric spec.
    const archInputs = { spec: out(q4, 'spec'), weights: out(q4, 'weights'), profile: dataHash('gf180mcu'), library: dataHash('library-180nm') }
    const small = await run(client, 'arch-taalas', 'design', archInputs, { weightStorage: 'gf180mcu.stdcell-rom' })
    const smallCheck = await run(client, 'arch-taalas', 'verify_spec', { chip: out(small, 'chip'), spec: out(q4, 'spec'), ppa: out(small, 'ppa') })
    expect((await judge('arch.spec', smallCheck)).verdict).toBe('fail') // stories260K does not fit 2 mm2
    const design = await run(client, 'arch-taalas', 'design', archInputs, { weightStorage: 'gf180mcu.stdcell-rom', targetAreaMm2: 8 })
    await bind('chip-spec', out(design, 'chip'))
    await bind('ppa-estimate', out(design, 'ppa'))
    const archCheck = await run(client, 'arch-taalas', 'verify_spec', { chip: out(design, 'chip'), spec: out(q4, 'spec'), ppa: out(design, 'ppa') })
    expect((await judge('arch.spec', archCheck)).verdict).toBe('pass')
    const g2 = await approve('G2')
    for (const gate of ['G0', 'G1', 'G2']) expect((await client.call('gates.release', { project: 'chip', gate })).released).toBe(true)

    // Approvals are bound to snapshots: changing the numeric spec invalidates G1 and G2 and their evidence.
    const q8 = await quantize('int8')
    await bind('numeric-spec', out(q8, 'spec'))
    await bind('l2-weights', out(q8, 'weights'))
    for (const gate of ['G1', 'G2']) {
      expect(await client.call('gates.release', { project: 'chip', gate })).toMatchObject({ released: false, status: 'invalidated' })
      expect((await client.call('gates.snapshot', { project: 'chip', gate })).evidence).toEqual([])
    }
    expect((await client.call('gates.release', { project: 'chip', gate: 'G0' })).released).toBe(true)
    const invalidated = (await client.call('events.list', { kind: 'approval.invalidated' })).events.map((e) => e.payload.approval)
    expect(invalidated).toEqual(expect.arrayContaining([g1.id, g2.id]))
    // The old chip spec does not follow the new numeric spec.
    const stale = await run(client, 'arch-taalas', 'verify_spec', { chip: out(design, 'chip'), spec: out(q8, 'spec'), ppa: out(design, 'ppa') })
    expect(await judge('arch.spec', stale)).toMatchObject({ verdict: 'fail', reasons: [{ code: 'CRITERION_FAILED', params: { metric: 'numeric_spec_matches' } }] })
  })
})
