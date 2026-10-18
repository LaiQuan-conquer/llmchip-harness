// M4 acceptance: S0-S8 on GF180MCU through the kernel with the bundled plugins
// and the proxy of the reference model, the human approving at G0-G4 (AC-02 for
// the prerun): physical implementation, signoff (DRC, LVS, multi-corner STA with
// constraint completeness, gate-level simulation), the tapeout package, its
// integrity check, G4 and the export with lineage (AC-05, AC-08, AC-10). Reverse
// tests: a removed constraint and an unregistered false path block signoff
// (AC-06), an SDF result with incomplete annotation never passes (AC-12), an
// external report for another GDS does not count (AC-11), and the migration
// assessment to the target foundry restarts at S6 for this standard-cell-ROM design
// (AC-14).
//
// The full flow runs the EDA tools for about half an hour, so it is opt-in:
// LLMCHIP_FULL_FLOW=1 (and the pinned image installed); otherwise it is skipped, visibly.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import { flow, out, run, throughG3Proxy } from './flow-helpers.ts'
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

describe.skipIf(!process.env.LLMCHIP_FULL_FLOW || !available(IMAGE))('M4: physical implementation, signoff and tapeout package on GF180MCU', () => {
  it('runs S0-S8, signs off, approves G4 and exports the package with its lineage', { timeout: 5_400_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true, config: { scheduler: { containerCpus: 16, containerMemory: '12g' } } })
    const client = await connect(kernel)
    const { bind, judge, approve, dataHash } = flow(client)
    const g3 = await throughG3Proxy(client)
    const { spec, rtl, trace, design, l2 } = g3
    const chip = out(design, 'chip')

    // S6: constraints from the chip spec, physical implementation.
    const views6 = await run(client, 'estimator', 'check_views', { calibration: out(g3.calibration, 'report') })
    expect((await judge('impl.views.S6', views6)).verdict).toBe('pass')
    await client.call('stages.enter', { project: 'chip', stage: 'S6' })
    const sdc = out(await run(client, 'eda-gf180', 'constraints', { chip }), 'constraints')
    const registry = await client.upload(JSON.stringify({ format: 'llmchip-timing-exceptions/1', exceptions: [] }), { type: 'timing.exceptions', mediaType: 'application/json' })
    const impl = await run(client, 'eda-gf180', 'implement', { rtl, chip, constraints: sdc })
    expect(impl.metrics).toMatchObject({ die_area_mm2: 2.4, routing_drc_errors: 0 })
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
    const removed = await client.upload(sdcText.split('\n').filter((l) => !l.includes('set_input_delay -clock clk -max') || !l.includes('kv_miso')).join('\n'), { type: 'timing.constraints' })
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

    // AC-11: an external DRC report bound to another GDS is marked as not matching the design.
    const external = await client.upload(
      JSON.stringify({ check: 'drc', gdsHash: 'f'.repeat(64), pdkVersion: 'gf180mcuD', ruleFileVersion: 'v1', tool: 'calibre', toolVersion: '2025.1', conditions: { switches: ['FEOL', 'BEOL'] }, topCell: 'chip_top', violations: 0 }),
      { type: 'external.report' },
    )
    const imported = await run(client, 'packager', 'import_external', { report: external.hash, gds })
    expect(imported.metrics).toMatchObject({ binding_complete: true, matches_current_design: false })
    expect((await judge('external.signoff', imported)).verdict).toBe('fail')

    // S8: tapeout package and its integrity check, then G4 and the export.
    await client.call('stages.enter', { project: 'chip', stage: 'S8' })
    const template = await dataHash('package-gf180mcu')
    const pkg = await run(client, 'packager', 'build', {
      template, gds, layout, rtl, chip, pads: out(impl, 'pads'), drc: out(drc, 'report'), lvs: out(lvs, 'report'), sta: out(sta, 'report'),
      constraints: sdc, exceptions: registry.hash, measurements: out(impl, 'measurements'), l2: out(l2, 'outputs'),
    })
    await bind('package-template', template)
    await bind('tapeout-package', out(pkg, 'package'))
    const integrity = await run(client, 'packager', 'check', { package: out(pkg, 'package'), template, gds })
    expect((await judge('package.integrity', integrity)).verdict).toBe('pass')
    const g4 = await approve('G4')
    const exported = await client.call('packages.export', { project: 'chip' })
    expect(exported.approval).toBe(g4.id)
    // AC-08: from the package back to the model weights, with runs, tool versions and verdicts.
    const tar = Buffer.from(await (await client.download(exported.archive.hash)).arrayBuffer())
    const lineage = JSON.parse(entry(tar, 'lineage.json'))
    const types = new Set(lineage.artifacts.map((a: { type: string }) => a.type))
    for (const type of ['model.weights.llama2c', 'numeric.spec', 'rtl.bundle', 'layout.gds', 'drc.report', 'tapeout.package']) expect(types).toContain(type)
    expect(lineage.runs.some((r: { plugin: string; image: string | null }) => r.plugin === 'eda-gf180' && r.image === IMAGE)).toBe(true)

    // AC-10: changing a snapshot member after G4 (here the constraints) invalidates it and blocks the export.
    await bind('constraints', removed.hash)
    expect(await client.call('gates.release', { project: 'chip', gate: 'G4' })).toMatchObject({ released: false, status: 'invalidated' })
    await expect(client.call('packages.export', { project: 'chip' })).rejects.toMatchObject({ code: 'EXPORT_BLOCKED' })

    // AC-14 (and FR-MIG-01/02): the target foundry, assessed for this standard-cell-ROM design.
    const migration = await run(client, 'estimator', 'migrate', { chip, current: await dataHash('gf180mcu'), target: await dataHash('sephonchitech-180bcd'), library: await dataHash('library-180nm') })
    expect(migration.metrics).toMatchObject({ restart_stage: 'S6', needs_owner_approval: false })
  })
})

function entry(tar: Buffer, name: string): string {
  let offset = 0
  while (offset + 512 <= tar.length && tar[offset] !== 0) {
    const entryName = tar.subarray(offset, offset + 100).toString('utf8').replace(/\0.*$/s, '')
    const size = parseInt(tar.subarray(offset + 124, offset + 136).toString('utf8').replace(/\0.*$/s, '').trim(), 8)
    if (entryName === name) return tar.subarray(offset + 512, offset + 512 + size).toString('utf8')
    offset += 512 + Math.ceil(size / 512) * 512
  }
  throw new Error(`${name} is not in the archive`)
}
