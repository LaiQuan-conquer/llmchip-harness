import { DatabaseSync } from 'node:sqlite'
import {
  KERNEL_EVENT_PAYLOAD_DEFS,
  type Actor,
  type EventRecord,
  type IntegrityReport,
  type Label,
  type Message,
  type ProjectionRebuiltPayload,
} from '@llmchip/kernel-api'
import { canonicalJson, sha256 } from '../util/canonical.ts'
import { KernelError } from '../util/errors.ts'
import { assertValid } from '../util/schema.ts'
import { ulid } from '../util/ulid.ts'

/** Hash that the first event's `prevHash` points at. */
export const GENESIS_HASH = '0'.repeat(64)

export interface AppendInput {
  kind: string
  payload: Record<string, unknown>
  actor: Actor
  project?: string | null
  label?: Label
  causation?: string | null
  correlation?: string | null
  reqRef?: string | null
}

/**
 * A read model derived from the event log (architecture AP-3). Projections own
 * their tables, are updated in the same transaction as the event they apply,
 * and can be dropped and rebuilt from the log at any time.
 */
export interface Projection {
  readonly name: string
  /** Bump when `setup` or `apply` change; the projection is then rebuilt. */
  readonly version: number
  /** Creates the projection's tables if they do not exist. */
  setup(db: DatabaseSync): void
  /** Removes all projection state. `setup` is called again afterwards. */
  reset(db: DatabaseSync): void
  apply(event: EventRecord, db: DatabaseSync): void
}

export interface EventStoreOptions {
  /** Applied to every payload before it is hashed and stored (secret redaction). */
  redact?: (payload: Record<string, unknown>) => Record<string, unknown>
}

type Listener = (event: EventRecord) => void

interface EventRow {
  seq: number
  id: string
  ts: string
  project: string | null
  actor: string
  kind: string
  payload: string
  label: string
  causation: string | null
  correlation: string | null
  req_ref: string | null
  prev_hash: string
  hash: string
}

/**
 * K1: the append-only event log, the single source of truth (architecture
 * §4.1). SQLite in WAL mode with synchronous=FULL, so a committed event
 * survives a crash or power loss. Every event carries the hash of its
 * predecessor, forming a chain that `verify` checks.
 */
export class EventStore {
  readonly db: DatabaseSync
  private readonly kinds = new Map<string, string>()
  private readonly projections: Projection[] = []
  private readonly listeners = new Set<Listener>()
  private readonly redact: (payload: Record<string, unknown>) => Record<string, unknown>
  private inTransaction = false
  private pending: EventRecord[] = []

  constructor(path: string, options: EventStoreOptions = {}) {
    this.redact = options.redact ?? ((payload) => payload)
    this.db = new DatabaseSync(path)
    this.db.exec(`
      PRAGMA journal_mode = WAL;
      PRAGMA synchronous = FULL;
      PRAGMA foreign_keys = ON;
      CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY,
        id TEXT NOT NULL UNIQUE,
        ts TEXT NOT NULL,
        project TEXT,
        actor TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        label TEXT NOT NULL,
        causation TEXT,
        correlation TEXT,
        req_ref TEXT,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS events_kind ON events (kind);
      CREATE INDEX IF NOT EXISTS events_project ON events (project);
      CREATE TABLE IF NOT EXISTS projections (
        name TEXT PRIMARY KEY,
        version INTEGER NOT NULL,
        seq INTEGER NOT NULL
      );
    `)
    for (const [kind, def] of Object.entries(KERNEL_EVENT_PAYLOAD_DEFS)) this.registerKind(kind, def)
  }

  /** Registers an event kind and the kernel schema definition its payload must satisfy. */
  registerKind(kind: string, payloadDef: string): void {
    if (this.kinds.has(kind) && this.kinds.get(kind) !== payloadDef) {
      throw new KernelError('EVENT_KIND_CONFLICT', `event kind ${kind} is already registered`, { kind })
    }
    this.kinds.set(kind, payloadDef)
  }

  /**
   * Registers a projection. A projection that is new, or whose version
   * changed, is rebuilt from the whole log; otherwise it catches up from its
   * checkpoint.
   */
  registerProjection(projection: Projection): void {
    if (this.projections.some((p) => p.name === projection.name)) {
      throw new Error(`projection ${projection.name} is already registered`)
    }
    this.projections.push(projection)
    projection.setup(this.db)
    const row = this.db.prepare('SELECT version, seq FROM projections WHERE name = ?').get(projection.name) as
      | { version: number; seq: number }
      | undefined
    if (!row || row.version !== projection.version) this.rebuild(projection)
    else if (row.seq < this.head()) this.catchUp(projection, row.seq)
  }

  head(): number {
    const row = this.db.prepare('SELECT MAX(seq) AS seq FROM events').get() as { seq: number | null }
    return row.seq ?? 0
  }

  /** Appends one event, applying it to every projection in the same transaction. */
  append(input: AppendInput): EventRecord {
    return this.transaction(() => this.appendInTransaction(input))
  }

  /**
   * Runs `fn` in one write transaction. Events appended inside it, and any
   * projection writes, commit or roll back together; listeners are notified
   * only after commit.
   */
  transaction<T>(fn: () => T): T {
    if (this.inTransaction) return fn()
    this.db.exec('BEGIN IMMEDIATE')
    this.inTransaction = true
    let result: T
    try {
      result = fn()
      this.db.exec('COMMIT')
    } catch (error) {
      this.db.exec('ROLLBACK')
      this.pending = []
      throw error
    } finally {
      this.inTransaction = false
    }
    const committed = this.pending
    this.pending = []
    for (const event of committed) this.notify(event)
    return result
  }

  private appendInTransaction(input: AppendInput): EventRecord {
    const def = this.kinds.get(input.kind)
    if (!def) throw new KernelError('EVENT_KIND_UNKNOWN', `unknown event kind ${input.kind}`, { kind: input.kind })
    const payload = this.redact(input.payload)
    assertValid(def, payload, 'EVENT_PAYLOAD_INVALID')
    assertValid('Actor', input.actor, 'EVENT_ACTOR_INVALID')

    const last = this.db.prepare('SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1').get() as
      | { seq: number; hash: string }
      | undefined
    const unhashed = {
      seq: (last?.seq ?? 0) + 1,
      id: ulid(),
      ts: new Date().toISOString(),
      project: input.project ?? null,
      actor: input.actor,
      kind: input.kind,
      payload,
      label: input.label ?? 'internal',
      causation: input.causation ?? null,
      correlation: input.correlation ?? null,
      reqRef: input.reqRef ?? null,
      prevHash: last?.hash ?? GENESIS_HASH,
    }
    const event: EventRecord = { ...unhashed, hash: eventHash(unhashed) }
    this.db
      .prepare(
        `INSERT INTO events (seq, id, ts, project, actor, kind, payload, label, causation, correlation, req_ref, prev_hash, hash)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        event.seq,
        event.id,
        event.ts,
        event.project,
        JSON.stringify(event.actor),
        event.kind,
        JSON.stringify(event.payload),
        event.label,
        event.causation,
        event.correlation,
        event.reqRef,
        event.prevHash,
        event.hash,
      )
    const checkpoint = this.db.prepare('UPDATE projections SET seq = ? WHERE name = ?')
    for (const projection of this.projections) {
      projection.apply(event, this.db)
      checkpoint.run(event.seq, projection.name)
    }
    this.pending.push(event)
    return event
  }

  list(options: { since?: number; limit?: number; kind?: string; project?: string } = {}): EventRecord[] {
    const where = ['seq > ?']
    const args: (string | number)[] = [options.since ?? 0]
    if (options.kind) {
      if (options.kind.endsWith('.')) {
        where.push('substr(kind, 1, ?) = ?')
        args.push(options.kind.length, options.kind)
      } else {
        where.push('kind = ?')
        args.push(options.kind)
      }
    }
    if (options.project) {
      where.push('project = ?')
      args.push(options.project)
    }
    args.push(options.limit ?? 100)
    const rows = this.db
      .prepare(`SELECT * FROM events WHERE ${where.join(' AND ')} ORDER BY seq LIMIT ?`)
      .all(...args) as unknown as EventRow[]
    return rows.map(fromRow)
  }

  /** Calls `listener` after every committed event. Returns the unsubscribe function. */
  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /**
   * Checks the hash chain. With `tail`, only the most recent `tail` events
   * are checked (against their stored predecessor hash).
   */
  verify(options: { tail?: number } = {}): IntegrityReport {
    const head = this.head()
    const from = options.tail === undefined ? 1 : Math.max(1, head - options.tail + 1)
    const problems: Message[] = []
    let prevHash = GENESIS_HASH
    if (from > 1) {
      const prev = this.db.prepare('SELECT hash FROM events WHERE seq = ?').get(from - 1) as { hash: string } | undefined
      if (!prev) problems.push({ code: 'EVENT_MISSING', params: { seq: from - 1 } })
      prevHash = prev?.hash ?? ''
    }
    let expected = from
    let checked = 0
    for (const row of this.db.prepare('SELECT * FROM events WHERE seq >= ? ORDER BY seq').iterate(from) as Iterable<EventRow>) {
      const event = fromRow(row)
      if (event.seq !== expected) problems.push({ code: 'EVENT_MISSING', params: { seq: expected } })
      if (event.prevHash !== prevHash) problems.push({ code: 'EVENT_CHAIN_BROKEN', params: { seq: event.seq } })
      const { hash, ...unhashed } = event
      if (eventHash(unhashed) !== hash) problems.push({ code: 'EVENT_HASH_MISMATCH', params: { seq: event.seq } })
      prevHash = hash
      expected = event.seq + 1
      checked++
      if (problems.length >= 100) break
    }
    return { ok: problems.length === 0, checked, problems }
  }

  /** Drops and rebuilds the named projections (all when omitted) from the log. */
  rebuildProjections(names?: string[]): ProjectionRebuiltPayload[] {
    const selected = names ? this.projections.filter((p) => names.includes(p.name)) : this.projections
    return selected.map((projection) => this.rebuild(projection))
  }

  close(): void {
    this.listeners.clear()
    if (this.db.isOpen) this.db.close()
  }

  private rebuild(projection: Projection): ProjectionRebuiltPayload {
    let events = 0
    this.db.exec('BEGIN IMMEDIATE')
    try {
      projection.reset(this.db)
      projection.setup(this.db)
      let seq = 0
      for (const row of this.db.prepare('SELECT * FROM events ORDER BY seq').iterate() as Iterable<EventRow>) {
        const event = fromRow(row)
        projection.apply(event, this.db)
        seq = event.seq
        events++
      }
      this.db
        .prepare('INSERT INTO projections (name, version, seq) VALUES (?, ?, ?) ON CONFLICT (name) DO UPDATE SET version = excluded.version, seq = excluded.seq')
        .run(projection.name, projection.version, seq)
      this.db.exec('COMMIT')
    } catch (error) {
      this.db.exec('ROLLBACK')
      throw error
    }
    return { projection: projection.name, version: projection.version, events }
  }

  private catchUp(projection: Projection, since: number): void {
    this.db.exec('BEGIN IMMEDIATE')
    try {
      let seq = since
      for (const row of this.db.prepare('SELECT * FROM events WHERE seq > ? ORDER BY seq').iterate(since) as Iterable<EventRow>) {
        const event = fromRow(row)
        projection.apply(event, this.db)
        seq = event.seq
      }
      this.db.prepare('UPDATE projections SET seq = ? WHERE name = ?').run(seq, projection.name)
      this.db.exec('COMMIT')
    } catch (error) {
      this.db.exec('ROLLBACK')
      throw error
    }
  }

  private notify(event: EventRecord): void {
    for (const listener of this.listeners) {
      try {
        listener(event)
      } catch {
        // A failing subscriber must not affect the log or other subscribers.
      }
    }
  }
}

export function eventHash(event: Omit<EventRecord, 'hash'>): string {
  return sha256(canonicalJson(event))
}

function fromRow(row: EventRow): EventRecord {
  return {
    seq: row.seq,
    id: row.id,
    ts: row.ts,
    project: row.project,
    actor: JSON.parse(row.actor),
    kind: row.kind,
    payload: JSON.parse(row.payload),
    label: row.label as Label,
    causation: row.causation,
    correlation: row.correlation,
    reqRef: row.req_ref,
    prevHash: row.prev_hash,
    hash: row.hash,
  }
}
