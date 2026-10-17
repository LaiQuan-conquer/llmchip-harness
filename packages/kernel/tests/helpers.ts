import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach } from 'vitest'

/** A fresh temporary directory, removed after the current test. */
export function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'llmchip-test-'))
  afterEach(() => rmSync(dir, { recursive: true, force: true }))
  return dir
}

export const OWNER = { kind: 'human', id: 'tester' } as const
