// M1c acceptance: managed agents, policy, egress gateway and handoff
// (AC-M1-02, AC-M1-07, AC-M1-08, AC-M1-09, AC-M1-13). LLM providers are
// scripted, so the tests are deterministic and need no API key; a scripted
// provider is subject to the same egress checks as a real one.

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { KernelClient } from '@llmchip/client'
import { readRuntimeInfo, sha256 } from '@llmchip/kernel'
import type { EventRecord, LlmProviderConfig, TaskSpec } from '@llmchip/kernel-api'
import { connect, FIXTURE_PLUGINS, startKernel } from './helpers.ts'

const FAKE_PDK = readFileSync(resolve(import.meta.dirname, 'fixtures/fake-pdk/design-rules.txt'), 'utf8')
const PDK_HASH = sha256(FAKE_PDK)

type Step = { text?: string; toolCalls?: { name: string; arguments?: Record<string, unknown> }[] }
const scripted = (id: string, location: 'local' | 'cloud', script: Step[]): LlmProviderConfig => ({ id, kind: 'scripted', location, script })
const call = (name: string, args: Record<string, unknown> = {}): Step => ({ toolCalls: [{ name, arguments: args }] })
const finish = (result = 'done') => call('finish', { result })

async function setup(providers: LlmProviderConfig[], home?: string) {
  const { kernel } = await startKernel(home, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS] }, llm: { providers } } })
  const client = await connect(kernel)
  if (!home) await client.call('projects.create', { id: 'chip', title: 'Test chip', flow: 'test' })
  return { kernel, client }
}

function task(provider: string, extra: Partial<TaskSpec> = {}): TaskSpec {
  return { project: 'chip', goal: 'FR-RTL-01', title: 'Write the RMSNorm generator', role: 'executor', provider, writeScope: ['generators/**'], ...extra }
}

async function incidentCodes(client: KernelClient, kind?: string): Promise<string[]> {
  return (await client.call('incidents.list', kind ? { kind } : {})).incidents.map((i) => `${i.kind}:${i.summary.code}`).sort()
}

async function egress(client: KernelClient): Promise<EventRecord[]> {
  return (await client.call('events.list', { kind: 'egress.decided', limit: 1000 })).events
}

/** No allowed request to a cloud destination carried NDA content. */
async function expectNoNdaToCloud(client: KernelClient): Promise<void> {
  for (const event of await egress(client)) {
    const { destination, segments, decision } = event.payload as { destination: { location: string }; segments: { label: string }[]; decision: string }
    if (destination.location === 'cloud' && decision === 'allowed') expect(segments.map((s) => s.label)).not.toContain('nda')
  }
}

describe('managed agents (AC-M1-02)', () => {
  it('runs a task through write, submit, independent review and real verification, with a full trajectory', async () => {
    const { client } = await setup([
      scripted('author', 'cloud', [
        { text: 'Writing the generator.', toolCalls: [{ name: 'write_file', arguments: { path: 'generators/rmsnorm.py', content: 'def rmsnorm(x): ...' } }] },
        call('submit_changeset', { summary: 'RMSNorm generator' }),
        finish('change set submitted'),
      ]),
      scripted('reviewer', 'cloud', [
        call('list_artifacts', { type: 'changeset.file' }),
        call('start_run', { plugin: 'stub-tool', handler: 'report', inputs: [{ name: 'design', hash: '{{result.0.0.hash}}' }] }),
        call('evaluate_check', { check: 'stub.drc', run: '{{result.0.id}}' }),
      ]),
    ])
    const author = await client.call('tasks.create', task('author'))
    expect(await client.call('tasks.run', { task: author.id })).toMatchObject({ state: 'finished', steps: 3, result: 'change set submitted' })
    const [changeset] = (await client.call('changesets.list', { task: author.id })).changesets
    expect(changeset).toMatchObject({ state: 'submitted', goal: 'FR-RTL-01', author: author.agent, files: [{ path: 'generators/rmsnorm.py' }] })

    const reviewer = await client.call('tasks.create', task('reviewer', { role: 'reviewer', title: 'Verify the RMSNorm generator', runs: ['stub-tool/report'], writeScope: [] }))
    expect(reviewer.agent).not.toBe(author.agent)
    await client.call('tasks.run', { task: reviewer.id, maxSteps: 3 })
    const [verdict] = (await client.call('verdicts.list', { check: 'stub.drc' })).verdicts
    expect(verdict).toMatchObject({ verdict: 'pass', subjects: [changeset!.files[0]!.hash] })

    // Every step, tool call and result is in the event log, and the run's lineage reaches the change set file.
    const steps = (await client.call('events.list', { kind: 'agent.step', limit: 100 })).events
    expect(steps.map((e) => [e.actor.id, (e.payload.toolCalls as { name: string }[]).map((c) => c.name)])).toEqual([
      [author.agent, ['write_file']],
      [author.agent, ['submit_changeset']],
      [author.agent, ['finish']],
      [reviewer.agent, ['list_artifacts']],
      [reviewer.agent, ['start_run']],
      [reviewer.agent, ['evaluate_check']],
    ])
    expect(steps.every((e) => (e.payload.results as { ok: boolean }[]).every((r) => r.ok))).toBe(true)
    const [run] = (await client.call('runs.list')).runs
    const lineage = await client.call('artifacts.lineage', { hash: run!.outputs[0]!.hash })
    expect(lineage.edges).toEqual([{ from: changeset!.files[0]!.hash, to: run!.outputs[0]!.hash, run: run!.id }])
  })

  it('enforces the order of the iteration loop on change sets', async () => {
    const write = { name: 'write_file', arguments: { path: 'generators/a.py', content: 'a' } }
    const first = await setup([scripted('author', 'cloud', [{ toolCalls: [write, { name: 'submit_changeset' }] }, finish()])])
    const author = await first.client.call('tasks.create', task('author'))
    await first.client.call('tasks.run', { task: author.id })
    const [cs] = (await first.client.call('changesets.list', { task: author.id })).changesets
    const home = first.kernel.layout.root
    await first.client.close()
    await first.kernel.stop()

    // Restart on the same home with scripts that know the change set id.
    const { client } = await setup(
      [
        scripted('executor', 'cloud', [call('review_changeset', { changeset: cs!.id, approved: true })]),
        scripted('reviewer', 'cloud', [
          call('merge_changeset', { changeset: cs!.id }),
          call('review_changeset', { changeset: cs!.id, approved: true, comment: 'ok' }),
          call('merge_changeset', { changeset: cs!.id }),
          call('verify_changeset', { changeset: cs!.id, verdict: 'no-such-verdict' }),
        ]),
      ],
      home,
    )
    const executor = await client.call('tasks.create', task('executor'))
    await client.call('tasks.run', { task: executor.id, maxSteps: 1 })
    expect(await incidentCodes(client, 'permission_denied')).toEqual(['permission_denied:REVIEW_REQUIRES_REVIEWER'])

    const reviewer = await client.call('tasks.create', task('reviewer', { role: 'reviewer' }))
    await client.call('tasks.run', { task: reviewer.id, maxSteps: 4 })
    const results = (await client.call('events.list', { kind: 'agent.step', limit: 100 })).events
      .filter((e) => e.actor.id === reviewer.agent)
      .map((e) => (e.payload.results as { ok: boolean; content: string }[])[0]!)
    expect(results.map((r) => r.ok)).toEqual([false, true, false, false])
    expect(results[0]!.content).toMatch(/only verified change sets merge/)
    expect(results[2]!.content).toMatch(/only verified change sets merge/)
    expect(results[3]!.content).toMatch(/needs a passing verdict/)
    expect((await client.call('changesets.list', { task: author.id })).changesets[0]).toMatchObject({
      state: 'reviewed',
      review: { agent: reviewer.agent, approved: true },
    })
  })
})

describe('policy and permissions (AC-M1-07)', () => {
  it('blocks out-of-scope writes, deletions, shell commands and ungranted runs, recording incidents', async () => {
    const { client } = await setup([
      scripted('rogue', 'cloud', [
        {
          toolCalls: [
            { name: 'write_file', arguments: { path: 'kernel/escape.ts', content: 'x' } },
            { name: 'write_file', arguments: { path: 'generators/../../etc/passwd', content: 'x' } },
            { name: 'delete_artifact', arguments: { hash: '0'.repeat(64) } },
            { name: 'run_command', arguments: { command: 'rm -rf ~' } },
            { name: 'start_run', arguments: { plugin: 'stub-tool', handler: 'crash' } },
          ],
        },
        finish(),
      ]),
    ])
    const rogue = await client.call('tasks.create', task('rogue'))
    expect((await client.call('tasks.run', { task: rogue.id })).state).toBe('finished')
    expect(await incidentCodes(client)).toEqual([
      'permission_denied:AGENT_CANNOT_DELETE',
      'permission_denied:NO_SHELL_FOR_AGENTS',
      'permission_denied:RUN_NOT_GRANTED',
      'scope_violation:WRITE_OUTSIDE_SCOPE',
      'scope_violation:WRITE_OUTSIDE_SCOPE',
    ])
    expect((await client.call('artifacts.list', { type: 'changeset.file' })).artifacts).toEqual([])
    expect((await client.call('runs.list')).runs).toEqual([])
    expect((await client.call('kernel.info')).name).toBe('harnessd')
  })

  it('requires a goal, and pauses an agent that repeats the same failing call', async () => {
    const loop = call('write_file', { path: 'outside.txt', content: 'x' })
    const { client } = await setup([scripted('stuck', 'cloud', [loop, loop, loop, loop, loop])])
    await expect(client.call('tasks.create', { ...task('stuck'), goal: 'improve things' })).rejects.toMatchObject({ code: 'INVALID_PARAMS' })
    const t = await client.call('tasks.create', task('stuck', { loopThreshold: 3 }))
    expect(await client.call('tasks.run', { task: t.id })).toMatchObject({ state: 'paused', steps: 3 })
    expect(await incidentCodes(client, 'loop_detected')).toEqual(['loop_detected:AGENT_LOOP_DETECTED'])
  })

  it('stops agents at the emergency stop and at their budget', async () => {
    const { client } = await setup([scripted('busy', 'cloud', Array.from({ length: 10 }, () => call('list_artifacts')))])
    const limited = await client.call('tasks.create', task('busy', { maxSteps: 2 }))
    expect(await client.call('tasks.run', { task: limited.id })).toMatchObject({ state: 'paused', steps: 2, error: 'budget exceeded' })
    await client.call('kernel.pause', { reason: 'test' })
    const t = await client.call('tasks.create', task('busy'))
    expect(await client.call('tasks.run', { task: t.id })).toMatchObject({ state: 'paused', steps: 0 })
  })

  it('limits external clients to reading and starting work', async () => {
    const { kernel, client } = await setup([])
    const runtime = readRuntimeInfo(kernel.layout.root)!
    const external = await KernelClient.connect({ url: runtime.url, token: runtime.externalToken })
    try {
      expect((await external.call('kernel.info')).name).toBe('harnessd')
      expect((await external.call('projects.list')).projects).toHaveLength(1)
      for (const [method, params] of [
        ['approvals.decide', { project: 'chip', gate: 'GA', decision: 'approved' }],
        ['secrets.list', {}],
        ['kernel.shutdown', {}],
        ['declass.setRule', { id: 'r', sourceType: 't', fields: ['/a'], targetLabel: 'public' }],
        ['checks.override', { project: 'chip', check: 'stub.drc', override: { criteria: [{ metric: 'violations', op: '<=', value: 9 }] }, reason: 'x' }],
      ] as const) {
        await expect((external.call as (m: string, p: unknown) => Promise<unknown>)(method, params)).rejects.toMatchObject({ code: 'PERMISSION_DENIED' })
      }
      await expect(external.upload('x', { type: 't' })).rejects.toMatchObject({ code: 'PERMISSION_DENIED' })
      expect((await incidentCodes(client, 'permission_denied')).length).toBe(6)
      expect((await client.call('kernel.info')).name).toBe('harnessd')
    } finally {
      await external.close()
    }
  })
})

describe('egress interception with a fake PDK (AC-M1-08)', () => {
  it('blocks NDA tool results from reaching a cloud model, and allows a local one', async () => {
    const script = [call('read_artifact', { hash: PDK_HASH }), finish()]
    const { client } = await setup([scripted('cloud', 'cloud', script), scripted('local', 'local', script)])
    expect((await client.upload(FAKE_PDK, { type: 'pdk.rules', label: 'nda' })).hash).toBe(PDK_HASH)

    const cloud = await client.call('tasks.create', task('cloud'))
    const paused = await client.call('tasks.run', { task: cloud.id })
    expect(paused).toMatchObject({ state: 'paused', steps: 1 })
    expect(paused.error).toMatch(/outbound request blocked/)
    const [blocked] = (await egress(client)).filter((e) => e.payload.decision === 'blocked')
    expect(blocked!.payload).toMatchObject({ destination: { id: 'llm:cloud', location: 'cloud' }, decision: 'blocked' })
    expect((blocked!.payload.reasons as { code: string }[]).map((r) => r.code)).toEqual(expect.arrayContaining(['LABEL_EXCEEDS_DESTINATION', 'NDA_FINGERPRINT']))
    expect(await incidentCodes(client, 'egress_blocked')).toEqual(['egress_blocked:EGRESS_BLOCKED'])

    const local = await client.call('tasks.create', task('local'))
    expect((await client.call('tasks.run', { task: local.id })).state).toBe('finished')
    await expectNoNdaToCloud(client)
  })

  it('catches leaked NDA text by fingerprint even when it is mislabelled', async () => {
    const leak = `tool log: ${FAKE_PDK.split('\n')[4]}`
    const { client } = await setup([scripted('cloud', 'cloud', [call('read_artifact', { hash: sha256(leak) }), finish()])])
    await client.upload(FAKE_PDK, { type: 'pdk.rules', label: 'nda' })
    // A log line copied out of an NDA file, wrongly labelled internal.
    await client.upload(leak, { type: 'run.log', label: 'internal' })
    const t = await client.call('tasks.create', task('cloud'))
    expect((await client.call('tasks.run', { task: t.id })).state).toBe('paused')
    const [blocked] = (await egress(client)).filter((e) => e.payload.decision === 'blocked')
    expect(blocked!.payload.reasons).toEqual([{ code: 'NDA_FINGERPRINT', params: { destination: 'llm:cloud', matches: ['FPDK_M1_W_0p23', 'fpdkMET1_drawing'] } }])
  })

  it('blocks error messages and agent output that echo NDA identifiers', async () => {
    const { client } = await setup([scripted('cloud', 'cloud', [call('evaluate_check', { check: 'FPDK_M1_S_0p28', run: 'x' }), finish()])])
    await client.upload(FAKE_PDK, { type: 'pdk.rules', label: 'nda' })
    const t = await client.call('tasks.create', task('cloud'))
    expect((await client.call('tasks.run', { task: t.id })).state).toBe('paused')
    const blocked = (await egress(client)).filter((e) => e.payload.decision === 'blocked')
    expect(blocked).toHaveLength(1)
    expect(JSON.stringify(blocked[0]!.payload.reasons)).toContain('FPDK_M1_S_0p28')
  })

  it('protects handoff packs and MCP responses', async () => {
    const { kernel, client } = await setup([scripted('local', 'local', [call('read_artifact', { hash: PDK_HASH }), finish()]), scripted('cloud', 'cloud', [finish()])])
    const pdk = await client.upload(FAKE_PDK, { type: 'pdk.rules', label: 'nda' })
    const t = await client.call('tasks.create', task('local'))
    await client.call('tasks.run', { task: t.id, maxSteps: 1 })

    // Handoff path: the pack now carries NDA content, so a cloud model may not take over.
    const refused = await client.call('tasks.switch', { task: t.id, provider: 'cloud' })
    expect(refused).toMatchObject({ switched: false, handoffLabel: 'nda', reasons: [{ code: 'HANDOFF_LABEL_EXCEEDS_PROVIDER' }] })
    expect(refused.options.join(' ')).toMatch(/local[\s\S]*declassification/)

    // MCP path: an external client cannot download NDA content or see NDA event payloads.
    const runtime = readRuntimeInfo(kernel.layout.root)!
    const external = await KernelClient.connect({ url: runtime.url, token: runtime.externalToken })
    try {
      await expect(external.download(pdk.hash)).rejects.toMatchObject({ code: 'EGRESS_BLOCKED' })
      const { events } = await external.call('events.list', { kind: 'agent.step' })
      expect(events[0]!.label).toBe('nda')
      expect(events[0]!.payload).toEqual({ redacted: 'nda' })
      expect(JSON.stringify(events)).not.toContain('fpdkMET1')
    } finally {
      await external.close()
    }
    await expectNoNdaToCloud(client)
  })

  it('releases only the structured fields a human-defined declassification rule names', async () => {
    const { client } = await setup([])
    const report = await client.upload(JSON.stringify({ summary: { violations: 0, area: 1.92 }, text: 'see fpdkMET1_drawing' }), { type: 'signoff.report', label: 'nda' })
    await expect(client.call('declass.apply', { rule: 'signoff-counts', hash: report.hash })).rejects.toMatchObject({ code: 'DECLASS_RULE_NOT_FOUND' })
    await client.call('declass.setRule', { id: 'signoff-counts', sourceType: 'signoff.report', fields: ['/summary/violations', '/summary/area'], targetLabel: 'internal' })
    const declassified = await client.call('declass.apply', { rule: 'signoff-counts', hash: report.hash })
    expect(declassified).toMatchObject({ label: 'internal', declassifiedBy: 'signoff-counts', inputs: [report.hash] })
    expect(JSON.parse(await (await client.download(declassified.hash)).text()).fields).toEqual({ '/summary/violations': 0, '/summary/area': 1.92 })
    await client.call('declass.setRule', { id: 'signoff-text', sourceType: 'signoff.report', fields: ['/text'], targetLabel: 'internal' })
    await expect(client.call('declass.apply', { rule: 'signoff-text', hash: report.hash })).rejects.toMatchObject({ code: 'DECLASS_FIELD_NOT_SCALAR' })
  })
})

describe('handing a task to another model (AC-M1-09)', () => {
  it('(a) continues the task with another provider when it may receive the handoff pack', async () => {
    const { client } = await setup([
      scripted('cloud-a', 'cloud', [call('write_file', { path: 'generators/softmax.py', content: 'def softmax(x): ...' })]),
      scripted('cloud-b', 'cloud', [call('submit_changeset', { summary: 'softmax' }), finish('submitted by the second model')]),
    ])
    const t = await client.call('tasks.create', task('cloud-a'))
    expect(await client.call('tasks.run', { task: t.id, maxSteps: 1 })).toMatchObject({ state: 'paused', steps: 1 })
    const switched = await client.call('tasks.switch', { task: t.id, provider: 'cloud-b' })
    expect(switched).toMatchObject({ switched: true, handoffLabel: 'internal', task: { spec: { provider: 'cloud-b' } } })
    expect(switched.task.agent).not.toBe(t.agent)
    const pack = JSON.parse(await (await client.download(switched.handoff)).text())
    expect(pack.recentSteps[0]).toMatch(/write_file/)

    const done = await client.call('tasks.run', { task: t.id })
    expect(done).toMatchObject({ state: 'finished', result: 'submitted by the second model' })
    // The second model's change set contains the file the first model wrote.
    expect((await client.call('changesets.list', { task: t.id })).changesets[0]!.files.map((f) => f.path)).toEqual(['generators/softmax.py'])
  })

  it('(b) refuses a cloud provider once the task has seen NDA data, but allows a local one', async () => {
    const { client } = await setup([
      scripted('local-a', 'local', [call('read_artifact', { hash: PDK_HASH })]),
      scripted('local-b', 'local', [finish('continued locally')]),
      scripted('cloud', 'cloud', [finish()]),
    ])
    await client.upload(FAKE_PDK, { type: 'pdk.rules', label: 'nda' })
    const t = await client.call('tasks.create', task('local-a'))
    await client.call('tasks.run', { task: t.id, maxSteps: 1 })
    expect(await client.call('tasks.switch', { task: t.id, provider: 'cloud' })).toMatchObject({ switched: false })
    expect((await client.call('tasks.get', { task: t.id })).spec.provider).toBe('local-a')
    expect(await client.call('tasks.switch', { task: t.id, provider: 'local-b' })).toMatchObject({ switched: true })
    expect(await client.call('tasks.run', { task: t.id })).toMatchObject({ state: 'finished', result: 'continued locally' })
    await expectNoNdaToCloud(client)
  })
})

describe('lowering a standard to pass (AC-M1-13)', () => {
  it("turns an agent's change of a check standard into a human decision and flags the resulting pass", async () => {
    const lowered = { criteria: [{ metric: 'violations', op: '<=', value: 5 }] }
    const { client } = await setup([scripted('agent', 'cloud', [call('propose_check_override', { check: 'stub.drc', override: lowered, reason: 'too strict' }), finish()])])
    const design = (await client.upload('design', { type: 'test.design' })).hash
    const started = await client.call('runs.start', { plugin: 'stub-tool', handler: 'report', params: { violations: 2 }, inputs: [{ name: 'design', hash: design }] })
    const run = await client.call('runs.wait', { run: started.id })
    expect(await client.call('verdicts.evaluate', { check: 'stub.drc', run: run.id, project: 'chip' })).toMatchObject({ verdict: 'fail', checkVersion: '1' })

    const t = await client.call('tasks.create', task('agent'))
    await client.call('tasks.run', { task: t.id })
    const [proposal] = (await client.call('constraints.list', { project: 'chip' })).proposals
    expect(proposal).toMatchObject({ status: 'pending', check: 'stub.drc', by: { kind: 'agent' } })
    expect(await incidentCodes(client, 'hard_constraint_change')).toEqual(['hard_constraint_change:HARD_CONSTRAINT_CHANGE_REQUESTED'])
    // Not applied while pending.
    expect((await client.call('verdicts.evaluate', { check: 'stub.drc', run: run.id, project: 'chip' })).verdict).toBe('fail')

    await client.call('constraints.decide', { proposal: proposal!.id, decision: 'approved', comment: 'accepted for the prerun' })
    const after = await client.call('verdicts.evaluate', { check: 'stub.drc', run: run.id, project: 'chip' })
    expect(after.verdict).toBe('pass')
    expect(after.checkVersion).toMatch(/^1\+[0-9a-f]{12}$/)
    expect(await incidentCodes(client, 'standard_lowered')).toEqual(['standard_lowered:CHECK_PASSES_AFTER_STANDARD_CHANGE'])
  })
})
