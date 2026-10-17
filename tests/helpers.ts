import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach } from 'vitest'
import { KernelClient } from '@llmchip/client'
import { Kernel } from '@llmchip/kernel'

/** A fresh temporary directory, removed after the current test. */
export function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'llmchip-test-'))
  afterEach(() => rmSync(dir, { recursive: true, force: true }))
  return dir
}

/** Starts an in-process kernel on a temporary home; stopped after the test. */
export async function startKernel(home = tempDir()): Promise<{ kernel: Kernel; home: string }> {
  const kernel = await Kernel.start({ home })
  afterEach(() => kernel.stop('test finished'))
  return { kernel, home }
}

export async function connect(kernel: Kernel): Promise<KernelClient> {
  const runtime = await import('@llmchip/kernel').then((m) => m.readRuntimeInfo(kernel.layout.root)!)
  const client = await KernelClient.connect(runtime)
  afterEach(() => client.close())
  return client
}
