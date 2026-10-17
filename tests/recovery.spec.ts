// Crash recovery of harnessd (requirements NFR-04, FR-TRC-03): the kernel
// process is killed with SIGKILL while clients write, then restarted on the
// same home. Every acknowledged write must survive, the hash chain must
// verify, no partially written artifact may exist, and projections must be
// rebuildable from the event log.

import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { join, resolve } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { afterEach, describe, expect, it } from 'vitest'
import { KernelClient } from '@llmchip/client'
import { tempDir } from './helpers.ts'

const CLI = resolve(import.meta.dirname, '../packages/kernel/src/cli.ts')
const children = new Set<ChildProcess>()
afterEach(() => {
  for (const child of children) child.kill('SIGKILL')
  children.clear()
})

async function spawnKernel(home: string): Promise<{ child: ChildProcess; client: KernelClient }> {
  const child = spawn(process.execPath, ['--conditions=development', CLI, 'serve', '--home', home], { stdio: ['ignore', 'ignore', 'pipe'] })
  children.add(child)
  let stderr = ''
  child.stderr!.on('data', (chunk) => (stderr += chunk))
  const runtimePath = join(home, 'run', 'harnessd.json')
  for (let i = 0; i < 200; i++) {
    if (child.exitCode !== null) throw new Error(`harnessd exited with ${child.exitCode}: ${stderr}`)
    if (existsSync(runtimePath)) {
      const runtime = JSON.parse(readFileSync(runtimePath, 'utf8'))
      if (runtime.pid === child.pid) return { child, client: await KernelClient.connect(runtime) }
    }
    await sleep(50)
  }
  throw new Error(`harnessd did not start: ${stderr}`)
}

async function kill(child: ChildProcess): Promise<void> {
  const exited = new Promise((resolve) => child.once('exit', resolve))
  child.kill('SIGKILL')
  await exited
  children.delete(child)
}

describe('crash recovery', () => {
  it('keeps every acknowledged write across repeated SIGKILLs', { timeout: 120_000 }, async () => {
    const home = tempDir()
    const acknowledged: string[] = []

    for (let round = 0; round < 3; round++) {
      const { child, client } = await spawnKernel(home)
      let stop = false
      const writer = async (worker: number) => {
        while (!stop) {
          // Mix small and large uploads so that kills land mid-write.
          const size = worker === 0 ? 4 * 1024 * 1024 : 1024 + Math.floor(Math.random() * 64 * 1024)
          const inputs = acknowledged.length > 0 ? [acknowledged[Math.floor(Math.random() * acknowledged.length)]!] : []
          try {
            const meta = await client.upload(new Uint8Array(randomBytes(size)), { type: 'test.blob', inputs })
            acknowledged.push(meta.hash)
          } catch {
            return // The kernel was killed.
          }
        }
      }
      const writers = [0, 1, 2, 3].map(writer)
      await sleep(400 + round * 200)
      await kill(child)
      stop = true
      await Promise.all(writers)
    }

    const { child, client } = await spawnKernel(home)
    expect(readdirSync(join(home, 'store', 'tmp'))).toEqual([])
    expect(await client.call('events.verify')).toMatchObject({ ok: true, problems: [] })
    expect(await client.call('artifacts.verify')).toMatchObject({ ok: true, problems: [] })
    expect(acknowledged.length).toBeGreaterThan(10)
    for (const hash of acknowledged) {
      const meta = await client.call('artifacts.stat', { hash })
      expect(meta.hash).toBe(hash)
    }

    const before = await client.call('artifacts.list', { limit: 1000 })
    const { rebuilt } = await client.call('projections.rebuild')
    expect(rebuilt.map((r) => r.projection)).toContain('artifacts')
    expect(await client.call('artifacts.list', { limit: 1000 })).toEqual(before)

    await client.call('kernel.shutdown')
    await new Promise((resolve) => child.once('exit', resolve))
    expect(child.exitCode).toBe(0)
  })
})
