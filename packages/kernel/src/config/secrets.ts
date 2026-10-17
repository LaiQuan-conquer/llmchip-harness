import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Actor } from '@llmchip/kernel-api'
import type { EventStore } from '../events/store.ts'
import { KernelError } from '../util/errors.ts'

const KEY_ENV = 'LLMCHIP_MASTER_KEY'

/**
 * Encrypted secret storage (requirements NFR-07, architecture §4.10). Values
 * are kept in one AES-256-GCM encrypted file. The master key comes from
 * `LLMCHIP_MASTER_KEY` (base64, 32 bytes); the desktop shell supplies it from
 * the OS credential store. Without it, a key file with mode 0600 is created in
 * the secrets directory. Secret values never enter events, logs or artifacts.
 */
export class SecretStore {
  private readonly file: string
  private readonly key: Buffer
  private values: Map<string, string>
  private events: EventStore | undefined

  constructor(secretsDir: string) {
    this.file = join(secretsDir, 'secrets.json.enc')
    this.key = loadKey(secretsDir)
    this.values = this.load()
  }

  /**
   * Records later changes as `secret.changed` events. Separate from the
   * constructor because the event store itself redacts with this store.
   */
  bindEvents(events: EventStore): void {
    this.events = events
  }

  list(): string[] {
    return [...this.values.keys()].sort()
  }

  get(name: string): string | undefined {
    return this.values.get(name)
  }

  set(name: string, value: string, actor: Actor): void {
    const next = new Map(this.values).set(name, value)
    this.save(next)
    this.values = next
    this.events?.append({ kind: 'secret.changed', payload: { name, change: 'set' }, actor })
  }

  remove(name: string, actor: Actor): void {
    if (!this.values.has(name)) throw new KernelError('SECRET_NOT_FOUND', `secret ${name} does not exist`, { name })
    const next = new Map(this.values)
    next.delete(name)
    this.save(next)
    this.values = next
    this.events?.append({ kind: 'secret.changed', payload: { name, change: 'removed' }, actor })
  }

  /** Name and value of every secret, for redaction. */
  entries(): [string, string][] {
    return [...this.values.entries()]
  }

  private load(): Map<string, string> {
    if (!existsSync(this.file)) return new Map()
    try {
      const { iv, tag, data } = JSON.parse(readFileSync(this.file, 'utf8'))
      const decipher = createDecipheriv('aes-256-gcm', this.key, Buffer.from(iv, 'base64'))
      decipher.setAuthTag(Buffer.from(tag, 'base64'))
      const plain = Buffer.concat([decipher.update(Buffer.from(data, 'base64')), decipher.final()])
      return new Map(Object.entries(JSON.parse(plain.toString('utf8'))))
    } catch {
      throw new KernelError('SECRETS_UNREADABLE', `cannot decrypt ${this.file}; is ${KEY_ENV} correct?`, { path: this.file })
    }
  }

  private save(values: Map<string, string>): void {
    const iv = randomBytes(12)
    const cipher = createCipheriv('aes-256-gcm', this.key, iv)
    const data = Buffer.concat([cipher.update(JSON.stringify(Object.fromEntries(values)), 'utf8'), cipher.final()])
    const body = JSON.stringify({
      v: 1,
      iv: iv.toString('base64'),
      tag: cipher.getAuthTag().toString('base64'),
      data: data.toString('base64'),
    })
    const tmp = `${this.file}.tmp`
    writeFileSync(tmp, body, { mode: 0o600 })
    renameSync(tmp, this.file)
  }
}

function loadKey(secretsDir: string): Buffer {
  const fromEnv = process.env[KEY_ENV]
  if (fromEnv) {
    const key = Buffer.from(fromEnv, 'base64')
    if (key.length !== 32) throw new KernelError('MASTER_KEY_INVALID', `${KEY_ENV} must be 32 bytes, base64-encoded`)
    return key
  }
  const path = join(secretsDir, 'master.key')
  if (!existsSync(path)) writeFileSync(path, randomBytes(32).toString('base64'), { mode: 0o600, flag: 'wx' })
  return Buffer.from(readFileSync(path, 'utf8').trim(), 'base64')
}

/** Shortest secret value that is redacted; shorter values would cause false matches. */
const MIN_REDACT_LENGTH = 4

/**
 * Replaces every occurrence of a secret value in strings, recursively, with
 * `[REDACTED:<name>]`. Applied to event payloads and log lines.
 */
export class Redactor {
  private readonly secrets: () => [string, string][]

  constructor(secrets: () => [string, string][]) {
    this.secrets = secrets
  }

  text(text: string): string {
    let out = text
    for (const [name, value] of this.secrets()) {
      if (value.length >= MIN_REDACT_LENGTH && out.includes(value)) out = out.split(value).join(`[REDACTED:${name}]`)
    }
    return out
  }

  value<T>(value: T): T {
    if (typeof value === 'string') return this.text(value) as T
    if (Array.isArray(value)) return value.map((item) => this.value(item)) as T
    if (value && typeof value === 'object') {
      const out: Record<string, unknown> = {}
      for (const [key, item] of Object.entries(value)) out[this.text(key)] = this.value(item)
      return out as T
    }
    return value
  }
}
