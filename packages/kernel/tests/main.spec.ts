import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { KERNEL_API_VERSION } from '@llmchip/kernel-api'
import { main } from '@llmchip/kernel'

const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))

function run(argv: string[]) {
  let out = ''
  let err = ''
  const code = main(argv, { out: (text) => { out += text }, err: (text) => { err += text } })
  return { code, out, err }
}

describe('harnessd main', () => {
  it('prints the daemon and kernel API versions', () => {
    const { code, out, err } = run(['--version'])
    expect(code).toBe(0)
    expect(out).toBe(`harnessd ${pkg.version} (kernel-api ${KERNEL_API_VERSION})\n`)
    expect(err).toBe('')
  })

  it('prints usage with no arguments or --help', () => {
    for (const argv of [[], ['--help']]) {
      const { code, out } = run(argv)
      expect(code).toBe(0)
      expect(out).toMatch(/^Usage: harnessd/)
    }
  })

  it('rejects unknown arguments with exit code 2', () => {
    const { code, out, err } = run(['--serve'])
    expect(code).toBe(2)
    expect(out).toBe('')
    expect(err).toMatch(/unknown argument: --serve/)
  })

  it('rejects extra arguments with exit code 2', () => {
    expect(run(['--version', '--help']).code).toBe(2)
  })
})
