// K6 verdict engine, K3 projects and K7 approvals through the kernel API,
// with the stub tool and the test flow pack:
//   stages A -> B; gate GA after A with members [design, spec] and check stub.drc.

import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import { connect, FIXTURE_PLUGINS, startKernel } from './helpers.ts'

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

  it('applies only the waiver kind a check admits', async () => {
    const { client } = await setup()
    const design = (await client.upload('design', { type: 'test.design' })).hash
    const waiver = await client.call('waivers.create', { kind: 'risk_acceptance', check: 'stub.drc', subjects: [design], reason: 'known false positive' })
    expect(await verdict(client, 'stub.drc', design, { violations: 1 })).toMatchObject({ verdict: 'waived', waiver: waiver.id })
    // A coverage gap is never waived: it is not a failure but a missing check.
    expect(await verdict(client, 'stub.drc', design, { violations: 1, coverage: false })).toMatchObject({ verdict: 'indeterminate' })

    // Manufacturing rules: the owner cannot waive them, only the foundry can.
    await expect(client.call('waivers.create', { kind: 'risk_acceptance', check: 'stub.mfg', subjects: [design], reason: 'x' })).rejects.toMatchObject({
      code: 'WAIVER_NOT_ALLOWED',
    })
    await expect(client.call('waivers.create', { kind: 'foundry_waiver', check: 'stub.mfg', subjects: [design], reason: 'x' })).rejects.toMatchObject({
      code: 'FOUNDRY_ACK_REQUIRED',
    })
    const notAnAck = (await client.upload('email', { type: 'test.design' })).hash
    await expect(
      client.call('waivers.create', { kind: 'foundry_waiver', check: 'stub.mfg', subjects: [design], reason: 'x', acknowledgement: notAnAck }),
    ).rejects.toMatchObject({ code: 'FOUNDRY_ACK_REQUIRED' })
    const ack = (await client.upload('foundry letter', { type: 'foundry.waiver_ack' })).hash
    await client.call('waivers.create', { kind: 'foundry_waiver', check: 'stub.mfg', subjects: [design], reason: 'density waiver', acknowledgement: ack })
    expect(await verdict(client, 'stub.mfg', design, { violations: 1 })).toMatchObject({ verdict: 'waived' })
  })
})

describe('approvals and release (AC-M1-05, AC-M1-06)', () => {
  async function project(client: KernelClient) {
    await client.call('projects.create', { id: 'chip', title: 'Test chip', flow: 'test' })
    const design = (await client.upload('design v1', { type: 'test.design' })).hash
    const spec = (await client.upload('spec v1', { type: 'test.spec' })).hash
    await client.call('projects.bind', { project: 'chip', slot: 'design', hash: design })
    await client.call('projects.bind', { project: 'chip', slot: 'spec', hash: spec })
    await client.call('stages.enter', { project: 'chip', stage: 'A' })
    return { design, spec }
  }

  it('requires a complete snapshot and passing evidence before approving', async () => {
    const { client } = await setup()
    await client.call('projects.create', { id: 'chip', title: 'Test chip', flow: 'test' })
    const design = (await client.upload('design', { type: 'test.design' })).hash
    await client.call('projects.bind', { project: 'chip', slot: 'design', hash: design })
    await expect(client.call('approvals.decide', { project: 'chip', gate: 'GA', decision: 'approved' })).rejects.toMatchObject({ code: 'SNAPSHOT_INCOMPLETE' })
    await client.call('projects.bind', { project: 'chip', slot: 'spec', hash: (await client.upload('spec', { type: 'test.spec' })).hash })
    await expect(client.call('approvals.decide', { project: 'chip', gate: 'GA', decision: 'approved' })).rejects.toMatchObject({ code: 'GATE_EVIDENCE_INCOMPLETE' })
    await verdict(client, 'stub.drc', design, { truncate: true })
    await expect(client.call('approvals.decide', { project: 'chip', gate: 'GA', decision: 'approved' })).rejects.toMatchObject({ code: 'GATE_EVIDENCE_INCOMPLETE' })
    // A rejection needs no evidence.
    expect(await client.call('approvals.decide', { project: 'chip', gate: 'GA', decision: 'rejected', comment: 'not ready' })).toMatchObject({ decision: 'rejected' })
    expect(await client.call('gates.release', { project: 'chip', gate: 'GA' })).toMatchObject({ released: false, status: 'rejected' })
  })

  it('invalidates an approval when a snapshot member changes and blocks advancing (AC-M1-05)', async () => {
    const { client } = await setup()
    const { design } = await project(client)
    await expect(client.call('stages.enter', { project: 'chip', stage: 'B' })).rejects.toMatchObject({ code: 'STAGE_ENTRY_BLOCKED' })

    await verdict(client, 'stub.drc', design)
    const snapshot = await client.call('gates.snapshot', { project: 'chip', gate: 'GA' })
    expect(snapshot.evidence).toHaveLength(1)
    const approval = await client.call('approvals.decide', { project: 'chip', gate: 'GA', decision: 'approved' })
    expect(approval.snapshotHash).toBe(snapshot.hash)
    expect(await client.call('gates.release', { project: 'chip', gate: 'GA' })).toMatchObject({ released: true, status: 'approved' })
    await client.call('stages.enter', { project: 'chip', stage: 'B' })

    // Change one member of the approved snapshot.
    const design2 = (await client.upload('design v2', { type: 'test.design' })).hash
    await client.call('projects.bind', { project: 'chip', slot: 'design', hash: design2 })
    expect(await client.call('gates.release', { project: 'chip', gate: 'GA' })).toMatchObject({
      released: false,
      status: 'invalidated',
      reasons: [{ code: 'APPROVAL_INVALIDATED' }],
    })
    await expect(client.call('stages.enter', { project: 'chip', stage: 'B' })).rejects.toMatchObject({ code: 'STAGE_ENTRY_BLOCKED' })
    const { events } = await client.call('events.list', { kind: 'approval.invalidated' })
    expect(events.map((e) => e.payload)).toEqual([{ approval: approval.id, gate: 'GA', reason: { code: 'SNAPSHOT_MEMBER_CHANGED', params: { slot: 'design' } } }])
    // The old evidence does not judge the new design.
    expect((await client.call('gates.snapshot', { project: 'chip', gate: 'GA' })).evidence).toEqual([])
  })

  it('holds a conditional approval until every condition is closed by evidence (AC-M1-06)', async () => {
    const { client } = await setup()
    const { design, spec } = await project(client)
    await verdict(client, 'stub.drc', design)
    const approval = await client.call('approvals.decide', {
      project: 'chip',
      gate: 'GA',
      decision: 'conditional',
      conditions: [{ id: 'timing', title: 'Run the SDF timing check', owner: 'verifier', closeCriteria: 'stub.timing passes', check: 'stub.timing' }],
    })
    expect(await client.call('gates.release', { project: 'chip', gate: 'GA' })).toMatchObject({
      released: false,
      status: 'conditional_pending',
      reasons: [{ code: 'CONDITIONS_OPEN', params: { conditions: ['timing'] } }],
    })
    await expect(client.call('stages.enter', { project: 'chip', stage: 'B' })).rejects.toMatchObject({ code: 'STAGE_ENTRY_BLOCKED' })

    const close = (verdictId: string) => client.call('approvals.closeCondition', { approval: approval.id, condition: 'timing', verdict: verdictId })
    const wrongCheck = await verdict(client, 'stub.drc', design)
    await expect(close(wrongCheck.id)).rejects.toMatchObject({ code: 'CONDITION_EVIDENCE_INVALID' })
    const unqualified = await verdict(client, 'stub.timing', design)
    expect(unqualified.verdict).toBe('indeterminate')
    await expect(close(unqualified.id)).rejects.toMatchObject({ code: 'CONDITION_EVIDENCE_INVALID' })
    const qualification = await report(client, design, {}, 'qualify')
    await client.call('verdicts.evaluate', { check: 'stub-tool.qualify.sdf', run: qualification.id, project: 'chip' })
    const timing = await verdict(client, 'stub.timing', design)
    expect(timing.verdict).toBe('pass')

    const closed = await close(timing.id)
    expect(closed.conditions[0]!.closed).toMatchObject({ verdict: timing.id })
    expect(await client.call('gates.release', { project: 'chip', gate: 'GA' })).toMatchObject({ released: true, status: 'conditional_closed' })
    await client.call('stages.enter', { project: 'chip', stage: 'B' })

    // Changing the design afterwards invalidates the approval like any other.
    await client.call('projects.bind', { project: 'chip', slot: 'spec', hash: (await client.upload('spec v2', { type: 'test.spec' })).hash })
    expect((await client.call('gates.release', { project: 'chip', gate: 'GA' })).status).toBe('invalidated')
    expect(spec).not.toBe('')
  })

  it('rejects closing a condition after the snapshot changed', async () => {
    const { client } = await setup()
    const { design } = await project(client)
    await verdict(client, 'stub.drc', design)
    const approval = await client.call('approvals.decide', {
      project: 'chip',
      gate: 'GA',
      decision: 'conditional',
      conditions: [{ id: 'again', title: 'Re-run DRC', owner: 'verifier', closeCriteria: 'stub.drc passes', check: 'stub.drc' }],
    })
    await client.call('projects.bind', { project: 'chip', slot: 'spec', hash: (await client.upload('spec v2', { type: 'test.spec' })).hash })
    const rerun = await verdict(client, 'stub.drc', design)
    await expect(client.call('approvals.closeCondition', { approval: approval.id, condition: 'again', verdict: rerun.id })).rejects.toMatchObject({
      code: 'CONDITION_SNAPSHOT_CHANGED',
    })
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
