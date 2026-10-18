// M1.5 acceptance (AC-M15-01..03): storage implementation calibration on
// GF180MCU with real tools in the pinned image, admission checks on integration
// views, and the calibrated estimate behind the G0 reference-model decision.
// Skipped, visibly, where the tool image is not installed.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'
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

async function run(client: KernelClient, plugin: string, handler: string, params: Record<string, unknown>, inputs: { name: string; hash: string }[] = []): Promise<RunRecord> {
  const started = await client.call('runs.start', { plugin, handler, params, inputs, project: 'prerun' })
  for (;;) {
    const record = await client.call('runs.wait', { run: started.id, timeoutMs: 600_000 })
    if (record.state === 'finished') return record
  }
}

async function json(client: KernelClient, hash: string) {
  return JSON.parse(await (await client.download(hash)).text())
}

async function dataHash(client: KernelClient, id: string): Promise<string> {
  return (await client.call('plugins.list')).plugins.flatMap((p) => p.data).find((d) => d.id === id)!.hash
}

describe.skipIf(!available(IMAGE))('storage calibration on GF180MCU', () => {
  it('ends every candidate as available or unavailable, gates stages on views, and feeds the estimate', { timeout: 900_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    await client.call('projects.create', { id: 'prerun', title: 'GF180MCU prerun' })

    // AC-M15-01: every candidate ends with a conclusion and evidence.
    const stdcell = await run(client, 'eda-gf180', 'calibrate', { implementation: 'gf180mcu.stdcell-rom', sizes: [[256, 16], [1024, 16]] })
    const sram = await run(client, 'eda-gf180', 'calibrate', { implementation: 'gf180mcu.sram-512x8' })
    const openram = await run(client, 'eda-gf180', 'calibrate', { implementation: 'gf180mcu.openram-rom' })
    for (const r of [stdcell, sram, openram]) expect(r).toMatchObject({ execution: 'exited_zero', parse: { status: 'complete' } })

    const reports = {
      stdcell: await json(client, stdcell.outputs[0]!.hash),
      sram: await json(client, sram.outputs[0]!.hash),
      openram: await json(client, openram.outputs[0]!.hash),
    }
    expect(reports.stdcell).toMatchObject({ availability: 'available', densityUm2PerBit: { confidence: 'calibrated' } })
    expect(reports.stdcell.measurements.map((m: { bits: number }) => m.bits)).toEqual([4096, 16384])
    expect(reports.stdcell.densityUm2PerBit.typ).toBeGreaterThan(0.5)
    expect(reports.stdcell.densityUm2PerBit.typ).toBeLessThan(20)
    expect(reports.sram).toMatchObject({ availability: 'available', views: { verilog: true, liberty: true, lef: true, gds: true, lvs_netlist: true } })
    expect(reports.sram.measurements[0]).toMatchObject({ widthUm: 431.86, heightUm: 484.88, bits: 4096 })
    expect(reports.openram).toMatchObject({ availability: 'unavailable', missingViews: { S4: ['verilog'], S6: ['liberty', 'lef'] } })
    expect(reports.openram.failure).toMatch(/OpenRAM is not part of the pinned tool image/)

    // AC-M15-03: a candidate without the views a stage needs is stopped at that stage, with the missing views listed.
    const views = async (r: RunRecord) => {
      const checked = await run(client, 'estimator', 'check_views', {}, [{ name: 'calibration', hash: r.outputs[0]!.hash }])
      return client.call('verdicts.evaluate', { check: 'impl.views.S4', run: checked.id, project: 'prerun' })
    }
    expect((await views(openram)).verdict).toBe('fail')
    expect((await views(stdcell)).verdict).toBe('pass')
    await client.call('projects.bind', { project: 'prerun', slot: 'storage-impl', hash: openram.outputs[0]!.hash })
    const blocked = await client.call('stages.enter', { project: 'prerun', stage: 'S4' }).catch((error) => error)
    expect(blocked).toMatchObject({ code: 'STAGE_ENTRY_BLOCKED' })
    expect(blocked.params.checks).toEqual([
      expect.objectContaining({ check: 'impl.views.S4', slot: 'storage-impl', verdict: 'fail', details: { 'views.S4.missing': 'verilog' } }),
    ])
    expect(blocked.message).toMatch(/impl\.views\.S4 on storage-impl \(fail; verilog\)/)
    await client.call('projects.bind', { project: 'prerun', slot: 'storage-impl', hash: stdcell.outputs[0]!.hash })
    const now = await client.call('stages.enter', { project: 'prerun', stage: 'S4' }).catch((error) => error)
    expect(now.params.checks).toEqual([]) // Only the gate G2 still blocks.

    // AC-M15-02 (input to the owner's G0 decision): the estimate uses calibrated values.
    const estimate = await run(client, 'estimator', 'estimate', { model: { preset: 'stories260K' }, weightStorage: 'gf180mcu.stdcell-rom' }, [
      { name: 'profile', hash: await dataHash(client, 'gf180mcu') },
      { name: 'library', hash: await dataHash(client, 'library-180nm') },
      { name: 'calibration', hash: stdcell.outputs[0]!.hash },
      { name: 'calibration', hash: openram.outputs[0]!.hash },
    ])
    const report = await json(client, estimate.outputs.find((o) => o.name === 'report')!.hash)
    const weights = report.items.find((i: { item: string }) => i.item === 'weight storage')
    expect(weights.confidence).toBe('calibrated')
    expect(report.reverse.maxParams['2'].typ).toBeGreaterThan(0)
  })
})
