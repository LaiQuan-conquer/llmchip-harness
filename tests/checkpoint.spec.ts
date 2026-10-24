// Run checkpoints (IF-2 `checkpoint`, requirements FR-TRC-10): a checkpoint is stored as an
// artifact while the run is still going, even if the run then dies; a retry gets the latest
// one as the input `resume` and continues from it.

import { describe, expect, it } from 'vitest'
import { connect, FIXTURE_PLUGINS, startKernel } from './helpers.ts'

async function setup() {
  const { kernel } = await startKernel(undefined, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS] } } })
  return { kernel, client: await connect(kernel) }
}

describe('run checkpoints', () => {
  it('stores each checkpoint at once, as an artifact of the run', async () => {
    const { client } = await setup()
    const run = await client.call('runs.start', { plugin: 'stub-tool', handler: 'steps', params: { steps: 3, stepMs: 50 } })
    const done = await client.call('runs.wait', { run: run.id, timeoutMs: 30_000 })
    expect(done).toMatchObject({ execution: 'exited_zero', metrics: { steps: 3, resumed_from: 0 } })
    const { events } = await client.call('events.list', { kind: 'run.checkpointed' })
    expect(events.map((e) => (e.payload as { run: string }).run)).toEqual([run.id, run.id, run.id])
    const latest = (events.at(-1)!.payload as { artifact: string }).artifact
    expect(done.checkpoint).toBe(latest)
    expect(await client.call('artifacts.stat', { hash: latest })).toMatchObject({ type: 'run.checkpoint', run: run.id })
    expect(await (await client.download(latest)).text()).toBe('3')
  })

  it('keeps the checkpoint of a run that died, and the retry resumes from it', async () => {
    const { client } = await setup()
    const run = await client.call('runs.start', { plugin: 'stub-tool', handler: 'steps', params: { steps: 6, stepMs: 50, crashAt: 4 }, maxAttempts: 2 })
    const first = await client.call('runs.wait', { run: run.id, timeoutMs: 30_000 })
    expect(first.execution).toBe('crashed')
    expect(await (await client.download(first.checkpoint!)).text()).toBe('4')
    const retry = (await client.call('runs.list', { limit: 100 })).runs.find((r) => r.retryOf === run.id)!
    expect(retry.inputs).toEqual([{ name: 'resume', hash: first.checkpoint }])
    const second = await client.call('runs.wait', { run: retry.id, timeoutMs: 30_000 })
    expect(second).toMatchObject({ execution: 'exited_zero', attempt: 2, metrics: { steps: 6, resumed_from: 4 } })
  })

  it('refuses a checkpoint outside the work directory of the run', async () => {
    const { client } = await setup()
    const run = await client.call('runs.start', { plugin: 'stub-tool', handler: 'steps', params: { steps: 2, stepMs: 0, badCheckpoint: true } })
    const done = await client.call('runs.wait', { run: run.id, timeoutMs: 30_000 })
    expect(done).toMatchObject({ execution: 'exited_zero', checkpoint: null })
    expect((await client.call('events.list', { kind: 'run.checkpointed' })).events).toEqual([])
    expect(await (await client.download(done.log!)).text()).toMatch(/checkpoint not stored: checkpoint steps \(\.\.\/\.\.\/outside\.txt\) is not a file in the run's work directory/)
  })
})
