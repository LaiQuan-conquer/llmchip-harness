// The llmchip command line against an in-process kernel.

import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { main } from '@llmchip/cli'
import { sha256 } from '@llmchip/kernel'
import { startKernel, tempDir } from './helpers.ts'

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
    expect((await llmchip(home, ['projections', 'rebuild'])).out).toMatch(/^artifacts v1: \d+ events\n$/)
    expect((await llmchip(home, ['bogus'])).code).toBe(2)
    expect((await llmchip(home, ['artifact', 'stat', '0'.repeat(64)])).err).toMatch(/ARTIFACT_NOT_FOUND/)
  })
})
