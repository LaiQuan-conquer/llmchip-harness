// Resource-aware scheduling (requirements NFR-15): runs start in queue order while
// they fit the host's capacity (CPU, memory, GPUs), a run larger than the whole
// capacity runs alone, and only a run that asked for a GPU sees one.

import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'
import { connect, FIXTURE_PLUGINS, startKernel } from './helpers.ts'

async function setup() {
  const { kernel } = await startKernel(undefined, {
    config: { plugins: { searchPaths: [FIXTURE_PLUGINS] }, scheduler: { maxParallel: 8, resources: { cpu: 8, memGb: 4, gpu: 1 } } },
  })
  return { kernel, client: await connect(kernel) }
}

const start = (client: KernelClient, handler: string, ms = 400) => client.call('runs.start', { plugin: 'stub-tool', handler, params: { ms } })
const finished = (client: KernelClient, run: RunRecord) => client.call('runs.wait', { run: run.id, timeoutMs: 30_000 })
/** Whether two finished runs were active at the same time. */
const overlapped = (a: RunRecord, b: RunRecord) => a.startedAt! < b.endedAt! && b.startedAt! < a.endedAt!

describe('resource-aware scheduling', () => {
  it('reports the capacity it schedules against', async () => {
    const { kernel } = await setup()
    expect(kernel.scheduler.capacity).toEqual({ cpu: 8, memGb: 4, gpu: 1 })
  })

  it('holds a run that does not fit until memory is free, in queue order', async () => {
    const { client } = await setup()
    // Two runs of 3 GiB do not fit in 4 GiB together; the small run behind them waits its turn.
    const [a, b, small] = [await start(client, 'heavy'), await start(client, 'heavy'), await start(client, 'sleep', 50)]
    const [ra, rb, rs] = [await finished(client, a), await finished(client, b), await finished(client, small)]
    for (const r of [ra, rb, rs]) expect(r.execution).toBe('exited_zero')
    expect(overlapped(ra, rb)).toBe(false)
    expect(rs.startedAt! >= rb.startedAt!).toBe(true)
  })

  it('runs small runs side by side', async () => {
    const { client } = await setup()
    const runs = [await start(client, 'sleep', 600), await start(client, 'sleep', 600)]
    const [r1, r2] = [await finished(client, runs[0]!), await finished(client, runs[1]!)]
    expect(overlapped(r1, r2)).toBe(true)
  })

  it('gives the one GPU to one run at a time, and hides it from the others', async () => {
    const { client } = await setup()
    const [g1, g2, cpu] = [await start(client, 'gpu'), await start(client, 'gpu'), await start(client, 'heavy', 100)]
    const [r1, r2, rc] = [await finished(client, g1), await finished(client, g2), await finished(client, cpu)]
    expect(overlapped(r1, r2)).toBe(false)
    expect(r1.metrics.cuda_visible_devices).toBe('0')
    expect(rc.metrics.cuda_visible_devices).toBe('')
  })

  it('lets one run at a time use the GPUs, even with more than one', async () => {
    const { kernel } = await startKernel(undefined, {
      config: { plugins: { searchPaths: [FIXTURE_PLUGINS] }, scheduler: { maxParallel: 8, resources: { cpu: 8, memGb: 8, gpu: 2 } } },
    })
    const client = await connect(kernel)
    const [g1, g2] = [await start(client, 'gpu'), await start(client, 'gpu')]
    const [r1, r2] = [await finished(client, g1), await finished(client, g2)]
    expect(overlapped(r1, r2)).toBe(false)
    expect([r1.metrics.cuda_visible_devices, r2.metrics.cuda_visible_devices]).toEqual(['0', '0'])
  })

  it('runs a run larger than the whole capacity alone instead of never', async () => {
    const { client } = await setup()
    const [huge, after] = [await start(client, 'huge', 200), await start(client, 'sleep', 50)]
    const [rh, ra] = [await finished(client, huge), await finished(client, after)]
    expect(rh.execution).toBe('exited_zero')
    expect(overlapped(rh, ra)).toBe(false)
  })
})
