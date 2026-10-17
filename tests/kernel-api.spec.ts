// End-to-end tests of the kernel API (IF-1) through the client library, against
// an in-process kernel.

import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { KernelCallError, KernelClient } from '@llmchip/client'
import { EventStore, Kernel, readRuntimeInfo, sha256 } from '@llmchip/kernel'
import type { EventRecord } from '@llmchip/kernel-api'
import { connect, startKernel, tempDir } from './helpers.ts'

describe('kernel API', () => {
  it('reports kernel info and the started event', async () => {
    const { kernel } = await startKernel()
    const client = await connect(kernel)
    const info = await client.call('kernel.info')
    expect(info).toMatchObject({ name: 'harnessd', pid: process.pid, head: 1 })
    const { events } = await client.call('events.list')
    expect(events.map((e) => e.kind)).toEqual(['kernel.started'])
  })

  it('rejects clients without the token', async () => {
    const { kernel } = await startKernel()
    await expect(KernelClient.connect({ url: kernel.url, token: 'wrong' })).rejects.toThrow(KernelCallError)
    const response = await fetch(`${kernel.url}/v1/artifacts/${'0'.repeat(64)}`)
    expect(response.status).toBe(401)
  })

  it('validates params and reports unknown methods', async () => {
    const { kernel } = await startKernel()
    const client = await connect(kernel)
    await expect(client.call('events.list', { limit: 0 })).rejects.toMatchObject({ rpcCode: -32602, code: 'INVALID_PARAMS' })
    await expect(client.call('artifacts.stat', { hash: 'nope' })).rejects.toMatchObject({ rpcCode: -32602 })
    await expect((client.call as (method: string) => Promise<unknown>)('no.such')).rejects.toMatchObject({ rpcCode: -32601 })
    await expect(client.call('artifacts.stat', { hash: '0'.repeat(64) })).rejects.toMatchObject({ rpcCode: -32000, code: 'ARTIFACT_NOT_FOUND' })
  })

  it('uploads and downloads artifacts with lineage and label propagation', async () => {
    const { kernel } = await startKernel()
    const client = await connect(kernel)
    const source = await client.upload('weights', { type: 'model.weights', label: 'nda' })
    expect(source.hash).toBe(sha256('weights'))
    const derived = await client.upload(new Blob(['quantized']), { type: 'model.quantized', label: 'public', inputs: [source.hash] })
    expect(derived.label).toBe('nda')

    const response = await client.download(derived.hash)
    expect(response.headers.get('x-llmchip-label')).toBe('nda')
    expect(await response.text()).toBe('quantized')

    const lineage = await client.call('artifacts.lineage', { hash: derived.hash })
    expect(lineage.edges).toEqual([{ from: source.hash, to: derived.hash, run: null }])
    await expect(client.upload('x', { type: 't', inputs: ['f'.repeat(64)] })).rejects.toMatchObject({ code: 'ARTIFACT_INPUT_NOT_FOUND' })
  })

  it('replays and streams events without gaps or duplicates', async () => {
    const { kernel } = await startKernel()
    const client = await connect(kernel)
    for (let i = 0; i < 20; i++) await client.upload(`before ${i}`, { type: 't' })
    const seen: EventRecord[] = []
    const writes: Promise<unknown>[] = []
    // Writes race with the replay.
    for (let i = 0; i < 20; i++) writes.push(client.upload(`during ${i}`, { type: 't' }))
    await client.subscribe((event) => seen.push(event), 0)
    await Promise.all(writes)
    for (let i = 0; i < 20; i++) await client.upload(`after ${i}`, { type: 't' })
    const head = (await client.call('kernel.info')).head
    await expect.poll(() => seen.length).toBe(head)
    expect(seen.map((e) => e.seq)).toEqual(Array.from({ length: head }, (_, i) => i + 1))
  })

  it('never writes secret values to the event log or the log file', async () => {
    const { kernel, home } = await startKernel()
    const client = await connect(kernel)
    const secret = 'sk-live-0123456789abcdef'
    await client.call('secrets.set', { name: 'llm.deepseek.apiKey', value: secret })
    expect(await client.call('secrets.list')).toEqual({ names: ['llm.deepseek.apiKey'] })
    const artifact = await client.upload('x', { type: 't' })
    const marked = await client.call('artifacts.markDeleted', { hash: artifact.hash, reason: `leaked ${secret} here` })
    expect(marked.deleted?.reason).toBe('leaked [REDACTED:llm.deepseek.apiKey] here')
    kernel.logger.info(`log line containing ${secret}`)
    await kernel.stop()

    for (const file of ['db/harness.sqlite', 'db/harness.sqlite-wal', 'logs/harnessd.log', 'secrets/secrets.json.enc']) {
      const path = join(home, file)
      if (existsSync(path)) expect(readFileSync(path).includes(secret), file).toBe(false)
    }
    const events = new EventStore(join(home, 'db', 'harness.sqlite'))
    expect(events.list({ kind: 'secret.changed' })[0]?.payload).toEqual({ name: 'llm.deepseek.apiKey', change: 'set' })
    expect(events.verify().ok).toBe(true)
    events.close()
  })

  it('allows only one kernel per home and shuts down cleanly', async () => {
    const home = tempDir()
    const { kernel } = await startKernel(home)
    await expect(Kernel.start({ home })).rejects.toMatchObject({ code: 'KERNEL_ALREADY_RUNNING' })
    const client = await connect(kernel)
    await client.call('kernel.shutdown')
    await kernel.stopped
    expect(readRuntimeInfo(home)).toBeUndefined()
    expect(existsSync(join(home, 'run', 'harnessd.lock'))).toBe(false)
    const events = new EventStore(join(home, 'db', 'harness.sqlite'))
    expect(events.list().at(-1)).toMatchObject({ kind: 'kernel.stopped', payload: { reason: 'requested by client' } })
    events.close()
  })
})
