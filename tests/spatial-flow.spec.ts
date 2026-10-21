// The spatial engine (architecture taalas-spatial/1) through the kernel, S2 -> S5 on the
// proxy of the reference model: arch-taalas designs it (speed from the fixed schedule of its
// circuits, area from their measured cells, the die from the size rules), rtl-taalas
// generates the circuits from the weights (round trip read back from the netlist),
// sim-verilator lints them and runs the spatial verification plan: L3 equals L2 bit for bit,
// observation points read through the host READ command, every step takes the cycles S3
// stated, at every position.
// Needs the pinned EDA image; skipped, visibly, without it.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { flow, out, PROXY, run } from './flow-helpers.ts'
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

describe.skipIf(!available(IMAGE))('spatial engine (taalas-spatial/1) from S2 to S5', () => {
  it('designs, generates and verifies the model as circuits at more than 1000 tokens/s', { timeout: 3_600_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    const { dataHash } = flow(client)
    await client.call('projects.create', { id: 'chip', title: 'Spatial engine' })
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

    // S3: speed and area of the circuits. Without a die the profile's prerun area applies and the
    // spatial engine does not fit; the die it suggests from its measured cell area is then chosen.
    const params = { weightStorage: 'gf180mcu.stdcell-rom', architecture: 'taalas-spatial/1', clockMHz: 8, cyclesPerToken: 8000 }
    const first = await run(client, 'arch-taalas', 'design', { spec, weights, profile, library }, params)
    expect(first.metrics.feasibility).toBe('infeasible')
    const suggested = (await (await client.download(out(first, 'ppa'))).json() as { area: { suggestedDie: { xMm: number; yMm: number } } }).area.suggestedDie
    const design = await run(client, 'arch-taalas', 'design', { spec, weights, profile, library }, { ...params, die: { xMm: suggested.xMm, yMm: suggested.yMm } })
    expect(design.metrics).toMatchObject({ architecture: 'taalas-spatial/1', memory_interface: 'none' })
    const cycles = design.metrics.cycles_per_token as number
    expect(cycles).toBeLessThanOrEqual(8000)
    expect(design.metrics.tokens_per_second as number).toBeGreaterThan(1000)
    const ppa = await (await client.download(out(design, 'ppa'))).json() as { area: { chosenDie: { xMm: number; yMm: number }; cellsMm2: { typ: number } } }
    expect([2, 3, 5]).toContain(ppa.area.chosenDie.yMm)
    const check = await run(client, 'arch-taalas', 'verify_spec', { chip: out(design, 'chip'), spec, ppa: out(design, 'ppa'), weights })
    expect(check.metrics).toMatchObject({ numeric_spec_matches: true, observation_points_covered: true, fits_area: true, cycles_match_model: true, cycles_within_target: true })

    // S4: the circuits from the weights; S5: lint and the spatial verification plan.
    const generated = await run(client, 'rtl-taalas', 'generate', { spec, weights, chip: out(design, 'chip') })
    expect(generated.metrics).toMatchObject({ architecture: 'taalas-spatial/1', mapping_round_trip: true, cycles_per_token: cycles })
    const rtl = out(generated, 'rtl')
    const lint = await run(client, 'sim-verilator', 'lint', { rtl })
    expect(lint.metrics).toMatchObject({ lint_errors: 0, lint_warnings: 0 })
    const plan = await dataHash('verification-plan-spatial')
    const verify = await run(client, 'sim-verilator', 'verify', { rtl, spec, trace, plan })
    expect(verify.metrics).toMatchObject({
      items_failed: 0, items_not_executed: 0, regression_mismatches: 0, random_mismatches: 0,
      regression_prompts: 20, tokens_per_prompt: 64, busy_cycles_data_dependent: false, busy_cycles_per_token: cycles,
    })
    expect(verify.metrics.coverage_line_pct as number).toBeGreaterThanOrEqual(90)
    const report = await (await client.download(out(verify, 'report'))).json() as { items: { id: string; details: { busyCyclesByPosition: Record<string, number> } }[] }
    const measured = report.items.find((i) => i.id === 'regression.evalset')!.details.busyCyclesByPosition
    expect(Object.keys(measured).length).toBe(64)
    for (const pos of Object.keys(measured)) expect(measured[pos], `position ${pos}`).toBe(cycles)
  })
})
