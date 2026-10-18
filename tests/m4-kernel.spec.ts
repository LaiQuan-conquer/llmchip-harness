// Kernel side of M4 with the stub tool and the test flow pack
// (stages A -> B -> C; gate GA after A, gate GB after B; `registry` is a hard-constraint slot):
// hard constraints only a human binds (FR-SGN-02b), export only of a released package with
// its lineage (FR-PKG-02/04, AC-08, AC-10), and migration invalidation (FR-MIG-03/04, AC-13/14).

import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import type { LlmProviderConfig } from '@llmchip/kernel-api'
import { connect, FIXTURE_PLUGINS, startKernel } from './helpers.ts'

async function setup(providers: LlmProviderConfig[] = []) {
  const { kernel } = await startKernel(undefined, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS] }, llm: { providers } } })
  const client = await connect(kernel)
  await client.call('projects.create', { id: 'chip', title: 'Test chip', flow: 'test' })
  return { kernel, client }
}

/** Binds the slots, judges stub.drc on `design` and approves the gate. */
async function approveGate(client: KernelClient, gate: string, design: string) {
  const run = await client.call('runs.start', { plugin: 'stub-tool', handler: 'report', params: {}, inputs: [{ name: 'design', hash: design }] })
  const finished = await client.call('runs.wait', { run: run.id })
  expect((await client.call('verdicts.evaluate', { check: 'stub.drc', run: finished.id, project: 'chip' })).verdict).toBe('pass')
  return client.call('approvals.decide', { project: 'chip', gate, decision: 'approved' })
}

async function throughGB(client: KernelClient) {
  const design = (await client.upload('design v1', { type: 'test.design' })).hash
  const spec = (await client.upload('spec v1', { type: 'test.spec' })).hash
  const registry = (await client.upload('{"exceptions": []}', { type: 'timing.exceptions' })).hash
  const pkg = (await client.upload('package v1', { type: 'tapeout.package', inputs: [design] })).hash
  for (const [slot, hash] of [['design', design], ['spec', spec], ['registry', registry], ['package', pkg]] as const) {
    await client.call('projects.bind', { project: 'chip', slot, hash })
  }
  const ga = await approveGate(client, 'GA', design)
  await client.call('stages.enter', { project: 'chip', stage: 'B' })
  const gb = await approveGate(client, 'GB', design)
  return { design, spec, registry, pkg, ga, gb }
}

describe('export of a released package (FR-PKG-04, AC-08, AC-10)', () => {
  it('exports only the approved snapshot, with the lineage of every artifact', async () => {
    const { client } = await setup()
    await expect(client.call('packages.export', { project: 'chip', gate: 'GB', slot: 'package' })).rejects.toMatchObject({ code: 'EXPORT_BLOCKED' })
    const { design, pkg } = await throughGB(client)
    const exported = await client.call('packages.export', { project: 'chip', gate: 'GB', slot: 'package' })
    expect(exported).toMatchObject({ package: pkg, lineageArtifacts: 2 })
    const archive = Buffer.from(await (await client.download(exported.archive.hash)).arrayBuffer())
    const lineage = JSON.parse(entry(archive, 'lineage.json'))
    // From the package back to its input, with the verdicts on each.
    expect(lineage.artifacts.map((a: { hash: string }) => a.hash).sort()).toEqual([design, pkg].sort())
    expect(lineage.artifacts.find((a: { hash: string }) => a.hash === design).verdicts[0]).toMatch(/^stub\.drc:pass:/)
    expect(lineage.edges).toEqual([expect.objectContaining({ from: design, to: pkg })])
    expect(entry(archive, 'package/package.tar')).toBe('package v1')
    // AC-10: changing any snapshot member after the approval invalidates GB and blocks the export.
    const changed = (await client.upload('package v2', { type: 'tapeout.package' })).hash
    await client.call('projects.bind', { project: 'chip', slot: 'package', hash: changed })
    expect(await client.call('gates.release', { project: 'chip', gate: 'GB' })).toMatchObject({ released: false, status: 'invalidated' })
    await expect(client.call('packages.export', { project: 'chip', gate: 'GB', slot: 'package' })).rejects.toMatchObject({ code: 'EXPORT_BLOCKED' })
  })
})

describe('hard-constraint slots (FR-SGN-02b)', () => {
  it('lets a human bind the timing exception registry and stops an agent', async () => {
    const call = (name: string, args: Record<string, unknown> = {}) => ({ toolCalls: [{ name, arguments: args }] })
    const { client } = await setup([
      {
        id: 'agent',
        kind: 'scripted',
        location: 'local',
        // Looks up the registry artifacts, then tries to bind one of them.
        script: [call('list_artifacts', { type: 'timing.exceptions' }), call('bind_slot', { slot: 'registry', hash: '{{result.0.0.hash}}' }), call('finish', { result: 'done' })],
      },
    ])
    const { registry } = await throughGB(client)
    const loosened = (await client.upload('{"exceptions": [{"command": "set_false_path -from [get_ports a]"}]}', { type: 'timing.exceptions' })).hash
    const scripted = await client.call('tasks.create', {
      project: 'chip',
      goal: 'FR-SGN-02b',
      title: 'Relax timing',
      role: 'executor',
      provider: 'agent',
      writeScope: [],
    })
    await client.call('tasks.run', { task: scripted.id, maxSteps: 3 })
    const incidents = (await client.call('incidents.list', { kind: 'hard_constraint_change' })).incidents
    expect(incidents.map((i) => i.summary.code)).toContain('HARD_CONSTRAINT_REQUIRES_HUMAN')
    // The registry is unchanged; GA and GB are still released.
    expect((await client.call('projects.get', { project: 'chip' })).slots.registry).toBe(registry)
    for (const gate of ['GA', 'GB']) expect((await client.call('gates.release', { project: 'chip', gate })).released).toBe(true)
    // A human may bind it (a new snapshot: GB must be approved again).
    await client.call('projects.bind', { project: 'chip', slot: 'registry', hash: loosened })
    expect(await client.call('gates.release', { project: 'chip', gate: 'GB' })).toMatchObject({ released: false, status: 'invalidated' })
  })
})

describe('migration (FR-MIG-03/04, AC-13, AC-14)', () => {
  it('invalidates the gates after the restart stage and voids their earlier evidence', async () => {
    const { client } = await setup()
    await throughGB(client)
    const s6like = (await client.upload(JSON.stringify({ restartStage: 'B' }), { type: 'migration.report' })).hash
    expect(await client.call('migration.apply', { project: 'chip', report: s6like })).toEqual({ restartStage: 'B', invalidatedGates: ['GB'] })
    expect((await client.call('gates.release', { project: 'chip', gate: 'GA' })).released).toBe(true)
    expect(await client.call('gates.release', { project: 'chip', gate: 'GB' })).toMatchObject({ released: false, status: 'invalidated' })
    expect((await client.call('gates.snapshot', { project: 'chip', gate: 'GB' })).evidence).toEqual([])
    const early = (await client.upload(JSON.stringify({ restartStage: 'A' }), { type: 'migration.report' })).hash
    expect(await client.call('migration.apply', { project: 'chip', report: early })).toEqual({ restartStage: 'A', invalidatedGates: ['GA', 'GB'] })
    expect(await client.call('gates.release', { project: 'chip', gate: 'GA' })).toMatchObject({ released: false, status: 'invalidated' })
    const events = (await client.call('events.list', { kind: 'approval.invalidated' })).events
    expect(events.map((e) => (e.payload as { reason: { code: string } }).reason.code)).toEqual(['MIGRATION_APPLIED', 'MIGRATION_APPLIED'])
  })
})

/** Content of one entry of a USTAR archive. */
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
