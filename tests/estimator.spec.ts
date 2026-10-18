// The estimator plugin (requirements FR-EST, AC-M1-10) end to end: bundled
// plugins, the Python plugin process, the CLI and the G0 feasibility check.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { main } from '@llmchip/cli'
import type { KernelClient } from '@llmchip/client'
import { connect, REPO_PLUGINS, startKernel } from './helpers.ts'

async function llmchip(home: string, args: string[]) {
  let out = ''
  let err = ''
  const code = await main(['--home', home, ...args], { out: (text) => (out += text), err: (text) => (err += text) })
  return { code, out, err }
}

async function setup() {
  const { kernel, home } = await startKernel(undefined, { bundled: true })
  return { home, client: await connect(kernel) }
}

async function dataHash(client: KernelClient, id: string): Promise<string> {
  const { plugins } = await client.call('plugins.list')
  return plugins.flatMap((p) => p.data).find((d) => d.id === id)!.hash
}

describe('estimator (AC-M1-10)', () => {
  it('registers every bundled plugin without problems', async () => {
    const { client } = await setup()
    const { plugins, problems } = await client.call('plugins.list')
    expect(problems).toEqual([])
    expect(plugins.map((p) => p.id).sort()).toEqual(['eda-gf180', 'estimator', 'flow-default', 'process-library'])
    expect((await client.call('checks.list')).checks.map((c) => c.id)).toContain('estimate.feasibility')
  })

  it('estimates stories260K, 0.5B and 8B on the 180nm target with a full breakdown', { timeout: 60_000 }, async () => {
    const { home } = await setup()
    const stories = await llmchip(home, ['estimate', '--model', 'stories260K'])
    expect(stories.err).toBe('')
    expect(stories.out).toMatch(/\*\*Conclusion: conditionally_feasible\*\*/)
    for (const row of ['weight storage', 'KV cache (off chip)', 'activation buffers', 'compute units', 'control and interfaces', 'power network', 'IO ring, seal ring and unused die area']) {
      expect(stories.out).toContain(`| ${row} |`)
    }
    expect(stories.out).toMatch(/condition: .*ROM compiler/)
    expect(stories.out).toMatch(/## Cost[\s\S]*CNY \(5000 CNY\/mm2, minimum 2 mm2\)/)
    expect(stories.out).toMatch(/tokens\/s; bottleneck: (compute|off-chip interface)/)

    for (const model of ['qwen2.5-0.5b', 'llama3.1-8b']) {
      const report = JSON.parse((await llmchip(home, ['--json', 'estimate', '--model', model])).out)
      expect(report.conclusion.feasibility).toBe('infeasible')
      expect(report.conclusion.reasons[0]).toMatch(/reticle limit/)
      expect(report.multiChip.chipsAtReticle.typ).toBeGreaterThan(1)
      for (const item of report.items) expect(item.confidence).toBeTruthy()
    }
  })

  it('reports missing data instead of assuming zero, and the G0 check judges accordingly', { timeout: 60_000 }, async () => {
    const { client } = await setup()
    const profile = JSON.parse(readFileSync(join(REPO_PLUGINS, 'process-library/data/sephonchitech-180bcd.json'), 'utf8'))
    profile.ring.ioRingWidthMm = null
    const incomplete = await client.upload(JSON.stringify(profile), { type: 'process.profile', mediaType: 'application/json' })
    const library = await dataHash(client, 'library-180nm')
    const estimate = async (profileHash: string, preset: string) =>
      client.call('runs.wait', {
        run: (
          await client.call('runs.start', {
            plugin: 'estimator',
            handler: 'estimate',
            params: { model: { preset }, weightStorage: 'sephonchitech-180bcd.foundry-rom' },
            inputs: [
              { name: 'profile', hash: profileHash },
              { name: 'library', hash: library },
            ],
          })
        ).id,
      })

    const missing = await estimate(incomplete.hash, 'stories260K')
    expect(missing.metrics).toMatchObject({ feasibility: 'insufficient_data', missing_count: 1 })
    const report = JSON.parse(await (await client.download(missing.outputs.find((o) => o.name === 'report')!.hash)).text())
    expect(report.missing).toEqual([{ parameter: 'ring.ioRingWidthMm', howToObtain: 'IO library from the foundry' }])

    const g0 = (run: string) => client.call('verdicts.evaluate', { check: 'estimate.feasibility', run, project: 'chip' })
    expect(await g0(missing.id)).toMatchObject({ verdict: 'indeterminate', reasons: [{ code: 'COVERAGE_INCOMPLETE' }] })
    const target = await dataHash(client, 'sephonchitech-180bcd')
    expect(await g0((await estimate(target, 'stories260K')).id)).toMatchObject({ verdict: 'pass' })
    expect(await g0((await estimate(target, 'llama3.1-8b')).id)).toMatchObject({ verdict: 'fail' })
  })
})
