import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { KERNEL_API_VERSION } from '@llmchip/kernel-api'
import { main } from '@llmchip/kernel'

const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))

async function run(argv: string[]) {
  let out = ''
  let err = ''
  const code = await main(argv, { out: (text) => (out += text), err: (text) => (err += text) })
  return { code, out, err }
}

describe('harnessd main', () => {
  it('prints the daemon and kernel API versions', async () => {
    const { code, out, err } = await run(['--version'])
    expect(code).toBe(0)
    expect(out).toBe(`harnessd ${pkg.version} (kernel-api ${KERNEL_API_VERSION})\n`)
    expect(err).toBe('')
  })

  it('prints usage with no arguments or --help', async () => {
    for (const argv of [[], ['--help']]) {
      const { code, out } = await run(argv)
      expect(code).toBe(0)
      expect(out).toMatch(/^Usage: harnessd/)
    }
  })

  it('rejects unknown commands and options with exit code 2', async () => {
    const unknown = await run(['--serve'])
    expect(unknown.code).toBe(2)
    expect(unknown.out).toBe('')
    expect(unknown.err).toMatch(/unknown argument: --serve/)
    expect((await run(['--version', '--help'])).code).toBe(2)
    expect((await run(['serve', '--bogus'])).code).toBe(2)
    expect((await run(['serve', '--port', '70000'])).err).toMatch(/invalid port/)
  })
})
