// The default flow's stage pipelines with the bundled plugins (requirements FR-PRJ-07/08):
// on a fresh project, each stage's development pipeline runs from the flow pack's defaults and
// the slots the earlier stages bound, and its required tests reuse the development runs.
// S0-S3 always run (they need `.venv`); the whole flow S0-S7 through `stage: all` needs the
// EDA image and LLMCHIP_FULL_FLOW=1, like tests/m4-flow.spec.ts.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import type { PipelineRecord } from '@llmchip/kernel-api'
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

async function finished(client: KernelClient, id: string, timeoutMs: number): Promise<PipelineRecord> {
  for (const end = Date.now() + timeoutMs; Date.now() < end; await sleep(200)) {
    const record = await client.call('pipelines.get', { pipeline: id })
    if (record.state !== 'running') return record
  }
  throw new Error(`pipeline ${id} still running after ${timeoutMs} ms`)
}

const failures = (record: PipelineRecord) => record.nodes.filter((n) => n.state !== 'done').map((n) => `${n.id}: ${n.state} ${JSON.stringify(n.reason)}`)

describe('default flow pipelines', () => {
  it('runs the development and the tests of S0-S3 on a fresh project', { timeout: 900_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    await client.call('projects.create', { id: 'chip', title: 'Proxy through the stage pipelines' })
    for (const stage of ['S0', 'S1', 'S2', 'S3']) {
      expect(await client.call('pipelines.plan', { project: 'chip', stage, kind: 'develop' })).toMatchObject({ ready: true })
      const develop = await finished(client, (await client.call('pipelines.start', { project: 'chip', stage, kind: 'develop' })).id, 600_000)
      expect(failures(develop), stage).toEqual([])
      if (stage === 'S1') continue // the proxy's intake has no check
      const tests = await finished(client, (await client.call('pipelines.start', { project: 'chip', stage, kind: 'test' })).id, 600_000)
      expect(failures(tests), stage).toEqual([])
      // Each test judged a run of the development pipeline again.
      for (const node of tests.nodes) expect(node.reused, node.id).toBeTruthy()
    }
    const state = await client.call('projects.get', { project: 'chip' })
    expect(state.stages.filter((s) => s.entered).map((s) => s.id)).toEqual(['S0', 'S1', 'S2', 'S3'])
    for (const slot of ['estimate-request', 'model-ir', 'tokenizer', 'eval-set', 'numeric-spec', 'l2-weights', 'l2-trace', 'chip-spec', 'ppa-estimate']) {
      expect(state.slots[slot], slot).toBeTruthy()
    }
    // S1 declares no tests (the proxy's intake has no check).
    await expect(client.call('pipelines.start', { project: 'chip', stage: 'S1', kind: 'test' })).rejects.toMatchObject({ code: 'PIPELINE_EMPTY' })
  })

  it.skipIf(!process.env.LLMCHIP_FULL_FLOW || !available(IMAGE))(
    'runs all development and all tests of S0-S7 from the two project entries',
    { timeout: 7_200_000 },
    async () => {
      const { kernel } = await startKernel(undefined, { bundled: true, config: { scheduler: { containerCpus: 16, containerMemory: '12g' } } })
      const client = await connect(kernel)
      await client.call('projects.create', { id: 'chip', title: 'Proxy, all stages' })
      const develop = await finished(client, (await client.call('pipelines.start', { project: 'chip', stage: 'all', kind: 'develop' })).id, 7_000_000)
      expect(failures(develop)).toEqual([])
      expect(develop.stages).toEqual(['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7'])
      const tests = await finished(client, (await client.call('pipelines.start', { project: 'chip', stage: 'all', kind: 'test' })).id, 600_000)
      expect(failures(tests)).toEqual([])
      expect(tests.summary).toMatchObject({ fail: 0, indeterminate: 0 })
      expect(tests.summary.pass).toBe(tests.nodes.length)
    },
  )
})
