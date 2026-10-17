// K4 scheduler and process supervision (architecture §4.4) through the kernel
// API, with the stub tool plugin.

import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { afterEach, describe, expect, it } from 'vitest'
import { KernelClient } from '@llmchip/client'
import { EventStore } from '@llmchip/kernel'
import type { RunRecord } from '@llmchip/kernel-api'
import { connect, FIXTURE_PLUGINS, startKernel, tempDir } from './helpers.ts'

const plugins = { searchPaths: [FIXTURE_PLUGINS] }

async function setup(scheduler: { maxParallel?: number; cancelGraceMs?: number } = {}) {
  const { kernel, home } = await startKernel(undefined, { config: { plugins, scheduler: { cancelGraceMs: 300, ...scheduler } } })
  const client = await connect(kernel)
  const design = await client.upload('module top; endmodule', { type: 'test.design', label: 'internal' })
  return { kernel, home, client, design }
}

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

describe('scheduler', () => {
  it('runs a handler and registers its outputs with lineage, labels and a log', async () => {
    const { client, design } = await setup()
    const nda = await client.upload('pdk rules', { type: 'test.design', label: 'nda' })
    const queued = await client.call('runs.start', { plugin: 'stub-tool', handler: 'report', inputs: [{ name: 'design', hash: nda.hash }] })
    expect(queued).toMatchObject({ state: 'queued', label: 'nda', params: { violations: 0, truncate: false, coverage: true } })
    const run = await client.call('runs.wait', { run: queued.id })
    expect(run).toMatchObject({ state: 'finished', execution: 'exited_zero', exitCode: 0, metrics: { violations: 0 }, parse: { status: 'complete' } })

    const report = await client.call('artifacts.stat', { hash: run.outputs[0]!.hash })
    expect(report).toMatchObject({ type: 'test.report', label: 'nda', inputs: [nda.hash], run: run.id, creator: { kind: 'plugin', id: 'stub-tool' } })
    expect(await (await client.download(report.hash)).text()).toContain(`design ${nda.hash}`)
    const log = await (await client.download(run.log!)).text()
    expect(log).toContain('report written')
    expect(log).toContain('run finished: exited_zero')
    expect(design.label).toBe('internal')
  })

  it('validates params and inputs before queueing', async () => {
    const { client, design } = await setup()
    const start = (params: object) => client.call('runs.start', { plugin: 'stub-tool', handler: 'report', ...params })
    await expect(start({ inputs: [] })).rejects.toMatchObject({ code: 'RUN_INPUT_MISSING' })
    await expect(start({ inputs: [{ name: 'design', hash: design.hash }], params: { violations: -1 } })).rejects.toMatchObject({ code: 'RUN_PARAMS_INVALID' })
    await expect(start({ inputs: [{ name: 'other', hash: design.hash }] })).rejects.toMatchObject({ code: 'RUN_INPUT_UNKNOWN' })
    const log = await client.upload('log', { type: 'run.log' })
    await expect(start({ inputs: [{ name: 'design', hash: log.hash }] })).rejects.toMatchObject({ code: 'RUN_INPUT_TYPE' })
    await expect(client.call('runs.start', { plugin: 'nope', handler: 'x' })).rejects.toMatchObject({ code: 'PLUGIN_NOT_FOUND' })
  })

  it('records crashes, errors and invalid outputs without affecting the kernel', async () => {
    const { client } = await setup()
    const run = async (handler: string, extra: object = {}) =>
      client.call('runs.wait', { run: (await client.call('runs.start', { plugin: 'stub-tool', handler, ...extra })).id })

    expect(await run('crash')).toMatchObject({ execution: 'crashed', exitCode: 3, outputs: [] })
    expect(await run('fail')).toMatchObject({ execution: 'exited_nonzero', error: 'the tool reported an error' })
    const escaped = await run('escape')
    expect(escaped).toMatchObject({ execution: 'exited_nonzero', outputs: [] })
    expect(escaped.error).toMatch(/not a file in the run's out directory/)

    const retried = await run('crash', { maxAttempts: 2 })
    expect(retried.attempt).toBe(1)
    await expect.poll(async () => (await client.call('runs.list', { limit: 1000 })).runs.filter((r) => r.retryOf === retried.id)).toHaveLength(1)
    expect((await client.call('kernel.info')).name).toBe('harnessd')
  })

  it('times out and cancels runs, killing their process groups', async () => {
    const { kernel, client } = await setup()
    const hang = await client.call('runs.start', { plugin: 'stub-tool', handler: 'hang', timeoutMs: 300 })
    const timedOut = await client.call('runs.wait', { run: hang.id })
    expect(timedOut.execution).toBe('timed_out')

    const long = await client.call('runs.start', { plugin: 'stub-tool', handler: 'sleep', params: { ms: 30_000 } })
    await expect.poll(async () => (await client.call('runs.get', { run: long.id })).state).toBe('running')
    const pid = startedPid(kernel.events, long.id)
    expect(alive(pid)).toBe(true)
    await client.call('runs.cancel', { run: long.id })
    expect(await client.call('runs.wait', { run: long.id })).toMatchObject({ execution: 'cancelled' })
    expect(alive(pid)).toBe(false)
    expect(alive(startedPid(kernel.events, hang.id))).toBe(false)
  })

  it('holds new runs while paused and resumes them', async () => {
    const { client } = await setup()
    await client.call('kernel.pause', { reason: 'emergency stop' })
    expect((await client.call('kernel.info')).paused).toBe(true)
    const queued = await client.call('runs.start', { plugin: 'stub-tool', handler: 'sleep', params: { ms: 10 } })
    await sleep(300)
    expect((await client.call('runs.get', { run: queued.id })).state).toBe('queued')
    await client.call('kernel.resume')
    expect(await client.call('runs.wait', { run: queued.id })).toMatchObject({ execution: 'exited_zero', metrics: { slept: 10 } })
  })
})

function startedPid(events: EventStore, run: string): number {
  return events.list({ kind: 'run.started', limit: 1000 }).find((e) => e.payload.run === run)!.payload.pid as number
}

// AC-M1-04: the kernel process is killed while a run is in progress.
const CLI = resolve(import.meta.dirname, '../packages/kernel/src/cli.ts')
const children = new Set<ChildProcess>()
afterEach(() => {
  for (const child of children) child.kill('SIGKILL')
  children.clear()
})

async function spawnKernel(home: string): Promise<{ child: ChildProcess; client: KernelClient }> {
  const child = spawn(process.execPath, ['--conditions=development', CLI, 'serve', '--home', home, '--plugins', FIXTURE_PLUGINS], { stdio: 'ignore' })
  children.add(child)
  const runtime = join(home, 'run', 'harnessd.json')
  for (let i = 0; i < 200; i++) {
    if (existsSync(runtime)) {
      const info = JSON.parse(readFileSync(runtime, 'utf8'))
      if (info.pid === child.pid) return { child, client: await KernelClient.connect(info) }
    }
    await sleep(50)
  }
  throw new Error('harnessd did not start')
}

describe('crash recovery of runs (AC-M1-04)', () => {
  it('kills orphaned plugin processes and retries interrupted runs', { timeout: 60_000 }, async () => {
    const home = tempDir()
    const first = await spawnKernel(home)
    const run = await first.client.call('runs.start', { plugin: 'stub-tool', handler: 'sleep', params: { ms: 2000 }, maxAttempts: 2 })
    await expect.poll(async () => (await first.client.call('runs.get', { run: run.id })).state).toBe('running')
    const events = new EventStore(join(home, 'db', 'harness.sqlite'))
    const pid = startedPid(events, run.id)
    events.close()

    first.child.kill('SIGKILL')
    await new Promise((r) => first.child.once('exit', r))
    expect(alive(pid)).toBe(true) // The plugin outlived its kernel.

    const second = await spawnKernel(home)
    expect(alive(pid)).toBe(false)
    const interrupted = await second.client.call('runs.get', { run: run.id })
    expect(interrupted).toMatchObject({ state: 'finished', execution: 'interrupted' })
    let retry: RunRecord | undefined
    await expect
      .poll(async () => (retry = (await second.client.call('runs.list', { limit: 1000 })).runs.find((r) => r.retryOf === run.id)))
      .toBeDefined()
    expect(await second.client.call('runs.wait', { run: retry!.id })).toMatchObject({ attempt: 2, execution: 'exited_zero', metrics: { slept: 2000 } })
    expect(await second.client.call('events.verify')).toMatchObject({ ok: true })
    await second.client.call('kernel.shutdown')
  })
})
