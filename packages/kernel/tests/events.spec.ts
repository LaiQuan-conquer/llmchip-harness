import { join } from 'node:path'
import type { DatabaseSync } from 'node:sqlite'
import { describe, expect, it, vi } from 'vitest'
import { EventStore, GENESIS_HASH, KernelError, type Projection } from '@llmchip/kernel'
import { OWNER, tempDir } from './helpers.ts'

const started = { version: '0.0.0', apiVersion: '0.1.0', pid: 1 }

function open(dir: string, options?: ConstructorParameters<typeof EventStore>[1]) {
  return new EventStore(join(dir, 'events.sqlite'), options)
}

/** Counts events per kind; version is configurable to test rebuilds. */
function counter(version = 1): Projection & { calls: number } {
  return {
    name: 'counter',
    version,
    calls: 0,
    setup(db: DatabaseSync) {
      db.exec('CREATE TABLE IF NOT EXISTS counter (kind TEXT PRIMARY KEY, n INTEGER NOT NULL)')
    },
    reset(db: DatabaseSync) {
      db.exec('DROP TABLE IF EXISTS counter')
    },
    apply(event, db) {
      this.calls++
      db.prepare('INSERT INTO counter (kind, n) VALUES (?, 1) ON CONFLICT (kind) DO UPDATE SET n = n + 1').run(event.kind)
    },
  }
}

function counts(store: EventStore) {
  return store.db.prepare('SELECT kind, n FROM counter ORDER BY kind').all()
}

describe('EventStore', () => {
  it('appends events forming a verifiable hash chain', () => {
    const store = open(tempDir())
    const first = store.append({ kind: 'kernel.started', payload: started, actor: OWNER })
    const second = store.append({ kind: 'kernel.stopped', payload: { reason: 'test' }, actor: OWNER, label: 'public' })
    expect(first.seq).toBe(1)
    expect(first.prevHash).toBe(GENESIS_HASH)
    expect(second.prevHash).toBe(first.hash)
    expect(store.head()).toBe(2)
    expect(store.list()).toEqual([first, second])
    expect(store.list({ kind: 'kernel.' })).toHaveLength(2)
    expect(store.list({ kind: 'kernel.stopped' })).toEqual([second])
    expect(store.verify()).toEqual({ ok: true, checked: 2, problems: [] })
  })

  it('detects tampered, removed and reordered events', () => {
    const store = open(tempDir())
    for (let i = 0; i < 5; i++) store.append({ kind: 'kernel.stopped', payload: { reason: `r${i}` }, actor: OWNER })

    store.db.prepare(`UPDATE events SET payload = '{"reason":"forged"}' WHERE seq = 2`).run()
    expect(store.verify().problems).toContainEqual({ code: 'EVENT_HASH_MISMATCH', params: { seq: 2 } })

    store.db.prepare('DELETE FROM events WHERE seq = 4').run()
    const codes = store.verify().problems.map((p) => p.code)
    expect(codes).toContain('EVENT_MISSING')
    expect(codes).toContain('EVENT_CHAIN_BROKEN')
    // Tail verification also checks the link to the event before the tail.
    expect(store.verify({ tail: 1 }).problems).toContainEqual({ code: 'EVENT_MISSING', params: { seq: 4 } })
  })

  it('rejects unknown kinds and invalid payloads without writing anything', () => {
    const store = open(tempDir())
    const listener = vi.fn()
    store.subscribe(listener)
    expect(() => store.append({ kind: 'no.such', payload: {}, actor: OWNER })).toThrow(KernelError)
    expect(() => store.append({ kind: 'kernel.stopped', payload: { reason: 1 }, actor: OWNER })).toThrow(/EVENT_PAYLOAD_INVALID|reason/)
    expect(() => store.append({ kind: 'kernel.stopped', payload: { reason: 'x', extra: 1 }, actor: OWNER })).toThrow(KernelError)
    expect(() => store.append({ kind: 'kernel.stopped', payload: { reason: 'x' }, actor: { kind: 'robot', id: 'r' } as never })).toThrow(
      KernelError,
    )
    expect(store.head()).toBe(0)
    expect(listener).not.toHaveBeenCalled()
  })

  it('commits a transaction atomically and notifies only after commit', () => {
    const store = open(tempDir())
    const seen: number[] = []
    store.subscribe((event) => seen.push(event.seq))
    store.subscribe(() => {
      throw new Error('a broken subscriber is ignored')
    })
    expect(() =>
      store.transaction(() => {
        store.append({ kind: 'kernel.stopped', payload: { reason: 'a' }, actor: OWNER })
        expect(seen).toEqual([])
        throw new Error('abort')
      }),
    ).toThrow('abort')
    expect(store.head()).toBe(0)
    expect(seen).toEqual([])

    store.transaction(() => {
      store.append({ kind: 'kernel.stopped', payload: { reason: 'a' }, actor: OWNER })
      store.append({ kind: 'kernel.stopped', payload: { reason: 'b' }, actor: OWNER })
    })
    expect(seen).toEqual([1, 2])
  })

  it('redacts payloads before hashing and storing them', () => {
    const store = open(tempDir(), { redact: (payload) => JSON.parse(JSON.stringify(payload).replaceAll('hunter2', '[REDACTED]')) })
    const event = store.append({ kind: 'kernel.stopped', payload: { reason: 'password hunter2' }, actor: OWNER })
    expect(event.payload).toEqual({ reason: 'password [REDACTED]' })
    expect(JSON.stringify(store.db.prepare('SELECT * FROM events').all())).not.toContain('hunter2')
    expect(store.verify().ok).toBe(true)
  })

  it('rebuilds a projection from the log with identical state', () => {
    const store = open(tempDir())
    store.registerProjection(counter())
    store.append({ kind: 'kernel.started', payload: started, actor: OWNER })
    store.append({ kind: 'kernel.stopped', payload: { reason: 'a' }, actor: OWNER })
    store.append({ kind: 'kernel.stopped', payload: { reason: 'b' }, actor: OWNER })
    const before = counts(store)
    expect(before).toEqual([
      { kind: 'kernel.started', n: 1 },
      { kind: 'kernel.stopped', n: 2 },
    ])
    store.db.exec('DROP TABLE counter')
    expect(store.rebuildProjections()).toEqual([{ projection: 'counter', version: 1, events: 3 }])
    expect(counts(store)).toEqual(before)
  })

  it('catches a projection up from its checkpoint and rebuilds it on a version change', () => {
    const dir = tempDir()
    const first = open(dir)
    first.registerProjection(counter())
    first.append({ kind: 'kernel.stopped', payload: { reason: 'a' }, actor: OWNER })
    first.close()

    // Events appended while the projection is not loaded.
    const second = open(dir)
    second.append({ kind: 'kernel.stopped', payload: { reason: 'b' }, actor: OWNER })
    second.append({ kind: 'kernel.stopped', payload: { reason: 'c' }, actor: OWNER })
    second.close()

    const third = open(dir)
    const projection = counter()
    third.registerProjection(projection)
    expect(projection.calls).toBe(2)
    expect(counts(third)).toEqual([{ kind: 'kernel.stopped', n: 3 }])
    third.close()

    const fourth = open(dir)
    const upgraded = counter(2)
    fourth.registerProjection(upgraded)
    expect(upgraded.calls).toBe(3)
    expect(counts(fourth)).toEqual([{ kind: 'kernel.stopped', n: 3 }])
  })
})
