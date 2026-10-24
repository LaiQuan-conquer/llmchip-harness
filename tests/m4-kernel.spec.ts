// Kernel side of M4 with the stub tool and the test flow pack (stages A -> B -> C;
// `registry` is a hard-constraint slot): hard constraints only a human binds
// (FR-SGN-02b), and a confirmed migration records the stage to restart (FR-MIG-03).
// There are no approval gates and no export (owner decision 2026-10-24).

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

/** Binds the design, the spec and the timing exception registry. */
async function bindAll(client: KernelClient) {
  const design = (await client.upload('design v1', { type: 'test.design' })).hash
  const spec = (await client.upload('spec v1', { type: 'test.spec' })).hash
  const registry = (await client.upload('{"exceptions": []}', { type: 'timing.exceptions' })).hash
  for (const [slot, hash] of [['design', design], ['spec', spec], ['registry', registry]] as const) {
    await client.call('projects.bind', { project: 'chip', slot, hash })
  }
  return { design, spec, registry }
}

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
    const { registry } = await bindAll(client)
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
    // The registry is unchanged.
    expect((await client.call('projects.get', { project: 'chip' })).slots.registry).toBe(registry)
    // A human may bind it.
    await client.call('projects.bind', { project: 'chip', slot: 'registry', hash: loosened })
    expect((await client.call('projects.get', { project: 'chip' })).slots.registry).toBe(loosened)
  })
})

describe('migration (FR-MIG-03, AC-14)', () => {
  it('records the stage to restart and rejects an unknown stage', async () => {
    const { client } = await setup()
    await bindAll(client)
    const late = (await client.upload(JSON.stringify({ restartStage: 'B' }), { type: 'migration.report' })).hash
    expect(await client.call('migration.apply', { project: 'chip', report: late, comment: 'new PDK' })).toEqual({ restartStage: 'B' })
    const none = (await client.upload(JSON.stringify({ restartStage: null }), { type: 'migration.report' })).hash
    expect(await client.call('migration.apply', { project: 'chip', report: none })).toEqual({ restartStage: 'none' })
    const unknown = (await client.upload(JSON.stringify({ restartStage: 'Z' }), { type: 'migration.report' })).hash
    await expect(client.call('migration.apply', { project: 'chip', report: unknown })).rejects.toMatchObject({ code: 'MIGRATION_REPORT_INVALID' })
    const events = (await client.call('events.list', { kind: 'migration.applied' })).events
    expect(events.map((e) => e.payload)).toEqual([{ project: 'chip', branch: 'main', report: late, restartStage: 'B', comment: 'new PDK' }])
  })
})
