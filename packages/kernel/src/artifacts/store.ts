import { createHash, randomUUID } from 'node:crypto'
import { createReadStream, createWriteStream, existsSync, mkdirSync, readdirSync, rmSync, statSync } from 'node:fs'
import { chmod, open, rename, rm, stat } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { pipeline } from 'node:stream/promises'
import type { Readable } from 'node:stream'
import type { DatabaseSync } from 'node:sqlite'
import {
  maxLabel,
  type Actor,
  type ArtifactMeta,
  type EventRecord,
  type IntegrityReport,
  type Label,
  type LineageEdge,
  type LineageResult,
  type Message,
} from '@llmchip/kernel-api'
import type { EventStore, Projection } from '../events/store.ts'
import { KernelError } from '../util/errors.ts'

export interface PutOptions {
  type: string
  mediaType?: string
  /** Requested label; raised to the most sensitive input label (requirements FR-SEC-02). */
  label?: Label
  inputs?: string[]
  run?: string | null
  creator: Actor
  reqRef?: string | null
  project?: string | null
}

export type ArtifactSource = Uint8Array | string | Readable | AsyncIterable<Uint8Array>

/**
 * Projection of `artifact.*` events: the artifact catalog and the lineage
 * edges. Rebuildable from the event log like any projection.
 */
export const artifactsProjection: Projection = {
  name: 'artifacts',
  version: 1,
  setup(db) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS artifacts (
        hash TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        deleted INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        meta TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS artifacts_type ON artifacts (type);
      CREATE TABLE IF NOT EXISTS artifact_inputs (
        hash TEXT NOT NULL,
        input TEXT NOT NULL,
        PRIMARY KEY (hash, input)
      );
      CREATE INDEX IF NOT EXISTS artifact_inputs_input ON artifact_inputs (input);
    `)
  },
  reset(db) {
    db.exec('DROP TABLE IF EXISTS artifacts; DROP TABLE IF EXISTS artifact_inputs;')
  },
  apply(event: EventRecord, db: DatabaseSync) {
    if (event.kind === 'artifact.created') {
      const meta = (event.payload as { artifact: ArtifactMeta }).artifact
      db.prepare('INSERT OR IGNORE INTO artifacts (hash, type, deleted, seq, meta) VALUES (?, ?, 0, ?, ?)').run(
        meta.hash,
        meta.type,
        event.seq,
        JSON.stringify(meta),
      )
      const edge = db.prepare('INSERT OR IGNORE INTO artifact_inputs (hash, input) VALUES (?, ?)')
      for (const input of meta.inputs) edge.run(meta.hash, input)
    } else if (event.kind === 'artifact.deletion_marked') {
      const { hash, reason } = event.payload as { hash: string; reason: string }
      const row = db.prepare('SELECT meta FROM artifacts WHERE hash = ?').get(hash) as { meta: string } | undefined
      if (!row) return
      const meta: ArtifactMeta = { ...JSON.parse(row.meta), deleted: { by: event.actor, at: event.ts, reason } }
      db.prepare('UPDATE artifacts SET deleted = 1, meta = ? WHERE hash = ?').run(JSON.stringify(meta), hash)
    }
  },
}

/**
 * K2: the content-addressed artifact store (architecture §4.2). Content is
 * written to a temporary file, fsynced, and renamed into `objects/` read-only
 * before the `artifact.created` event is appended, so a crash can never leave
 * a registered artifact with partial content.
 */
export class ArtifactStore {
  private readonly objectsDir: string
  private readonly tmpDir: string
  private readonly events: EventStore

  constructor(storeDir: string, events: EventStore) {
    this.events = events
    this.objectsDir = join(storeDir, 'objects')
    this.tmpDir = join(storeDir, 'tmp')
    mkdirSync(this.objectsDir, { recursive: true })
    mkdirSync(this.tmpDir, { recursive: true })
    events.registerProjection(artifactsProjection)
  }

  /** Removes leftovers of writes interrupted by a crash. */
  cleanupTemp(): number {
    const names = readdirSync(this.tmpDir)
    for (const name of names) rmSync(join(this.tmpDir, name), { force: true })
    return names.length
  }

  objectPath(hash: string): string {
    return join(this.objectsDir, hash.slice(0, 2), hash.slice(2))
  }

  async put(source: ArtifactSource, options: PutOptions): Promise<ArtifactMeta> {
    const inputs = [...new Set(options.inputs ?? [])]
    const inputMetas = inputs.map((hash) => {
      const meta = this.stat(hash)
      if (!meta) throw new KernelError('ARTIFACT_INPUT_NOT_FOUND', `input artifact ${hash} does not exist`, { hash })
      return meta
    })

    const tmp = join(this.tmpDir, randomUUID())
    const hasher = createHash('sha256')
    let size = 0
    try {
      await pipeline(toStream(source), async function* (chunks: AsyncIterable<Uint8Array>) {
        for await (const chunk of chunks) {
          hasher.update(chunk)
          size += chunk.length
          yield chunk
        }
      }, createWriteStream(tmp, { flags: 'wx', mode: 0o600 }))
      await fsyncPath(tmp)
    } catch (error) {
      await rm(tmp, { force: true })
      throw error
    }

    const hash = hasher.digest('hex')
    const target = this.objectPath(hash)
    if (existsSync(target)) {
      await rm(tmp, { force: true })
    } else {
      mkdirSync(dirname(target), { recursive: true })
      await chmod(tmp, 0o444)
      await rename(tmp, target)
      await fsyncPath(dirname(target))
    }

    // Registration is synchronous, so the check and the append cannot interleave.
    const existing = this.stat(hash)
    if (existing) return existing
    const meta: ArtifactMeta = {
      hash,
      size,
      type: options.type,
      mediaType: options.mediaType ?? 'application/octet-stream',
      label: maxLabel([options.label ?? 'internal', ...inputMetas.map((m) => m.label)]),
      run: options.run ?? null,
      inputs,
      creator: options.creator,
      createdAt: new Date().toISOString(),
      reqRef: options.reqRef ?? null,
      deleted: null,
    }
    this.events.append({
      kind: 'artifact.created',
      payload: { artifact: meta },
      actor: options.creator,
      project: options.project ?? null,
      label: meta.label,
      reqRef: meta.reqRef,
    })
    return this.stat(hash)!
  }

  stat(hash: string): ArtifactMeta | undefined {
    const row = this.events.db.prepare('SELECT meta FROM artifacts WHERE hash = ?').get(hash) as { meta: string } | undefined
    return row ? JSON.parse(row.meta) : undefined
  }

  require(hash: string): ArtifactMeta {
    const meta = this.stat(hash)
    if (!meta) throw new KernelError('ARTIFACT_NOT_FOUND', `artifact ${hash} does not exist`, { hash })
    return meta
  }

  list(options: { type?: string; includeDeleted?: boolean; limit?: number } = {}): ArtifactMeta[] {
    const where: string[] = []
    const args: (string | number)[] = []
    if (options.type) {
      where.push('type = ?')
      args.push(options.type)
    }
    if (!options.includeDeleted) where.push('deleted = 0')
    args.push(options.limit ?? 100)
    const sql = `SELECT meta FROM artifacts ${where.length ? `WHERE ${where.join(' AND ')}` : ''} ORDER BY seq LIMIT ?`
    return (this.events.db.prepare(sql).all(...args) as { meta: string }[]).map((row) => JSON.parse(row.meta))
  }

  /** Opens the content of a registered artifact for reading. */
  read(hash: string): Readable {
    this.require(hash)
    return createReadStream(this.objectPath(hash))
  }

  /** The lineage graph reachable from `hash`, following inputs (`up`) or derivations (`down`). */
  lineage(hash: string, direction: 'up' | 'down' = 'up'): LineageResult {
    const nodes = new Map<string, ArtifactMeta>([[hash, this.require(hash)]])
    const edges: LineageEdge[] = []
    const queue = [hash]
    const next =
      direction === 'up'
        ? this.events.db.prepare('SELECT input AS other FROM artifact_inputs WHERE hash = ?')
        : this.events.db.prepare('SELECT hash AS other FROM artifact_inputs WHERE input = ?')
    while (queue.length > 0) {
      const current = queue.shift()!
      for (const { other } of next.all(current) as { other: string }[]) {
        const [from, to] = direction === 'up' ? [other, current] : [current, other]
        edges.push({ from, to, run: nodes.get(to)?.run ?? this.stat(to)?.run ?? null })
        if (!nodes.has(other)) {
          const meta = this.stat(other)
          if (!meta) continue
          nodes.set(other, meta)
          queue.push(other)
        }
      }
    }
    return { nodes: [...nodes.values()], edges }
  }

  /** Marks an artifact deleted. Content stays in place; only a human purge removes it (FR-AGT-20). */
  markDeleted(hash: string, reason: string, by: Actor): ArtifactMeta {
    const meta = this.require(hash)
    if (meta.deleted) return meta
    this.events.append({
      kind: 'artifact.deletion_marked',
      payload: { hash, reason },
      actor: by,
      label: meta.label,
    })
    return this.require(hash)
  }

  /** Checks that every registered artifact has intact, read-only content. */
  async verify(): Promise<IntegrityReport> {
    const problems: Message[] = []
    let checked = 0
    for (const { meta } of this.events.db.prepare('SELECT meta FROM artifacts ORDER BY seq').all() as { meta: string }[]) {
      const { hash, size } = JSON.parse(meta) as ArtifactMeta
      checked++
      const path = this.objectPath(hash)
      if (!existsSync(path)) {
        problems.push({ code: 'ARTIFACT_CONTENT_MISSING', params: { hash } })
        continue
      }
      const info = statSync(path)
      if (info.size !== size) problems.push({ code: 'ARTIFACT_SIZE_MISMATCH', params: { hash } })
      if (info.mode & 0o222) problems.push({ code: 'ARTIFACT_WRITABLE', params: { hash } })
      if ((await hashFile(path)) !== hash) problems.push({ code: 'ARTIFACT_HASH_MISMATCH', params: { hash } })
    }
    return { ok: problems.length === 0, checked, problems }
  }
}

function toStream(source: ArtifactSource): AsyncIterable<Uint8Array> {
  if (typeof source === 'string') return toStream(new TextEncoder().encode(source))
  if (source instanceof Uint8Array) {
    return (async function* () {
      yield source
    })()
  }
  return source as AsyncIterable<Uint8Array>
}

async function fsyncPath(path: string): Promise<void> {
  const handle = await open(path, (await stat(path)).isDirectory() ? 'r' : 'r+')
  try {
    await handle.sync()
  } finally {
    await handle.close()
  }
}

async function hashFile(path: string): Promise<string> {
  const hasher = createHash('sha256')
  for await (const chunk of createReadStream(path)) hasher.update(chunk as Buffer)
  return hasher.digest('hex')
}
