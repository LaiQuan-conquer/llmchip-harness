// The default flow with the bundled plugins on stories260K, shared by the
// milestone tests: S0 -> G0 -> S1 -> S2 -> G1 -> S3 -> G2 on project `chip`.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'

const MODEL = join(import.meta.dirname, 'fixtures/models/stories260K')

/** Runs a handler on project `chip` and waits for it; `exited_zero` unless `allowFailure`. */
export async function run(
  client: KernelClient,
  plugin: string,
  handler: string,
  inputs: Record<string, string>,
  params: Record<string, unknown> = {},
): Promise<RunRecord> {
  const started = await client.call('runs.start', { plugin, handler, params, project: 'chip', inputs: Object.entries(inputs).map(([name, hash]) => ({ name, hash })) })
  for (;;) {
    const r = await client.call('runs.wait', { run: started.id, timeoutMs: 600_000 })
    if (r.state === 'finished') {
      expect(r, `${plugin}/${handler}: ${r.error}`).toMatchObject({ execution: 'exited_zero' })
      return r
    }
  }
}

export const out = (r: RunRecord, name: string) => r.outputs.find((o) => o.name === name)!.hash

export function flow(client: KernelClient) {
  return {
    bind: (slot: string, hash: string) => client.call('projects.bind', { project: 'chip', slot, hash }),
    judge: (check: string, r: RunRecord) => client.call('verdicts.evaluate', { check, run: r.id, project: 'chip' }),
    approve: (gate: string) => client.call('approvals.decide', { project: 'chip', gate, decision: 'approved', comment: 'test' }),
    dataHash: async (id: string) => (await client.call('plugins.list')).plugins.flatMap((p) => p.data).find((d) => d.id === id)!.hash,
  }
}

/** Creates project `chip` and takes it through G2 with an int4 numeric spec. */
export async function throughG2(client: KernelClient) {
  const { bind, judge, approve, dataHash } = flow(client)
  await client.call('projects.create', { id: 'chip', title: 'stories260K on GF180MCU (comparison model)' })

  // S0 / G0: the estimate request is part of the snapshot, so the verdict judges exactly this request.
  const request = await client.upload(JSON.stringify({ model: { preset: 'stories260K' }, weightStorage: 'gf180mcu.stdcell-rom', weightBits: 4, targetAreaMm2: 8 }), {
    type: 'estimate.request',
    mediaType: 'application/json',
  })
  const charter = await client.upload('# CHARTER\nGoal: demonstrate the flow on the comparison model.\n', { type: 'project.charter', mediaType: 'text/markdown' })
  const profile = await dataHash('gf180mcu')
  const library = await dataHash('library-180nm')
  await bind('charter', charter.hash)
  await bind('estimate-request', request.hash)
  await bind('process-profile', profile)
  await bind('impl-library', library)
  const estimate = await run(client, 'estimator', 'estimate', { request: request.hash, profile, library })
  expect((await judge('estimate.feasibility', estimate)).verdict).toBe('pass')
  await approve('G0')

  // S1: model intake and the L0 reference.
  await client.call('stages.enter', { project: 'chip', stage: 'S1' })
  const weights = await client.upload(readFileSync(join(MODEL, 'stories260K.bin')), { type: 'model.weights.llama2c', label: 'public' })
  const tokenizer = (await client.upload(readFileSync(join(MODEL, 'tok512.bin')), { type: 'model.tokenizer.llama2c', label: 'public' })).hash
  const intake = await run(client, 'model-llama', 'intake', { weights: weights.hash, tokenizer }, { license: 'MIT' })
  expect(intake.metrics).toMatchObject({ parameters: 260_032, layers: 5, dim: 64, vocab: 512 })
  const ir = out(intake, 'ir')
  const evalset = await dataHash('evalset-tinystories')
  const l0 = await run(client, 'model-llama', 'reference', { ir, tokenizer, evalset })
  expect(l0.metrics.prompts).toBe(20)

  // S2 / G1: numeric spec, L2 and its accuracy against L0 (AC-04).
  await client.call('stages.enter', { project: 'chip', stage: 'S2' })
  const quantize = (weightFormat: string) => run(client, 'numeric', 'quantize', { ir, tokenizer, evalset }, { weightFormat })
  const q4 = await quantize('int4')
  const spec = out(q4, 'spec')
  const l2weights = out(q4, 'weights')
  for (const [slot, hash] of [['model-ir', ir], ['tokenizer', tokenizer], ['eval-set', evalset], ['numeric-spec', spec], ['l2-weights', l2weights]] as const) {
    await bind(slot, hash)
  }
  const accuracy = await run(client, 'numeric', 'evaluate', { ir, tokenizer, evalset, spec, weights: l2weights })
  expect(accuracy.metrics).toMatchObject({ prompts_evaluated: 20, tokens_per_prompt: 64 })
  expect(accuracy.metrics.ppl_increase_pct as number).toBeLessThan(20)
  expect((await judge('accuracy.budget', accuracy)).verdict).toBe('pass')
  await expect(client.call('stages.enter', { project: 'chip', stage: 'S3' })).rejects.toMatchObject({ code: 'STAGE_ENTRY_BLOCKED' })
  const g1 = await approve('G1')
  await client.call('stages.enter', { project: 'chip', stage: 'S3' })

  // The L2 traces exist for later bit-exact comparison with RTL.
  const l2 = await run(client, 'numeric', 'infer', { spec, weights: l2weights, tokenizer, evalset })
  expect(l2.metrics.prompts).toBe(20)

  // S3 / G2: chip spec from the frozen numeric spec.
  const archInputs = { spec, weights: l2weights, profile, library }
  const small = await run(client, 'arch-taalas', 'design', archInputs, { weightStorage: 'gf180mcu.stdcell-rom' })
  const smallCheck = await run(client, 'arch-taalas', 'verify_spec', { chip: out(small, 'chip'), spec, ppa: out(small, 'ppa') })
  expect((await judge('arch.spec', smallCheck)).verdict).toBe('fail') // stories260K does not fit 2 mm2
  const design = await run(client, 'arch-taalas', 'design', archInputs, { weightStorage: 'gf180mcu.stdcell-rom', targetAreaMm2: 8 })
  await bind('chip-spec', out(design, 'chip'))
  await bind('ppa-estimate', out(design, 'ppa'))
  const archCheck = await run(client, 'arch-taalas', 'verify_spec', { chip: out(design, 'chip'), spec, ppa: out(design, 'ppa') })
  expect((await judge('arch.spec', archCheck)).verdict).toBe('pass')
  const g2 = await approve('G2')
  return { ir, tokenizer, evalset, spec, l2weights, l2, design, quantize, g1, g2, profile, library }
}
