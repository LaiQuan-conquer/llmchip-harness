// Numeric schemes (requirements §3.4.2, FR-QNT, FR-RTL-01, FR-VER).
//
// Any recovered graph (here a tiny Qwen3 in the Hugging Face layout, and the proxy
// imported from its own llama2.c artifacts) is quantized with a chosen scheme and its
// L2 is evaluated against L0 through the kernel, judged by `numeric.accuracy`.
//
// The rounding, overflow and accumulator width chosen in S2 are part of the formal
// numeric spec and flow through S3/S4/S5. On the proxy of the reference model with a
// scheme other than the default (half-even rounding, wrap-around overflow, an
// accumulator that really overflows), the generated RTL equals L2 on the
// instruction-set simulator and in Verilator, and the arithmetic unit tests run in the
// scheme's modes. The RTL part needs the pinned EDA image and is skipped, visibly, without it.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { RunRecord } from '@llmchip/kernel-api'
import { flow, out, PROXY, PRERUN, run } from './flow-helpers.ts'
import { connect, REPO_PLUGINS, startKernel } from './helpers.ts'

const IMAGE = JSON.parse(readFileSync(join(REPO_PLUGINS, 'sim-verilator/llmchip-plugin.json'), 'utf8')).entry.image as string

function available(image: string): boolean {
  try {
    execFileSync('podman', ['image', 'exists', image], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

const TINY_QWEN3 = resolve(import.meta.dirname, 'fixtures/models/tiny-qwen3')

describe('numeric schemes on recovered graphs', () => {
  it('quantizes any recovered graph and measures its L2 against L0', { timeout: 600_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    const { judge, dataHash } = flow(client)
    await client.call('projects.create', { id: 'chip', title: 'Recovered graphs' })
    const graphInputs = async (imported: RunRecord) => {
      const recovered = await run(client, 'model-llama', 'recover', { census: out(imported, 'census') })
      return [
        { name: 'graph', hash: out(recovered, 'graph') },
        { name: 'bundle', hash: out(imported, 'bundle') },
        ...imported.outputs.filter((o) => o.name === 'file').map((o) => ({ name: 'files', hash: o.hash })),
      ]
    }
    const start = async (handler: string, inputs: { name: string; hash: string }[], params: Record<string, unknown>) => {
      const started = await client.call('runs.start', { plugin: 'numeric', handler, params, project: 'chip', inputs })
      const r = await client.call('runs.wait', { run: started.id, timeoutMs: 600_000 })
      expect(r, `numeric/${handler}: ${r.error}`).toMatchObject({ state: 'finished', execution: 'exited_zero' })
      return r
    }

    // A tiny Qwen3 (QK norm, tied output) at int8, then with a narrow accumulator that overflows.
    const qwen = await graphInputs(await run(client, 'model-llama', 'import', {}, { path: TINY_QWEN3 }))
    const q8 = await start('quantize_graph', qwen, { weightFormat: 'int8', groupSize: 16 })
    expect(q8.metrics).toMatchObject({ family: 'qwen3', weight_bits: 8, rounding: 'half-up', overflow: 'saturate', accumulator_can_overflow: false })
    const spec = (await (await client.download(out(q8, 'spec'))).json()) as any
    expect(spec).toMatchObject({ format: 'llmchip-numeric-graph-spec/1', weights: { format: 'int8', groupSize: 16 } })
    const e8 = await start('evaluate_graph', [...qwen, { name: 'spec', hash: out(q8, 'spec') }, { name: 'weights', hash: out(q8, 'weights') }], { maxPrompts: 2, steps: 4 })
    expect(e8.metrics.prompts_evaluated).toBe(2)
    expect(e8.metrics.passage_tokens as number).toBeGreaterThanOrEqual(64)
    const report = (await (await client.download(out(e8, 'report'))).json()) as any
    expect(report.scheme).toMatchObject({ weightFormat: 'int8', rounding: 'half-up' })
    expect(report.layerError).toHaveLength(2)
    expect(report.samples).toHaveLength(2)
    expect(['pass', 'fail']).toContain((await judge('numeric.accuracy', e8)).verdict)

    const narrow = await start('quantize_graph', qwen, { weightFormat: 'int4', groupSize: 16, rounding: 'truncate', overflow: 'wrap', accumulatorBits: 16 })
    expect(narrow.metrics).toMatchObject({ accumulator_bits: 16, accumulator_can_overflow: true, rounding: 'truncate', overflow: 'wrap' })
    const en = await start('evaluate_graph', [...qwen, { name: 'spec', hash: out(narrow, 'spec') }, { name: 'weights', hash: out(narrow, 'weights') }], { maxPrompts: 1, steps: 2 })
    const narrowReport = (await (await client.download(out(en, 'report'))).json()) as any
    expect(narrowReport.saturationsByKind.accumulator).toBeGreaterThan(0)

    // The proxy, imported from its own llama2.c artifacts, with the proxy's evaluation set.
    const proxy = await run(client, 'model-llama', 'synthesize', {}, PROXY)
    const own = await graphInputs(await run(client, 'model-llama', 'import', { weights: out(proxy, 'weights'), tokenizer: out(proxy, 'tokenizer') }, { name: 'proxy' }))
    const evalset = { name: 'evalset', hash: await dataHash('evalset-proxy') }
    const qp = await start('quantize_graph', [...own, evalset], { weightFormat: 'ternary', groupSize: 16, contextLength: 64 })
    const ep = await start('evaluate_graph', [...own, evalset, { name: 'spec', hash: out(qp, 'spec') }, { name: 'weights', hash: out(qp, 'weights') }], { passageTokens: 56 })
    expect(ep.metrics).toMatchObject({ prompts_evaluated: 4 })

    // A graph spec is an analysis: the vector engine refuses it with a reason.
    const profile = await dataHash('gf180mcu')
    const library = await dataHash('library-180nm')
    const params = { weightStorage: 'gf180mcu.stdcell-rom', parallelMacs: PRERUN.parallelMacs, clockMHz: PRERUN.clockMHz, die: PRERUN.die, targetAreaMm2: 2.4 }
    const started = await client.call('runs.start', { plugin: 'arch-taalas', handler: 'design', project: 'chip', params, inputs: [{ name: 'spec', hash: out(q8, 'spec') }, { name: 'weights', hash: out(q8, 'weights') }, { name: 'profile', hash: profile }, { name: 'library', hash: library }] })
    const refused = await client.call('runs.wait', { run: started.id, timeoutMs: 120_000 })
    expect(refused.execution).not.toBe('exited_zero')
    expect(refused.error).toContain('llama2.c structure only')
  })
})

const SCHEME = { weightFormat: 'ternary', groupSize: 16, contextLength: 64, rounding: 'half-even', overflow: 'wrap', accumulatorBits: 24 }

describe.skipIf(!available(IMAGE))('numeric schemes in RTL', () => {
  it('carries a non-default scheme from the numeric spec to verified RTL', { timeout: 1_800_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    const { dataHash } = flow(client)
    await client.call('projects.create', { id: 'chip', title: 'Numeric schemes on the proxy' })
    const proxy = await run(client, 'model-llama', 'synthesize', {}, PROXY)
    const tokenizer = out(proxy, 'tokenizer')
    const ir = out(await run(client, 'model-llama', 'intake', { weights: out(proxy, 'weights'), tokenizer }, { license: 'proxy (untrained)' }), 'ir')
    const evalset = await dataHash('evalset-proxy')

    const q = await run(client, 'numeric', 'quantize', { ir, tokenizer, evalset }, SCHEME)
    expect(q.metrics).toMatchObject({ rounding: 'half-even', overflow: 'wrap', accumulator_bits: 24, accumulator_can_overflow: true })
    const spec = out(q, 'spec')
    const weights = out(q, 'weights')
    const accuracy = await run(client, 'numeric', 'evaluate', { ir, tokenizer, evalset, spec, weights })
    const report = (await (await client.download(out(accuracy, 'report'))).json()) as any
    expect(report.scheme).toMatchObject({ rounding: 'half-even', overflow: 'wrap', accumulatorBits: 24, accumulatorCanOverflow: true })
    expect(report.saturationsByKind.accumulator).toBeGreaterThan(0) // the narrow accumulator really overflows
    const l2 = await run(client, 'numeric', 'infer', { spec, weights, tokenizer, evalset })

    const profile = await dataHash('gf180mcu')
    const library = await dataHash('library-180nm')
    const design = await run(client, 'arch-taalas', 'design', { spec, weights, profile, library }, {
      weightStorage: 'gf180mcu.stdcell-rom',
      parallelMacs: PRERUN.parallelMacs,
      clockMHz: PRERUN.clockMHz,
      die: PRERUN.die,
      targetAreaMm2: 2.4,
    })
    const chip = (await (await client.download(out(design, 'chip'))).json()) as any
    expect(chip.dataflow).toMatchObject({ rounding: 'half-even', overflow: 'wrap', accumulatorBits: 24, accumulatorCanOverflow: true })

    const generated = await run(client, 'rtl-taalas', 'generate', { spec, weights, chip: out(design, 'chip') })
    const rtl = out(generated, 'rtl')
    const isa = await run(client, 'rtl-taalas', 'check_isa', { rtl, spec, weights, tokenizer, evalset })
    expect(isa.metrics).toMatchObject({ prompts: 20, mismatches: 0 })
    expect((await run(client, 'sim-verilator', 'lint', { rtl })).metrics).toMatchObject({ lint_errors: 0, lint_warnings: 0 })
    const verify = await run(client, 'sim-verilator', 'verify', { rtl, spec, trace: out(l2, 'trace'), plan: await dataHash('verification-plan-default') })
    expect(verify.metrics).toMatchObject({
      items_failed: 0,
      items_not_executed: 0,
      regression_mismatches: 0,
      random_mismatches: 0,
      debug_mismatches: 0,
      'boundary.alu.failures': 0,
      'random.alu.failures': 0,
    })
  })
})
