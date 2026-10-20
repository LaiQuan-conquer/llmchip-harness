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
  const smallCheck = await run(client, 'arch-taalas', 'verify_spec', { chip: out(small, 'chip'), spec, ppa: out(small, 'ppa'), weights: l2weights })
  expect((await judge('arch.spec', smallCheck)).verdict).toBe('fail') // stories260K does not fit 2 mm2
  const design = await run(client, 'arch-taalas', 'design', archInputs, { weightStorage: 'gf180mcu.stdcell-rom', targetAreaMm2: 8 })
  await bind('chip-spec', out(design, 'chip'))
  await bind('ppa-estimate', out(design, 'ppa'))
  const archCheck = await run(client, 'arch-taalas', 'verify_spec', { chip: out(design, 'chip'), spec, ppa: out(design, 'ppa'), weights: l2weights })
  expect((await judge('arch.spec', archCheck)).verdict).toBe('pass')
  const g2 = await approve('G2')
  return { ir, tokenizer, evalset, spec, l2weights, l2, design, quantize, g1, g2, profile, library }
}

/** The reference model's structure, untrained (owner decision 2026-10-18): the M3/M4 design subject. */
export const PROXY = { dim: 16, hiddenDim: 32, layers: 2, heads: 2, kvHeads: 1, seqLen: 64, seed: 1 }
/** The prerun die (owner decision 2026-10-18), the clock that closes timing at every corner, and the
 * microarchitecture chosen in S3 (owner decision 2026-10-20: operand buffers and a quad-SPI memory). */
export const PRERUN = { die: { xMm: 1.2, yMm: 2 }, clockMHz: 8, parallelMacs: 2, memoryInterface: 'sqi', operandBuffers: 'none', placementDensityPct: 70 }

/**
 * Creates project `chip` and takes the proxy model through G2: S0 estimate, S1 synthesis and
 * intake (L0), S2 ternary numeric spec (L2, accuracy), S3 chip spec on the 1.2 x 2 mm prerun die.
 * The proxy's accuracy result carries no meaning (it is untrained); G1 is approved to exercise the flow.
 */
export async function throughG2Proxy(client: KernelClient) {
  const { bind, judge, approve, dataHash } = flow(client)
  await client.call('projects.create', { id: 'chip', title: 'Proxy of the reference model on GF180MCU (prerun)' })
  const profile = await dataHash('gf180mcu')
  const library = await dataHash('library-180nm')
  const model = { params: 5248, dim: PROXY.dim, layers: PROXY.layers, heads: PROXY.heads, kvHeads: PROXY.kvHeads, vocab: 35, hiddenDim: PROXY.hiddenDim, title: 'proxy' }
  const request = await client.upload(
    JSON.stringify({ model, weightBits: 2, activationLocation: 'offchip', contextLength: 64, weightStorage: 'gf180mcu.stdcell-rom', parallelMacs: PRERUN.parallelMacs, clockMHz: PRERUN.clockMHz, targetAreaMm2: 2.4 }),
    { type: 'estimate.request', mediaType: 'application/json' },
  )
  const charter = await client.upload('# CHARTER\nGoal: run S0-S8 on GF180MCU with the proxy of the reference model.\n', { type: 'project.charter', mediaType: 'text/markdown' })
  await bind('charter', charter.hash)
  await bind('estimate-request', request.hash)
  await bind('process-profile', profile)
  await bind('impl-library', library)
  const estimate = await run(client, 'estimator', 'estimate', { request: request.hash, profile, library })
  expect((await judge('estimate.feasibility', estimate)).verdict).toBe('pass')
  await approve('G0')

  await client.call('stages.enter', { project: 'chip', stage: 'S1' })
  const proxy = await run(client, 'model-llama', 'synthesize', {}, PROXY)
  expect(proxy.metrics).toMatchObject({ parameters: 5248, proxy: true, trained: false })
  const tokenizer = out(proxy, 'tokenizer')
  const intake = await run(client, 'model-llama', 'intake', { weights: out(proxy, 'weights'), tokenizer }, { license: 'proxy (untrained)' })
  const ir = out(intake, 'ir')
  const evalset = await dataHash('evalset-proxy')
  await run(client, 'model-llama', 'reference', { ir, tokenizer, evalset })

  await client.call('stages.enter', { project: 'chip', stage: 'S2' })
  const q = await run(client, 'numeric', 'quantize', { ir, tokenizer, evalset }, { weightFormat: 'ternary', groupSize: 16, contextLength: 64 })
  const spec = out(q, 'spec')
  const l2weights = out(q, 'weights')
  for (const [slot, hash] of [['model-ir', ir], ['tokenizer', tokenizer], ['eval-set', evalset], ['numeric-spec', spec], ['l2-weights', l2weights]] as const) {
    await bind(slot, hash)
  }
  const accuracy = await run(client, 'numeric', 'evaluate', { ir, tokenizer, evalset, spec, weights: l2weights })
  expect(accuracy.metrics).toMatchObject({ prompts_evaluated: 20, tokens_per_prompt: 64 })
  expect((await judge('accuracy.budget', accuracy)).verdict).toBe('pass')
  await approve('G1')
  await client.call('stages.enter', { project: 'chip', stage: 'S3' })
  const l2 = await run(client, 'numeric', 'infer', { spec, weights: l2weights, tokenizer, evalset })

  const design = await run(client, 'arch-taalas', 'design', { spec, weights: l2weights, profile, library }, {
    weightStorage: 'gf180mcu.stdcell-rom',
    parallelMacs: PRERUN.parallelMacs,
    clockMHz: PRERUN.clockMHz,
    die: PRERUN.die,
    memoryInterface: PRERUN.memoryInterface,
    operandBuffers: PRERUN.operandBuffers,
    targetAreaMm2: 2.4,
  })
  await bind('chip-spec', out(design, 'chip'))
  await bind('ppa-estimate', out(design, 'ppa'))
  const archCheck = await run(client, 'arch-taalas', 'verify_spec', { chip: out(design, 'chip'), spec, ppa: out(design, 'ppa'), weights: l2weights })
  expect((await judge('arch.spec', archCheck)).verdict).toBe('pass')
  await approve('G2')
  return { ir, tokenizer, evalset, spec, l2weights, l2, design, profile, library }
}

/** S4 and S5 on top of throughG2Proxy: RTL, lint, the whole verification plan, G3. */
export async function throughG3Proxy(client: KernelClient) {
  const { bind, judge, approve, dataHash } = flow(client)
  const g2 = await throughG2Proxy(client)
  const { spec, l2weights, l2, design, tokenizer, evalset } = g2
  const calibration = await run(client, 'eda-gf180', 'calibrate', {}, { implementation: 'gf180mcu.stdcell-rom', sizes: [[256, 16]] })
  const views = await run(client, 'estimator', 'check_views', { calibration: out(calibration, 'report') })
  expect((await judge('impl.views.S4', views)).verdict).toBe('pass')
  await bind('storage-impl', out(calibration, 'report'))
  await client.call('stages.enter', { project: 'chip', stage: 'S4' })
  const generated = await run(client, 'rtl-taalas', 'generate', { spec, weights: l2weights, chip: out(design, 'chip') })
  const rtl = out(generated, 'rtl')
  const isa = await run(client, 'rtl-taalas', 'check_isa', { rtl, spec, weights: l2weights, tokenizer, evalset })
  expect(isa.metrics).toMatchObject({ mismatches: 0 })
  expect((await judge('rtl.lint', await run(client, 'sim-verilator', 'lint', { rtl }))).verdict).toBe('pass')
  await client.call('stages.enter', { project: 'chip', stage: 'S5' })
  const plan = await dataHash('verification-plan-default')
  const trace = out(l2, 'trace')
  await bind('rtl', rtl)
  await bind('l2-trace', trace)
  await bind('verification-plan', plan)
  const qualify = await run(client, 'sim-verilator', 'qualify', {})
  expect((await judge('sim-verilator.qualify.rtl-functional-sim', qualify)).verdict).toBe('pass')
  const verify = await run(client, 'sim-verilator', 'verify', { rtl, spec, trace, plan })
  expect((await judge('rtl.equivalence', verify)).verdict).toBe('pass')
  await approve('G3')
  return { ...g2, calibration, views, rtl, trace, plan, verify }
}
