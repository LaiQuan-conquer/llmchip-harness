// K6 verdict engine and K3 projects through the kernel API, with the stub tool
// and the test flow pack (stages A, B, C; no approval gates, no waivers: owner
// decision 2026-10-24).

import { cpSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import type { Actor } from '@llmchip/kernel-api'
import { connect, FIXTURE_PLUGINS, startKernel, tempDir } from './helpers.ts'

async function setup() {
  const { kernel } = await startKernel(undefined, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS] } } })
  const client = await connect(kernel)
  return { kernel, client }
}

async function report(client: KernelClient, design: string, params: Record<string, unknown> = {}, handler = 'report') {
  const run = await client.call('runs.start', {
    plugin: 'stub-tool',
    handler,
    params,
    inputs: handler === 'report' ? [{ name: 'design', hash: design }] : [],
  })
  return client.call('runs.wait', { run: run.id })
}

async function verdict(client: KernelClient, check: string, design: string, params: Record<string, unknown> = {}) {
  const run = await report(client, design, params)
  return client.call('verdicts.evaluate', { check, run: run.id, project: 'chip' })
}

describe('verdict engine (AC-M1-03)', () => {
  it('never turns a run that was not fully checked into a pass', async () => {
    const { client } = await setup()
    const design = (await client.upload('design v1', { type: 'test.design' })).hash

    expect(await verdict(client, 'stub.drc', design)).toMatchObject({ verdict: 'pass', subjects: [design], parse: 'complete' })
    expect(await verdict(client, 'stub.drc', design, { violations: 2 })).toMatchObject({
      verdict: 'fail',
      reasons: [{ code: 'CRITERION_FAILED', params: { metric: 'violations', actual: 2 } }],
    })
    // The tool exits normally but its report is truncated.
    expect(await verdict(client, 'stub.drc', design, { truncate: true })).toMatchObject({
      verdict: 'indeterminate',
      parse: 'incomplete',
      reasons: [{ code: 'PARSE_INCOMPLETE' }],
    })
    // Zero violations, but the check did not cover everything (DP-10).
    expect(await verdict(client, 'stub.drc', design, { coverage: false })).toMatchObject({
      verdict: 'indeterminate',
      reasons: [{ code: 'COVERAGE_INCOMPLETE' }],
    })
  })

  it('applies a conditional criterion only when its condition holds, and says so', async () => {
    const { client } = await setup()
    const design = (await client.upload('design', { type: 'test.design' })).hash
    // A limit is set: the criterion is judged.
    expect(await verdict(client, 'stub.limit', design, { violations: 1, limit: 2 })).toMatchObject({ verdict: 'pass', reasons: [] })
    expect(await verdict(client, 'stub.limit', design, { violations: 3, limit: 2 })).toMatchObject({
      verdict: 'fail',
      reasons: [{ code: 'CRITERION_FAILED', params: { metric: 'limit.met', actual: false } }],
    })
    // No limit: the criterion is not applied, and the verdict records it.
    expect(await verdict(client, 'stub.limit', design, { violations: 3 })).toMatchObject({
      verdict: 'pass',
      reasons: [{ code: 'CRITERION_NOT_APPLIED', params: { metric: 'limit.met', condition: 'limit.set == true', actual: false } }],
    })
    // A report without the condition metric is not a pass (DP-10).
    expect(await verdict(client, 'stub.limit', design, { violations: 3, conditional: false })).toMatchObject({
      verdict: 'indeterminate',
      reasons: [{ code: 'METRIC_MISSING', params: { metric: 'limit.set' } }],
    })
  })

  it('treats crashed runs and unqualified capabilities as indeterminate', async () => {
    const { client } = await setup()
    const design = (await client.upload('design', { type: 'test.design' })).hash
    const crashed = await client.call('runs.wait', { run: (await client.call('runs.start', { plugin: 'stub-tool', handler: 'crash' })).id })
    await expect(client.call('verdicts.evaluate', { check: 'stub.drc', run: crashed.id, project: 'p' })).rejects.toMatchObject({ code: 'CHECK_RUN_MISMATCH' })

    expect(await verdict(client, 'stub.drc', design, { crash: true })).toMatchObject({
      verdict: 'indeterminate',
      reasons: [{ code: 'EXECUTION_NOT_ACCEPTED', params: { execution: 'crashed', exitCode: 2 } }],
    })

    expect(await verdict(client, 'stub.timing', design)).toMatchObject({ verdict: 'indeterminate', reasons: [{ code: 'CAPABILITY_NOT_QUALIFIED' }] })
    // A failed qualification leaves the capability unqualified.
    const failedQualification = await report(client, design, { detected: false }, 'qualify')
    expect(await client.call('verdicts.evaluate', { check: 'stub-tool.qualify.sdf', run: failedQualification.id, project: 'p' })).toMatchObject({ verdict: 'fail' })
    expect(await verdict(client, 'stub.timing', design)).toMatchObject({ verdict: 'indeterminate' })
    const qualification = await report(client, design, {}, 'qualify')
    expect(await client.call('verdicts.evaluate', { check: 'stub-tool.qualify.sdf', run: qualification.id, project: 'p' })).toMatchObject({ verdict: 'pass' })
    expect(await verdict(client, 'stub.timing', design)).toMatchObject({ verdict: 'pass' })
  })

  it('keeps a failure a failure: there are no waivers, only a fixed design that passes again', async () => {
    const { client } = await setup()
    const call = client.call.bind(client) as (method: string, params?: unknown) => Promise<unknown>
    const design = (await client.upload('design v1', { type: 'test.design' })).hash
    // The waiver method no longer exists.
    await expect(call('waivers.create', { kind: 'risk_acceptance', check: 'stub.drc', subjects: [design], reason: 'known false positive' })).rejects.toMatchObject({
      rpcCode: -32601,
    })
    // A failure is recorded as a failure and opens an incident.
    const failed = await verdict(client, 'stub.drc', design, { violations: 1 })
    expect(failed).toMatchObject({ verdict: 'fail' })
    expect(failed).not.toHaveProperty('waiver')
    const { incidents } = await client.call('incidents.list', { open: true, kind: 'check_failed' })
    expect(incidents.some((i) => i.kind === 'check_failed' && i.summary.params?.verdictId === failed.id)).toBe(true)
    // The fix is a new design; its run passes, and the failure stays on record.
    const fixed = (await client.upload('design v2', { type: 'test.design' })).hash
    expect(await verdict(client, 'stub.drc', fixed)).toMatchObject({ verdict: 'pass', subjects: [fixed] })
    const history = await client.call('verdicts.list', { check: 'stub.drc' })
    expect(history.verdicts.map((v) => v.verdict)).toEqual(['pass', 'fail'])
  })
})

describe('projects and stages', () => {
  async function project(client: KernelClient) {
    await client.call('projects.create', { id: 'chip', title: 'Test chip', flow: 'test' })
    const design = (await client.upload('design v1', { type: 'test.design' })).hash
    const spec = (await client.upload('spec v1', { type: 'test.spec' })).hash
    await client.call('projects.bind', { project: 'chip', slot: 'design', hash: design })
    await client.call('projects.bind', { project: 'chip', slot: 'spec', hash: spec })
    await client.call('stages.enter', { project: 'chip', stage: 'A' })
    return { design, spec }
  }

  it('enters stages without approval gates; a failed check does not lock the flow', async () => {
    const { client } = await setup()
    const call = client.call.bind(client) as (method: string, params?: unknown) => Promise<unknown>
    const { design } = await project(client)
    expect(await verdict(client, 'stub.drc', design, { violations: 3 })).toMatchObject({ verdict: 'fail' })
    await client.call('stages.enter', { project: 'chip', stage: 'B' })
    await client.call('stages.enter', { project: 'chip', stage: 'C' })
    const state = await client.call('projects.get', { project: 'chip' })
    expect(state.stages).toEqual([
      { id: 'A', entered: true },
      { id: 'B', entered: true },
      { id: 'C', entered: true },
    ])
    expect(state).not.toHaveProperty('gates')
    for (const method of ['gates.snapshot', 'gates.release', 'approvals.decide', 'approvals.list', 'packages.export']) {
      await expect(call(method, { project: 'chip', gate: 'GA' })).rejects.toMatchObject({ rpcCode: -32601 })
    }
    await expect(client.call('stages.enter', { project: 'chip', stage: 'Z' })).rejects.toMatchObject({ code: 'STAGE_NOT_FOUND' })
  })

  it('branches from a checkpoint to roll back without rewriting history', async () => {
    const { client } = await setup()
    const { design } = await project(client)
    const checkpoint = (await client.call('kernel.info')).head
    const design2 = (await client.upload('design v2', { type: 'test.design' })).hash
    await client.call('projects.bind', { project: 'chip', slot: 'design', hash: design2 })
    await client.call('projects.branch', { project: 'chip', name: 'rollback', atSeq: checkpoint })
    const state = await client.call('projects.get', { project: 'chip', branch: 'rollback' })
    expect(state.branches).toEqual(['main', 'rollback'])
    expect(state.slots.design).toBe(design)
    expect((await client.call('projects.get', { project: 'chip' })).slots.design).toBe(design2)
    await expect(client.call('projects.branch', { project: 'chip', name: 'rollback' })).rejects.toMatchObject({ code: 'BRANCH_EXISTS' })
  })
})

describe('a home written before approval gates and waivers were removed', () => {
  /** The test flow pack as it was before R0: gates, gated stage entries, checks admitting waivers. */
  function oldFlowPack(): Record<string, any> {
    const pack = JSON.parse(readFileSync(join(FIXTURE_PLUGINS, 'test-flow/flow.json'), 'utf8'))
    pack.gates = [{ id: 'GA', title: 'Design freeze', after: 'A', members: ['design', 'spec'], checks: ['stub.drc'] }]
    pack.stages.find((s: { id: string }) => s.id === 'B').entry = { gates: ['GA'] }
    pack.checks.find((c: { id: string }) => c.id === 'stub.drc').waiver = 'risk_acceptance'
    return pack
  }

  it('still opens old projects, verdicts and events, and replays them', async () => {
    const { kernel } = await startKernel(undefined, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS] } } })
    const client = await connect(kernel)
    const flowHash = (await client.upload(JSON.stringify(oldFlowPack()), { type: 'flow.pack', mediaType: 'application/json' })).hash
    const design = (await client.upload('design', { type: 'test.design' })).hash
    const owner: Actor = { kind: 'human', id: 'owner' }
    const at = new Date().toISOString()
    // What the kernel wrote before R0 (it is the only writer of events, so the test appends them here).
    const history: [string, Record<string, unknown>][] = [
      ['project.created', { project: { id: 'old', title: 'Old chip', flow: 'test', flowHash, createdAt: at } }],
      ['verdict.recorded', { verdict: { id: 'V1', check: 'stub.drc', checkVersion: '1', subjects: [design], run: null, parse: 'complete', verdict: 'waived', reasons: [], reqRef: null, waiver: 'W1', createdAt: at } }],
      ['waiver.recorded', { waiver: { id: 'W1', kind: 'risk_acceptance', check: 'stub.drc', subjects: [design], reason: 'old', acknowledgement: null, by: owner, at } }],
      ['approval.recorded', { approval: { id: 'A1', project: 'old', branch: 'main', gate: 'GA', decision: 'approved', snapshotHash: design, evidenceHash: design, conditions: [], comment: '', by: owner, at } }],
      ['approval.invalidated', { approval: 'A1', gate: 'GA', reason: { code: 'SNAPSHOT_MEMBER_CHANGED', params: { slot: 'design' } } }],
      ['package.exported', { project: 'old', branch: 'main', gate: 'GA', approval: 'A1', package: design, archive: design }],
      ['migration.applied', { project: 'old', branch: 'main', report: design, restartStage: 'A', gates: ['GA'] }],
    ]
    for (const [kind, payload] of history) kernel.events.append({ kind, payload, actor: owner, project: 'old' })

    const read = async () => {
      const state = await client.call('projects.get', { project: 'old' })
      expect(state.stages.map((s) => s.id)).toEqual(['A', 'B', 'C'])
      expect(state).not.toHaveProperty('gates')
      expect((await client.call('verdicts.list', { check: 'stub.drc' })).verdicts).toEqual([expect.objectContaining({ id: 'V1', verdict: 'waived' })])
      const { events } = await client.call('events.list', { limit: 1000 })
      for (const [kind] of history) expect(events.map((e) => e.kind)).toContain(kind)
    }
    await read()
    // Stage B's old entry gate is history: it no longer blocks.
    await client.call('stages.enter', { project: 'old', stage: 'B' })
    await client.call('projections.rebuild', {})
    await read()
    expect((await client.call('events.verify', {})).ok).toBe(true)
  })

  it('refuses a newly registered flow pack that still declares gates', async () => {
    const dir = tempDir()
    cpSync(join(FIXTURE_PLUGINS, 'test-flow'), join(dir, 'old-flow'), { recursive: true })
    const manifest = JSON.parse(readFileSync(join(dir, 'old-flow/llmchip-plugin.json'), 'utf8'))
    writeFileSync(join(dir, 'old-flow/llmchip-plugin.json'), JSON.stringify({ ...manifest, id: 'old-flow' }))
    writeFileSync(join(dir, 'old-flow/flow.json'), JSON.stringify({ ...oldFlowPack(), id: 'old' }))
    const { kernel, home } = await startKernel(undefined, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS, dir] } } })
    const client = await connect(kernel)
    // The pack is not loaded; the problem goes to the kernel log.
    await expect(client.call('projects.create', { id: 'p', title: 'p', flow: 'old' })).rejects.toMatchObject({ code: 'FLOW_NOT_FOUND' })
    const log = readFileSync(join(home, 'logs/harnessd.log'), 'utf8')
    expect(log).toMatch(/FLOW_INVALID.*old-flow.*must NOT have additional properties/)
  })
})
