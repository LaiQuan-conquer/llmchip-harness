import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { KernelError, loadConfig, Redactor, SecretStore } from '@llmchip/kernel'
import { OWNER, tempDir } from './helpers.ts'

describe('loadConfig', () => {
  it('applies schema defaults, the config file, then overrides', () => {
    const dir = tempDir()
    expect(loadConfig(dir)).toEqual({
      server: { host: '127.0.0.1', port: 0 },
      events: { verifyTailOnStart: 1000 },
      plugins: { searchPaths: [], bundled: true },
      scheduler: { maxParallel: 2, defaultTimeoutMs: 3_600_000, cancelGraceMs: 2000 },
    })
    writeFileSync(join(dir, 'config.json'), JSON.stringify({ server: { port: 8080 } }))
    expect(loadConfig(dir).server).toEqual({ host: '127.0.0.1', port: 8080 })
    expect(loadConfig(dir, { server: { port: 9090 } }).server.port).toBe(9090)
  })

  it('rejects invalid configuration', () => {
    const dir = tempDir()
    writeFileSync(join(dir, 'config.json'), JSON.stringify({ server: { port: 'eighty' } }))
    expect(() => loadConfig(dir)).toThrow(KernelError)
    writeFileSync(join(dir, 'config.json'), '{not json')
    expect(() => loadConfig(dir)).toThrow(/CONFIG_UNREADABLE|cannot parse/)
  })
})

describe('SecretStore', () => {
  const env = process.env.LLMCHIP_MASTER_KEY
  afterEach(() => {
    if (env === undefined) delete process.env.LLMCHIP_MASTER_KEY
    else process.env.LLMCHIP_MASTER_KEY = env
  })

  it('persists secrets encrypted', () => {
    const dir = tempDir()
    new SecretStore(dir).set('deepseek.apiKey', 'sk-very-secret-value', OWNER)
    const reopened = new SecretStore(dir)
    expect(reopened.list()).toEqual(['deepseek.apiKey'])
    expect(reopened.get('deepseek.apiKey')).toBe('sk-very-secret-value')
    expect(readFileSync(join(dir, 'secrets.json.enc'), 'utf8')).not.toContain('sk-very-secret-value')
  })

  it('refuses to open secrets with the wrong master key', () => {
    const dir = tempDir()
    process.env.LLMCHIP_MASTER_KEY = Buffer.alloc(32, 1).toString('base64')
    new SecretStore(dir).set('a', 'value-a', OWNER)
    process.env.LLMCHIP_MASTER_KEY = Buffer.alloc(32, 2).toString('base64')
    expect(() => new SecretStore(dir)).toThrow(/SECRETS_UNREADABLE|cannot decrypt/)
    process.env.LLMCHIP_MASTER_KEY = 'short'
    expect(() => new SecretStore(dir)).toThrow(KernelError)
  })
})

describe('Redactor', () => {
  it('replaces secret values in nested strings and keys', () => {
    const redactor = new Redactor(() => [
      ['token', 'abcd1234'],
      ['tiny', 'ab'],
    ])
    expect(redactor.text('use abcd1234 now')).toBe('use [REDACTED:token] now')
    expect(redactor.value({ a: ['xabcd1234x', 3, null], abcd1234: true, b: 'ab' })).toEqual({
      a: ['x[REDACTED:token]x', 3, null],
      '[REDACTED:token]': true,
      b: 'ab',
    })
  })
})
