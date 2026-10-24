// M4 acceptance: S0-S7 on GF180MCU through the kernel with the bundled plugins
// and the proxy of the reference model, each stage judged by its checks:
// physical implementation, signoff (DRC, LVS, multi-corner STA with constraint
// completeness, gate-level simulation) and the lineage of the GDS back to the
// model weights (AC-05, AC-08). Reverse tests: a removed constraint and an
// unregistered false path block signoff (AC-06), an SDF result with incomplete
// annotation never passes (AC-12), and the migration assessment to another
// 180 nm process restarts at S6 for this standard-cell-ROM design (AC-14).
// There is no tapeout package and no export (owner decision 2026-10-24).
//
// The full flow runs the EDA tools for about half an hour, so it is opt-in:
// LLMCHIP_FULL_FLOW=1 (and the pinned image installed); otherwise it is skipped, visibly.
// LLMCHIP_ARCH=spatial runs it on the spatial engine (taalas-spatial/1) instead of the
// vector engine.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import { flow, out, PRERUN, PRERUN_SPATIAL, run, SPATIAL, throughS5Proxy } from './flow-helpers.ts'
import { connect, REPO_PLUGINS, startKernel } from './helpers.ts'

const IMAGE = JSON.parse(readFileSync(join(REPO_PLUGINS, 'eda-gf180/llmchip-plugin.json'), 'utf8')).entry.image as string

function available(image: string): boolean {
  try {
    execFileSync('podman', ['image', 'exists', image], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

const json = async (client: KernelClient, hash: string) => JSON.parse(await (await client.download(hash)).text())

describe.skipIf(!process.env.LLMCHIP_FULL_FLOW || !available(IMAGE))('M4: physical implementation and signoff on GF180MCU', () => {
  it('runs S0-S7, signs off, and traces the GDS back to the weights', { timeout: SPATIAL ? 36_000_000 : 5_400_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true, config: { scheduler: { containerCpus: 16, containerMemory: '12g' } } })
    const client = await connect(kernel)
    const { bind, judge, dataHash } = flow(client)
    const g3 = await throughS5Proxy(client)
    const { spec, rtl, trace, design } = g3
    const chip = out(design, 'chip')

    // S6: constraints from the chip spec, physical implementation.
    const views6 = await run(client, 'estimator', 'check_views', { calibration: out(g3.calibration, 'report') })
    expect((await judge('impl.views.S6', views6)).verdict).toBe('pass')
    await client.call('stages.enter', { project: 'chip', stage: 'S6' })
    const sdc = out(await run(client, 'eda-gf180', 'constraints', { chip }), 'constraints')
    const registry = await client.upload(JSON.stringify({ format: 'llmchip-timing-exceptions/1', exceptions: [] }), { type: 'timing.exceptions', mediaType: 'application/json' })
    const prerun = SPATIAL ? PRERUN_SPATIAL : PRERUN
    const impl = await run(client, 'eda-gf180', 'implement', { rtl, chip, constraints: sdc }, { placementDensityPct: prerun.placementDensityPct, ...(SPATIAL ? { synthHierarchy: PRERUN_SPATIAL.synthHierarchy } : {}) })
    expect(impl.metrics).toMatchObject({ die_area_mm2: prerun.die.xMm * prerun.die.yMm, routing_drc_errors: 0 })
    const layout = out(impl, 'layout')
    const gds = out(impl, 'gds')
    const measurements = await json(client, out(impl, 'measurements'))
    expect(measurements.timing).toBeTruthy() // FR-PHY-04
    expect((await client.call('artifacts.stat', { hash: out(impl, 'render') })).mediaType).toBe('image/png') // FR-PHY-05

    // S7: signoff, each check on exactly what it judges.
    const views7 = await run(client, 'estimator', 'check_views', { calibration: out(g3.calibration, 'report') })
    expect((await judge('impl.views.S7', views7)).verdict).toBe('pass')
    await client.call('stages.enter', { project: 'chip', stage: 'S7' })
    const rules = await dataHash('signoff-gf180mcu')
    for (const [slot, hash] of [['constraints', sdc], ['timing-exceptions', registry.hash], ['signoff-rules', rules], ['layout', layout], ['gds', gds]] as const) {
      await bind(slot, hash)
    }
    const drc = await run(client, 'eda-gf180', 'drc', { gds, layout, rules })
    const lvs = await run(client, 'eda-gf180', 'lvs', { gds, layout })
    const sta = await run(client, 'eda-gf180', 'sta', { layout, constraints: sdc, exceptions: registry.hash, chip })
    const gatesim = await run(client, 'sim-verilator', 'gatesim', { layout, rtl, spec, trace }, { prompts: 2, steps: 8 })
    expect((await judge('phy.drc', drc)).verdict).toBe('pass') // AC-05: zero DRC violations, no waiver on the prerun
    expect((await judge('phy.lvs', lvs)).verdict).toBe('pass')
    expect((await judge('phy.sta', sta)).verdict).toBe('pass')
    expect((await judge('phy.gatesim', gatesim)).verdict).toBe('pass')

    // AC-06: a removed input delay makes the timing check undecidable; an unregistered false path fails it.
    const sdcText = await (await client.download(sdc)).text()
    // The data input of the chip: kv_miso (SPI) or kv_sio0 (quad SPI) of the memory, or the host's
    // host_mosi on a chip without external memory (spatial).
    const dataPin = sdcText.includes('kv_sio0') ? 'kv_sio0' : sdcText.includes('kv_miso') ? 'kv_miso' : 'host_mosi'
    const removed = await client.upload(sdcText.split('\n').filter((l) => !l.includes('set_input_delay -clock clk -max') || !l.includes(dataPin)).join('\n'), { type: 'timing.constraints' })
    const noDelay = await run(client, 'eda-gf180', 'sta', { layout, constraints: removed.hash, exceptions: registry.hash, chip })
    expect(await judge('phy.sta', noDelay)).toMatchObject({ verdict: 'indeterminate', reasons: [{ code: 'COVERAGE_INCOMPLETE' }] })
    const falsePath = await client.upload(`${sdcText}set_false_path -from [get_ports host_mosi]\n`, { type: 'timing.constraints' })
    const exception = await run(client, 'eda-gf180', 'sta', { layout, constraints: falsePath.hash, exceptions: registry.hash, chip })
    expect(await judge('phy.sta', exception)).toMatchObject({ verdict: 'fail' })
    expect(exception.metrics.unregistered_exceptions).toBe(1)

    // AC-12: an SDF simulation whose annotation is incomplete never counts as timing evidence.
    const sdf = await (await client.download(out(impl, 'sdf'))).text()
    const truncated = await client.upload(sdf.slice(0, Math.floor(sdf.length / 2)).replace(/\(CELL\s*$/, ''), { type: 'timing.sdf' })
    const partial = await run(client, 'sim-verilator', 'sdfsim', { layout, sdf: truncated.hash }, { periodNs: 125 })
    expect(partial.metrics.sdf_annotation_complete).toBe(false)
    expect((await judge('sdf.timing', partial)).verdict).not.toBe('pass')

    // AC-08: from the GDS back to the model weights, through the runs that made each artifact.
    const lineage = await client.call('artifacts.lineage', { hash: gds, direction: 'up' })
    const types = new Set(lineage.nodes.map((n) => n.type))
    for (const type of ['model.weights.llama2c', 'numeric.spec', 'rtl.bundle', 'layout.gds']) expect(types).toContain(type)
    const implRun = await client.call('runs.get', { run: lineage.nodes.find((n) => n.type === 'layout.gds')!.run! })
    expect(implRun.plugin).toBe('eda-gf180')
    // The tool version of that run: the plugin's version and the image its manifest pins by digest
    // (the run record does not store the image itself; implementation guide §7.2).
    const eda = (await client.call('plugins.list')).plugins.find((p) => p.id === implRun.plugin)!
    expect(eda.version).toBeTruthy()
    expect(JSON.parse(readFileSync(join(eda.dir, 'llmchip-plugin.json'), 'utf8')).entry.image).toBe(IMAGE)
    // The verdicts recorded on the GDS: the signoff checks that judged it.
    const onGds = (await client.call('verdicts.list', { subject: gds })).verdicts
    for (const check of ['phy.drc', 'phy.lvs']) expect(onGds.find((v) => v.check === check)?.verdict).toBe('pass')

    // AC-14 (and FR-MIG-01/02): the target foundry, assessed for this standard-cell-ROM design.
    const migration = await run(client, 'estimator', 'migrate', { chip, current: await dataHash('gf180mcu'), target: await dataHash('sephonchitech-180bcd'), library: await dataHash('library-180nm') })
    expect(migration.metrics).toMatchObject({ restart_stage: 'S6', needs_owner_approval: false })
  })
})

