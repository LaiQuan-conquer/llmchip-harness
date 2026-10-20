// The speed of S3 is the cycle count of the microcode, and that count is exact
// (requirements FR-ARC-04/05, FR-EST-07): for the proxy of the reference model in
// every microarchitecture option (operand buffers none / attention / all, SPI or
// quad-SPI external memory), the busy cycles of every step of an RTL simulation
// (Verilator, sim-verilator `verify`) equal what arch-taalas put in the chip spec,
// position by position, and the RTL still equals L2 bit for bit.
// Needs the pinned EDA image; skipped, visibly, without it.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
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

const OPTIONS = [
  { memoryInterface: 'spi', operandBuffers: 'none' },
  { memoryInterface: 'sqi', operandBuffers: 'none' },
  { memoryInterface: 'sqi', operandBuffers: 'attention' },
  { memoryInterface: 'spi', operandBuffers: 'all' },
] as const

describe.skipIf(!available(IMAGE))('S3 speed equals the RTL simulation', () => {
  it('predicts the busy cycles of every step exactly, in every option', { timeout: 3_600_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    const { dataHash } = flow(client)
    await client.call('projects.create', { id: 'chip', title: 'Cycle model against the RTL' })
    const proxy = await run(client, 'model-llama', 'synthesize', {}, PROXY)
    const tokenizer = out(proxy, 'tokenizer')
    const ir = out(await run(client, 'model-llama', 'intake', { weights: out(proxy, 'weights'), tokenizer }, { license: 'proxy (untrained)' }), 'ir')
    const evalset = await dataHash('evalset-proxy')
    const q = await run(client, 'numeric', 'quantize', { ir, tokenizer, evalset }, { weightFormat: 'ternary', groupSize: 16, contextLength: 64 })
    const spec = out(q, 'spec')
    const weights = out(q, 'weights')
    const trace = out(await run(client, 'numeric', 'infer', { spec, weights, tokenizer, evalset }), 'trace')
    const profile = await dataHash('gf180mcu')
    const library = await dataHash('library-180nm')
    const plan = await dataHash('verification-plan-default')
    const speeds: Record<string, number> = {}
    for (const option of OPTIONS) {
      const design = await run(client, 'arch-taalas', 'design', { spec, weights, profile, library }, {
        weightStorage: 'gf180mcu.stdcell-rom', parallelMacs: PRERUN.parallelMacs, clockMHz: PRERUN.clockMHz, die: PRERUN.die, targetAreaMm2: 2.4, ...option,
      })
      const ppa = await (await client.download(out(design, 'ppa'))).json() as { performance: { cyclesWithTracesPerPosition: number[]; tokensPerSecond: number } }
      const check = await run(client, 'arch-taalas', 'verify_spec', { chip: out(design, 'chip'), spec, ppa: out(design, 'ppa'), weights })
      expect(check.metrics, JSON.stringify(option)).toMatchObject({ cycles_match_model: true })
      const generated = await run(client, 'rtl-taalas', 'generate', { spec, weights, chip: out(design, 'chip') })
      const rtl = out(generated, 'rtl')
      expect(generated.metrics).toMatchObject(option.operandBuffers === 'none' ? { memory_interface: option.memoryInterface } : { memory_interface: option.memoryInterface, operand_buffers: option.operandBuffers })
      const verify = await run(client, 'sim-verilator', 'verify', { rtl, spec, trace, plan })
      expect(verify.metrics, JSON.stringify(option)).toMatchObject({ items_failed: 0, regression_mismatches: 0, random_mismatches: 0, busy_cycles_data_dependent: false })
      const report = await (await client.download(out(verify, 'report'))).json() as { items: { id: string; details: { busyCyclesByPosition: Record<string, number> } }[] }
      const measured = report.items.find((i) => i.id === 'regression.evalset')!.details.busyCyclesByPosition
      const positions = Object.keys(measured)
      expect(positions.length).toBeGreaterThan(30)
      for (const pos of positions) expect(measured[pos], `${JSON.stringify(option)} position ${pos}`).toBe(ppa.performance.cyclesWithTracesPerPosition[Number(pos)])
      speeds[`${option.memoryInterface}/${option.operandBuffers}`] = ppa.performance.tokensPerSecond
    }
    expect(speeds['sqi/none']!).toBeGreaterThan(2 * speeds['spi/none']!)
    expect(speeds['sqi/attention']!).toBeGreaterThan(speeds['sqi/none']!)
  })
})
