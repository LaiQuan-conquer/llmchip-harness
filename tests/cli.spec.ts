// The llmchip command line against an in-process kernel.

import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { main } from '@llmchip/cli'
import { sha256 } from '@llmchip/kernel'
import { FIXTURE_PLUGINS, startKernel, tempDir } from './helpers.ts'

async function llmchip(home: string, args: string[], stdin?: string) {
  let out = ''
  let err = ''
  const code = await main(['--home', home, ...args], {
    out: (text) => (out += text),
    err: (text) => (err += text),
    ...(stdin === undefined ? {} : { stdin: async () => stdin }),
  })
  return { code, out, err }
}

describe('llmchip CLI', () => {
  it('explains how to start the kernel when none is running', async () => {
    const { code, err } = await llmchip(tempDir(), ['info'])
    expect(code).toBe(1)
    expect(err).toMatch(/harnessd is not running/)
  })

  it('manages artifacts, events and secrets', async () => {
    const { home } = await startKernel()
    const dir = tempDir()
    writeFileSync(join(dir, 'weights.bin'), 'weights')
    writeFileSync(join(dir, 'report.txt'), 'report')

    expect((await llmchip(home, ['info'])).out).toMatch(/^harnessd .* 1 events\n$/)
    const put = await llmchip(home, ['artifact', 'put', join(dir, 'weights.bin'), '--type', 'model.weights', '--label', 'nda'])
    expect(put).toEqual({ code: 0, out: `${sha256('weights')}\n`, err: '' })
    const weights = sha256('weights')
    const report = (await llmchip(home, ['artifact', 'put', join(dir, 'report.txt'), '--type', 'report', '--input', weights])).out.trim()

    const stat = JSON.parse((await llmchip(home, ['artifact', 'stat', report])).out)
    expect(stat).toMatchObject({ type: 'report', label: 'nda', inputs: [weights] })
    expect((await llmchip(home, ['artifact', 'lineage', report])).out).toBe(`${weights} -> ${report}\n`)

    await llmchip(home, ['artifact', 'get', report, '-o', join(dir, 'out.txt')])
    expect(readFileSync(join(dir, 'out.txt'), 'utf8')).toBe('report')

    expect((await llmchip(home, ['artifact', 'rm', report])).code).toBe(2)
    expect((await llmchip(home, ['artifact', 'rm', report, '--reason', 'obsolete'])).code).toBe(0)
    expect((await llmchip(home, ['artifact', 'list'])).out.split('\n').filter(Boolean)).toHaveLength(1)
    expect((await llmchip(home, ['artifact', 'list', '--all'])).out).toMatch(/deleted/)
    expect((await llmchip(home, ['artifact', 'verify'])).out).toBe('ok: 2 artifacts checked\n')

    expect((await llmchip(home, ['secret', 'set', 'llm.key'], 'sk-123456\n')).code).toBe(0)
    expect((await llmchip(home, ['secret', 'list'])).out).toBe('llm.key\n')

    const events = (await llmchip(home, ['events', 'list', '--kind', 'artifact.'])).out
    expect(events.match(/artifact\./g)).toHaveLength(3)
    expect((await llmchip(home, ['events', 'verify'])).code).toBe(0)
    expect((await llmchip(home, ['projections', 'rebuild'])).out).toMatch(/^artifacts v1: \d+ events\n/m)
    expect((await llmchip(home, ['bogus'])).code).toBe(2)
    expect((await llmchip(home, ['artifact', 'stat', '0'.repeat(64)])).err).toMatch(/ARTIFACT_NOT_FOUND/)
  })

  it('plans, starts, shows and resumes stage pipelines', { timeout: 30_000 }, async () => {
    const { home } = await startKernel(undefined, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS] } } })
    const dir = tempDir()
    expect((await llmchip(home, ['project', 'create', 'chip', '--title', 'T', '--flow', 'test'])).code).toBe(0)
    const plan = await llmchip(home, ['pipeline', 'plan', 'chip', 'A', '--kind', 'develop'])
    expect(plan).toMatchObject({ code: 1, out: 'A: 5 nodes, not ready\n  A/drc needs slot:design\n  A/flaky needs slot:design\n' })
    expect((await llmchip(home, ['pipeline', 'start', 'chip', 'A'])).code).toBe(2) // --kind is required
    writeFileSync(join(dir, 'design.txt'), 'bad design')
    const bad = (await llmchip(home, ['artifact', 'put', join(dir, 'design.txt'), '--type', 'test.design'])).out.trim()
    await llmchip(home, ['project', 'bind', 'chip', 'design', bad])
    const failed = await llmchip(home, ['pipeline', 'start', 'chip', 'A', '--kind', 'develop', '--wait'])
    expect(failed.code).toBe(1)
    expect(failed.out).toMatch(/develop A {2}failed {2}\(3\/5 done, 1 failed, 1 skipped\)/)
    expect(failed.out).toMatch(/A\/drc +done +\S+ {2}stub\.drc: pass/)
    expect(failed.out).toMatch(/A\/flaky +failed +\S+ {2}PIPELINE_RUN_FAILED/)
    const id = failed.out.split(' ')[0]!
    writeFileSync(join(dir, 'design.txt'), 'fixed design')
    const fixed = (await llmchip(home, ['artifact', 'put', join(dir, 'design.txt'), '--type', 'test.design'])).out.trim()
    await llmchip(home, ['project', 'bind', 'chip', 'design', fixed])
    expect((await llmchip(home, ['pipeline', 'resume', id, '--wait'])).out).toMatch(/develop A {2}passed/)
    expect((await llmchip(home, ['pipeline', 'list', '--project', 'chip'])).out).toMatch(new RegExp(`^${id} {2}chip {2}develop +A +passed\n$`))
    const tests = await llmchip(home, ['pipeline', 'start', 'chip', 'A', '--kind', 'test', '--wait'])
    expect(tests).toMatchObject({ code: 0 })
    expect(tests.out).toMatch(/tests 2 pass, 0 fail, 0 indeterminate/)
  })
})
