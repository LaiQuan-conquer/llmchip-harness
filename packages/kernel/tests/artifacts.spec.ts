import { chmodSync, existsSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { text } from 'node:stream/consumers'
import { describe, expect, it } from 'vitest'
import { ArtifactStore, EventStore, KernelError, sha256 } from '@llmchip/kernel'
import { OWNER, tempDir } from './helpers.ts'

function open(dir = tempDir()) {
  const events = new EventStore(join(dir, 'events.sqlite'))
  return { dir, events, artifacts: new ArtifactStore(join(dir, 'store'), events) }
}

describe('ArtifactStore', () => {
  it('stores content by hash, read-only, and records an event', async () => {
    const { events, artifacts } = open()
    const meta = await artifacts.put('hello', { type: 'text.note', mediaType: 'text/plain', creator: OWNER })
    expect(meta).toMatchObject({ hash: sha256('hello'), size: 5, type: 'text.note', label: 'internal', inputs: [], deleted: null })
    expect(await text(artifacts.read(meta.hash))).toBe('hello')
    expect(statSync(artifacts.objectPath(meta.hash)).mode & 0o222).toBe(0)
    expect(events.list({ kind: 'artifact.created' })).toHaveLength(1)
  })

  it('is idempotent for identical content', async () => {
    const { events, artifacts } = open()
    const a = await artifacts.put('same', { type: 't', creator: OWNER })
    const b = await artifacts.put(new TextEncoder().encode('same'), { type: 'other', creator: OWNER })
    expect(b).toEqual(a)
    expect(events.head()).toBe(1)
  })

  it('requires inputs to exist and raises the label to the most sensitive input', async () => {
    const { artifacts } = open()
    await expect(artifacts.put('x', { type: 't', creator: OWNER, inputs: ['0'.repeat(64)] })).rejects.toThrow(KernelError)
    const pdk = await artifacts.put('pdk', { type: 'pdk', label: 'nda', creator: OWNER })
    const report = await artifacts.put('report', { type: 'report', label: 'public', inputs: [pdk.hash], creator: OWNER })
    expect(report.label).toBe('nda')
  })

  it('answers lineage queries in both directions', async () => {
    const { artifacts } = open()
    const weights = await artifacts.put('w', { type: 'weights', creator: OWNER })
    const quant = await artifacts.put('q', { type: 'quant', inputs: [weights.hash], run: 'run-1', creator: OWNER })
    const rtl = await artifacts.put('r', { type: 'rtl', inputs: [quant.hash], run: 'run-2', creator: OWNER })
    const up = artifacts.lineage(rtl.hash, 'up')
    expect(up.nodes.map((n) => n.type).sort()).toEqual(['quant', 'rtl', 'weights'])
    expect(up.edges).toEqual([
      { from: quant.hash, to: rtl.hash, run: 'run-2' },
      { from: weights.hash, to: quant.hash, run: 'run-1' },
    ])
    expect(artifacts.lineage(weights.hash, 'down').nodes).toHaveLength(3)
  })

  it('marks artifacts deleted without removing content', async () => {
    const { artifacts } = open()
    const meta = await artifacts.put('keep me', { type: 't', creator: OWNER })
    const marked = artifacts.markDeleted(meta.hash, 'superseded', OWNER)
    expect(marked.deleted).toMatchObject({ by: OWNER, reason: 'superseded' })
    expect(artifacts.list()).toEqual([])
    expect(artifacts.list({ includeDeleted: true })).toHaveLength(1)
    expect(await text(artifacts.read(meta.hash))).toBe('keep me')
  })

  it('verifies stored content and detects tampering', async () => {
    const { artifacts } = open()
    const meta = await artifacts.put('original', { type: 't', creator: OWNER })
    expect((await artifacts.verify()).ok).toBe(true)
    const path = artifacts.objectPath(meta.hash)
    chmodSync(path, 0o644)
    writeFileSync(path, 'tampered content')
    const codes = (await artifacts.verify()).problems.map((p) => p.code)
    expect(codes).toEqual(expect.arrayContaining(['ARTIFACT_SIZE_MISMATCH', 'ARTIFACT_WRITABLE', 'ARTIFACT_HASH_MISMATCH']))
  })

  it('removes interrupted writes and keeps the catalog rebuildable', async () => {
    const { dir, events, artifacts } = open()
    await artifacts.put('a', { type: 't', creator: OWNER })
    writeFileSync(join(dir, 'store', 'tmp', 'partial'), 'half written')
    expect(artifacts.cleanupTemp()).toBe(1)
    expect(readdirSync(join(dir, 'store', 'tmp'))).toEqual([])
    const before = artifacts.list()
    events.db.exec('DROP TABLE artifacts; DROP TABLE artifact_inputs;')
    events.rebuildProjections(['artifacts'])
    expect(artifacts.list()).toEqual(before)
    expect(existsSync(artifacts.objectPath(before[0]!.hash))).toBe(true)
  })
})
