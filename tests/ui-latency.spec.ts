// AC-M1-11, kernel side: the calls the Web UI makes stay responsive (p95 under
// 200 ms, requirements §10.1) while two EDA tool runs load the machine and an
// agent streams steps. The desktop shell's own startup and memory are measured
// on Windows when the shell exists. Skipped, visibly, without the tool image.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
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

describe.skipIf(!available(IMAGE))('UI responsiveness under load', () => {
  it('answers UI queries within 200 ms at p95 during EDA runs and agent work', { timeout: 600_000 }, async () => {
    const listing = { toolCalls: [{ name: 'list_artifacts' }] }
    const { kernel } = await startKernel(undefined, {
      bundled: true,
      config: { llm: { providers: [{ id: 'busy', kind: 'scripted', location: 'local', script: Array.from({ length: 400 }, () => listing) }] } },
    })
    const client = await connect(kernel)
    await client.call('projects.create', { id: 'load', title: 'Load' })
    const heavy = { implementation: 'gf180mcu.stdcell-rom', sizes: [[16384, 16], [16384, 16], [16384, 16]] }
    const runs = await Promise.all([0, 1].map(() => client.call('runs.start', { plugin: 'eda-gf180', handler: 'calibrate', params: heavy })))
    await expect.poll(async () => (await client.call('runs.list', { state: 'running' })).runs.length, { timeout: 120_000 }).toBe(2)
    const task = await client.call('tasks.create', { project: 'load', goal: 'FR-UI-04', title: 'busy agent', role: 'executor', provider: 'busy', maxSteps: 400 })
    const agent = client.call('tasks.run', { task: task.id, maxSteps: 400 })

    const samples: number[] = []
    const queries = [
      () => client.call('projects.get', { project: 'load' }),
      () => client.call('events.list', { limit: 200 }),
      () => client.call('incidents.list', { open: true }),
      () => client.call('tasks.list'),
      () => client.call('runs.list', {}),
    ]
    for (let i = 0; i < 200; i++) {
      const started = performance.now()
      await queries[i % queries.length]!()
      samples.push(performance.now() - started)
      await new Promise((r) => setTimeout(r, 20))
    }
    samples.sort((a, b) => a - b)
    const p95 = samples[Math.floor(samples.length * 0.95)]!
    console.log(`UI query latency under load: p50 ${samples[100]!.toFixed(1)} ms, p95 ${p95.toFixed(1)} ms, max ${samples.at(-1)!.toFixed(1)} ms`)
    expect((await client.call('runs.list', { state: 'running' })).runs.length).toBe(2) // Still under load when sampling ended.
    expect(p95).toBeLessThan(200)
    // No call may stall for long: a long maximum means something blocked the event loop.
    expect(samples.at(-1)!).toBeLessThan(1000)

    await client.call('tasks.pause', { task: task.id })
    await agent
    for (const r of runs) await client.call('runs.cancel', { run: r.id })
  })
})
