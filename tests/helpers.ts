import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach } from 'vitest'
import { KernelClient } from '@llmchip/client'
import { resolve } from 'node:path'
import { Kernel, type KernelOptions } from '@llmchip/kernel'

/** Test plugins: a stub tool and a test flow pack. */
export const FIXTURE_PLUGINS = resolve(import.meta.dirname, 'fixtures/plugins')
/** The repository's own plugins (estimator, default flow). */
export const REPO_PLUGINS = resolve(import.meta.dirname, '../plugins')
/** Python of the uv workspace, with the plugin SDK installed. */
export const PYTHON = resolve(import.meta.dirname, '../.venv/bin/python')

/** A fresh temporary directory, removed after the current test. */
export function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'llmchip-test-'))
  afterEach(() => rmSync(dir, { recursive: true, force: true }))
  return dir
}

/**
 * Starts an in-process kernel on a temporary home; stopped after the test.
 * Bundled plugins are off unless `bundled` is set, so tests see only what they load.
 */
export async function startKernel(
  home = tempDir(),
  options: Omit<KernelOptions, 'home'> & { bundled?: boolean } = {},
): Promise<{ kernel: Kernel; home: string }> {
  const { bundled = false, config = {}, ...rest } = options
  const kernel = await Kernel.start({ home, ...rest, config: { ...config, plugins: { bundled, ...config.plugins } } })
  afterEach(() => kernel.stop('test finished'))
  return { kernel, home }
}

export async function connect(kernel: Kernel): Promise<KernelClient> {
  const runtime = await import('@llmchip/kernel').then((m) => m.readRuntimeInfo(kernel.layout.root)!)
  const client = await KernelClient.connect(runtime)
  afterEach(() => client.close())
  return client
}
