// Stage pipelines (requirements FR-PRJ-07/08, architecture K3): the two entries of a stage,
// its development pipeline and its required tests, run by the kernel from the flow pack, with
// the test flow pack and the stub tool. A failed node stays failed and its dependents are
// skipped until the pipeline is resumed; the tests reuse a finished run with the same inputs;
// a pipeline survives a SIGKILL of the kernel and its interrupted node resumes from its
// checkpoint.

import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { afterEach, describe, expect, it } from 'vitest'
import { KernelClient } from '@llmchip/client'
import type { PipelineRecord } from '@llmchip/kernel-api'
import { connect, FIXTURE_PLUGINS, startKernel, tempDir } from './helpers.ts'

async function setup() {
  const { kernel } = await startKernel(undefined, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS] } } })
  const client = await connect(kernel)
  await client.call('projects.create', { id: 'chip', title: 'Test chip', flow: 'test' })
  return { kernel, client }
}

async function design(client: KernelClient, text = 'design v1') {
  const hash = (await client.upload(text, { type: 'test.design' })).hash
  await client.call('projects.bind', { project: 'chip', slot: 'design', hash })
  return hash
}

/** Waits until the pipeline is no longer running. */
async function finished(client: KernelClient, id: string, timeoutMs = 60_000): Promise<PipelineRecord> {
  for (const end = Date.now() + timeoutMs; Date.now() < end; await sleep(50)) {
    const record = await client.call('pipelines.get', { pipeline: id })
    if (record.state !== 'running') return record
  }
  throw new Error(`pipeline ${id} still running after ${timeoutMs} ms`)
}

const node = (record: PipelineRecord, id: string) => record.nodes.find((n) => n.id === id)!

describe('stage pipelines', () => {
  it('plans a pipeline and refuses to start one with missing inputs or nothing to run', async () => {
    const { client } = await setup()
    expect(await client.call('pipelines.plan', { project: 'chip', stage: 'A', kind: 'develop' })).toMatchObject({
      stages: ['A'],
      ready: false,
      missing: [
        { node: 'A/drc', ref: 'slot:design' },
        { node: 'A/flaky', ref: 'slot:design' },
      ],
    })
    await expect(client.call('pipelines.start', { project: 'chip', stage: 'A', kind: 'develop' })).rejects.toMatchObject({ code: 'PIPELINE_INPUTS_MISSING' })
    await design(client)
    expect(await client.call('pipelines.plan', { project: 'chip', stage: 'A', kind: 'develop' })).toMatchObject({ ready: true, missing: [] })
    // Stage C's test needs a slot nothing binds.
    expect((await client.call('pipelines.plan', { project: 'chip', stage: 'C', kind: 'test' })).missing).toEqual([{ node: 'C/stub.drc', ref: 'slot:package' }])
    await expect(client.call('pipelines.start', { project: 'chip', stage: 'B', kind: 'test' })).rejects.toMatchObject({ code: 'PIPELINE_EMPTY' })
    // Over all stages, a slot an earlier node binds counts as available.
    expect(await client.call('pipelines.plan', { project: 'chip', stage: 'all', kind: 'develop' })).toMatchObject({ stages: ['A', 'B', 'C'], ready: true })
  })

  it('runs the development pipeline: enters the stage, runs, binds outputs, judges checks', async () => {
    const { client } = await setup()
    await design(client)
    const started = await client.call('pipelines.start', { project: 'chip', stage: 'A', kind: 'develop' })
    expect(started.state).toBe('running')
    const done = await finished(client, started.id)
    expect(done.state).toBe('passed')
    expect(done.nodes.map((n) => [n.id, n.state])).toEqual([
      ['A/defaults', 'done'],
      ['A/enter', 'done'],
      ['A/drc', 'done'],
      ['A/flaky', 'done'],
      ['A/wrapup', 'done'],
    ])
    expect(node(done, 'A/drc').verdicts).toEqual([expect.objectContaining({ check: 'stub.drc', verdict: 'pass' })])
    const state = await client.call('projects.get', { project: 'chip' })
    expect(state.stages.find((s) => s.id === 'A')!.entered).toBe(true)
    const drcRun = await client.call('runs.get', { run: node(done, 'A/drc').runs[0]! })
    expect(state.slots['drc-report']).toBe(drcRun.outputs[0]!.hash)
    expect(state.slots['flaky-report']).toBeTruthy()
  })

  it('runs the required tests, reusing a finished run with the same inputs', async () => {
    const { client } = await setup()
    await design(client)
    const develop = await finished(client, (await client.call('pipelines.start', { project: 'chip', stage: 'A', kind: 'develop' })).id)
    const tests = await finished(client, (await client.call('pipelines.start', { project: 'chip', stage: 'A', kind: 'test' })).id)
    expect(tests.state).toBe('passed')
    expect(tests.summary).toMatchObject({ nodes: 2, done: 2, pass: 2, fail: 0, indeterminate: 0 })
    // stub.drc judges the development run again; stub.limit needs other params, so it ran.
    expect(node(tests, 'A/stub.drc')).toMatchObject({ reused: node(develop, 'A/drc').runs[0], runs: [] })
    expect(node(tests, 'A/stub.limit')).toMatchObject({ reused: null, runs: [expect.any(String)] })
  })

  it('keeps a failure failed, skips its dependents, and resumes after the fix', { timeout: 30_000 }, async () => {
    const { client } = await setup()
    await design(client, 'bad design')
    const failed = await finished(client, (await client.call('pipelines.start', { project: 'chip', stage: 'A', kind: 'develop' })).id)
    expect(failed.state).toBe('failed')
    expect(node(failed, 'A/flaky')).toMatchObject({ state: 'failed', reason: { code: 'PIPELINE_RUN_FAILED' } })
    expect(node(failed, 'A/wrapup')).toMatchObject({ state: 'skipped', reason: { code: 'PIPELINE_DEPENDENCY_FAILED', params: { node: 'A/flaky' } } })
    expect(node(failed, 'A/drc').state).toBe('done')
    // Resuming without a fix fails again; with the fix it passes, and done nodes are not rerun.
    expect((await finished(client, (await client.call('pipelines.resume', { pipeline: failed.id })).id)).state).toBe('failed')
    await design(client, 'fixed design')
    const resumed = await finished(client, (await client.call('pipelines.resume', { pipeline: failed.id })).id)
    expect(resumed.state).toBe('passed')
    expect(node(resumed, 'A/drc').runs).toHaveLength(1)
    expect(node(resumed, 'A/flaky').runs).toHaveLength(3)
    await expect(client.call('pipelines.resume', { pipeline: failed.id })).rejects.toMatchObject({ code: 'PIPELINE_PASSED' })
  })

  it('cancels a running pipeline and its runs', async () => {
    const { client } = await setup()
    await design(client)
    const started = await client.call('pipelines.start', { project: 'chip', stage: 'B', kind: 'develop' })
    for (let i = 0; i < 100 && node(await client.call('pipelines.get', { pipeline: started.id }), 'B/steps').state !== 'running'; i++) await sleep(20)
    const cancelled = await client.call('pipelines.cancel', { pipeline: started.id })
    expect(cancelled.state).toBe('cancelled')
    expect(node(cancelled, 'B/steps').state).toBe('cancelled')
    const run = await client.call('runs.wait', { run: node(cancelled, 'B/steps').runs[0]!, timeoutMs: 10_000 })
    expect(run.execution).toBe('cancelled')
    // The stage's default slot was bound from the flow pack's data.
    expect((await client.call('projects.get', { project: 'chip' })).slots.spec).toBeTruthy()
  })
})

describe('stage pipelines: guards', () => {
  it('resumes a cancelled pipeline; the cancelled run does not decide the new attempt', { timeout: 30_000 }, async () => {
    const { client } = await setup()
    await design(client)
    const started = await client.call('pipelines.start', { project: 'chip', stage: 'B', kind: 'develop' })
    for (let i = 0; i < 100 && node(await client.call('pipelines.get', { pipeline: started.id }), 'B/steps').state !== 'running'; i++) await sleep(20)
    await client.call('pipelines.cancel', { pipeline: started.id })
    // Resume at once, while the cancelled run may still be ending.
    const resumed = await finished(client, (await client.call('pipelines.resume', { pipeline: started.id })).id)
    expect(resumed.state).toBe('passed')
    const steps = node(resumed, 'B/steps')
    expect(steps.runs).toHaveLength(2)
    expect((await client.call('runs.get', { run: steps.runs[0]! })).execution).toBe('cancelled')
    expect((await client.call('runs.get', { run: steps.runs[1]! })).execution).toBe('exited_zero')
  })

  it('runs one pipeline at a time on a stage', async () => {
    const { client } = await setup()
    await design(client)
    const first = await client.call('pipelines.start', { project: 'chip', stage: 'B', kind: 'develop' })
    await expect(client.call('pipelines.start', { project: 'chip', stage: 'all', kind: 'develop' })).rejects.toMatchObject({ code: 'PIPELINE_RUNNING' })
    await client.call('pipelines.cancel', { pipeline: first.id })
    // Another stage is free.
    expect((await finished(client, (await client.call('pipelines.start', { project: 'chip', stage: 'A', kind: 'test' })).id)).state).toBe('passed')
  })

  it('binds a hard-constraint slot only while it is empty, never over what a person bound', async () => {
    const { client } = await setup()
    await design(client, 'design v1')
    const first = await finished(client, (await client.call('pipelines.start', { project: 'chip', stage: 'C', kind: 'develop' })).id)
    expect(first.state).toBe('passed')
    const registry = (await client.call('projects.get', { project: 'chip' })).slots.registry
    expect(registry).toBeTruthy()
    await design(client, 'design v2')
    const second = await finished(client, (await client.call('pipelines.start', { project: 'chip', stage: 'C', kind: 'develop' })).id)
    expect(node(second, 'C/registry')).toMatchObject({ state: 'failed', reason: { code: 'PIPELINE_HARD_CONSTRAINT_BOUND', params: { slot: 'registry' } } })
    expect((await client.call('projects.get', { project: 'chip' })).slots.registry).toBe(registry)
  })

  it('refuses a flow pack whose nodes depend on each other in a cycle', async () => {
    const dir = tempDir()
    const plugin = join(dir, 'cycle-flow')
    mkdirSync(plugin)
    writeFileSync(join(plugin, 'llmchip-plugin.json'), JSON.stringify({ id: 'cycle-flow', version: '1.0.0', kinds: ['flow-pack'], runtime: 'data', kernelApi: '^0.3', data: [{ id: 'flow', type: 'flow.pack', path: 'flow.json' }] }))
    const nodes = [
      { id: 'early', plugin: 'stub-tool', handler: 'sleep', params: { ms: 1 }, beforeEntry: true, after: ['late'] },
      { id: 'late', plugin: 'stub-tool', handler: 'sleep', params: { ms: 1 } },
    ]
    writeFileSync(join(plugin, 'flow.json'), JSON.stringify({ id: 'cycle', version: '1', stages: [{ id: 'A', title: 'A', develop: { nodes } }], checks: [] }))
    const { kernel } = await startKernel(undefined, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS, dir] } } })
    const client = await connect(kernel)
    await client.call('projects.create', { id: 'c', title: 'Cycle', flow: 'cycle' })
    await expect(client.call('pipelines.plan', { project: 'c', stage: 'A', kind: 'develop' })).rejects.toMatchObject({ code: 'PIPELINE_INVALID' })
  })

  it('continues after a graceful kernel stop, from the checkpoint of the interrupted node', { timeout: 60_000 }, async () => {
    const config = { plugins: { searchPaths: [FIXTURE_PLUGINS] } }
    const { kernel, home } = await startKernel(undefined, { config })
    let client = await connect(kernel)
    await client.call('projects.create', { id: 'chip', title: 'Test chip', flow: 'test' })
    await design(client)
    const started = await client.call('pipelines.start', { project: 'chip', stage: 'B', kind: 'develop' })
    for (let i = 0; i < 400 && (await client.call('events.list', { kind: 'run.checkpointed' })).events.length < 2; i++) await sleep(25)
    await client.close()
    await kernel.stop('test restart')
    const again = await startKernel(home, { config })
    client = await connect(again.kernel)
    const done = await finished(client, started.id)
    expect(done.state).toBe('passed')
    const steps = node(done, 'B/steps')
    expect(steps.runs).toHaveLength(2)
    expect((await client.call('runs.get', { run: steps.runs[0]! })).execution).toBe('interrupted')
    expect((await client.call('runs.get', { run: steps.runs[1]! })).metrics.resumed_from as number).toBeGreaterThanOrEqual(2)
  })
})

// ---- kernel crash -----------------------------------------------------------

const CLI = resolve(import.meta.dirname, '../packages/kernel/src/cli.ts')
const children = new Set<ChildProcess>()
afterEach(() => {
  for (const child of children) child.kill('SIGKILL')
  children.clear()
})

async function spawnKernel(home: string): Promise<{ child: ChildProcess; client: KernelClient }> {
  const child = spawn(process.execPath, ['--conditions=development', CLI, 'serve', '--home', home], { stdio: ['ignore', 'ignore', 'pipe'] })
  children.add(child)
  let stderr = ''
  child.stderr!.on('data', (chunk) => (stderr += chunk))
  const runtimePath = join(home, 'run', 'harnessd.json')
  for (let i = 0; i < 400; i++) {
    if (child.exitCode !== null) throw new Error(`harnessd exited with ${child.exitCode}: ${stderr}`)
    if (existsSync(runtimePath)) {
      const runtime = JSON.parse(readFileSync(runtimePath, 'utf8'))
      if (runtime.pid === child.pid) return { child, client: await KernelClient.connect(runtime) }
    }
    await sleep(50)
  }
  throw new Error(`harnessd did not start: ${stderr}`)
}

describe('stage pipelines across a kernel crash', () => {
  it('continue after a SIGKILL: the interrupted node resumes from its checkpoint', { timeout: 120_000 }, async () => {
    const home = tempDir()
    mkdirSync(join(home, 'config'), { recursive: true })
    writeFileSync(join(home, 'config', 'config.json'), JSON.stringify({ plugins: { bundled: false, searchPaths: [FIXTURE_PLUGINS] } }))
    let { child, client } = await spawnKernel(home)
    await client.call('projects.create', { id: 'chip', title: 'Test chip', flow: 'test' })
    await design(client)
    const started = await client.call('pipelines.start', { project: 'chip', stage: 'B', kind: 'develop' })
    // Kill the kernel after the run stored at least two of its six checkpoints.
    for (let i = 0; i < 400; i++) {
      if ((await client.call('events.list', { kind: 'run.checkpointed' })).events.length >= 2) break
      await sleep(25)
    }
    const exited = new Promise((r) => child.once('exit', r))
    child.kill('SIGKILL')
    await exited
    children.delete(child)
    await client.close()

    ;({ child, client } = await spawnKernel(home))
    const done = await finished(client, started.id)
    expect(done.state).toBe('passed')
    const steps = node(done, 'B/steps')
    expect(steps.runs).toHaveLength(2)
    const [first, second] = [await client.call('runs.get', { run: steps.runs[0]! }), await client.call('runs.get', { run: steps.runs[1]! })]
    expect(first.execution).toBe('interrupted')
    expect(second.inputs).toContainEqual({ name: 'resume', hash: first.checkpoint })
    expect(second.metrics.resumed_from as number).toBeGreaterThanOrEqual(2)
    expect(second.metrics.steps).toBe(6)
    expect((await client.call('projects.get', { project: 'chip' })).slots['steps-report']).toBe(second.outputs[0]!.hash)
    await client.close()
  })
})
