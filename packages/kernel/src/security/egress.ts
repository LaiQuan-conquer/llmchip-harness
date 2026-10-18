import { readFileSync, statSync } from 'node:fs'
import {
  LABELS,
  type Actor,
  type ArtifactMeta,
  type DeclassRule,
  type EgressDestination,
  type EgressSegment,
  type Label,
  type Message,
} from '@llmchip/kernel-api'
import type { ArtifactStore } from '../artifacts/store.ts'
import type { EventStore, Projection } from '../events/store.ts'
import { sha256 } from '../util/canonical.ts'
import { KernelError } from '../util/errors.ts'
import type { Incidents } from './incidents.ts'

/** A piece of outbound content with its data label. */
export interface Segment {
  label: Label
  /** Where it came from: an artifact hash, `system-prompt`, `tool-result:<id>`, ... */
  source: string
  text: string
}

/** Largest NDA file scanned for fingerprints. */
const MAX_FINGERPRINT_BYTES = 16 * 1024 * 1024
/** Fingerprints extracted per NDA artifact. */
const MAX_FINGERPRINTS = 5000

/**
 * Distinctive tokens of an NDA document (FR-SEC-06): identifiers that look
 * like layer, rule or cell names (mixing letters with digits, underscores or
 * dots, at least 6 characters), rare in ordinary prose.
 */
export function extractFingerprints(text: string): string[] {
  const found = new Set<string>()
  for (const match of text.matchAll(/[A-Za-z][A-Za-z0-9_.$-]{5,63}/g)) {
    const token = match[0].replace(/[.$-]+$/, '')
    if (token.length < 6) continue
    if (!/[0-9_]/.test(token) && !/[a-z][A-Z]/.test(token)) continue
    found.add(token)
    if (found.size >= MAX_FINGERPRINTS) break
  }
  return [...found]
}

/**
 * Fingerprint index of NDA artifacts. A projection, so it is rebuilt from the
 * log like everything else; it reads artifact content, which is immutable and
 * stored before the event that registers it.
 */
export function fingerprintProjection(objectPath: (hash: string) => string): Projection {
  return {
    name: 'fingerprints',
    version: 1,
    setup(db) {
      db.exec('CREATE TABLE IF NOT EXISTS fingerprints (token TEXT NOT NULL, artifact TEXT NOT NULL, PRIMARY KEY (token, artifact))')
    },
    reset(db) {
      db.exec('DROP TABLE IF EXISTS fingerprints')
    },
    apply(event, db) {
      if (event.kind !== 'artifact.created') return
      const meta = (event.payload as { artifact: ArtifactMeta }).artifact
      // Only originals: derived NDA artifacts share their sources' tokens.
      if (meta.label !== 'nda' || meta.inputs.length > 0 || meta.size > MAX_FINGERPRINT_BYTES) return
      let text: string
      try {
        if (statSync(objectPath(meta.hash)).size > MAX_FINGERPRINT_BYTES) return
        text = readFileSync(objectPath(meta.hash), 'latin1')
      } catch {
        return
      }
      const insert = db.prepare('INSERT OR IGNORE INTO fingerprints (token, artifact) VALUES (?, ?)')
      for (const token of extractFingerprints(text)) insert.run(token, meta.hash)
    },
  }
}

/** Projection of declassification rules. */
export const declassProjection: Projection = {
  name: 'declass',
  version: 1,
  setup(db) {
    db.exec('CREATE TABLE IF NOT EXISTS declass_rules (id TEXT PRIMARY KEY, record TEXT NOT NULL)')
  },
  reset(db) {
    db.exec('DROP TABLE IF EXISTS declass_rules')
  },
  apply(event, db) {
    if (event.kind !== 'declass.rule_set') return
    const rule = (event.payload as { rule: DeclassRule }).rule
    db.prepare('INSERT INTO declass_rules (id, record) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET record = excluded.record').run(rule.id, JSON.stringify(rule))
  },
}

export class EgressBlockedError extends KernelError {
  readonly incident: string

  constructor(reasons: Message[], incident: string) {
    super('EGRESS_BLOCKED', `outbound content blocked: ${reasons.map((r) => r.code).join(', ')}`, { reasons, incident })
    this.incident = incident
  }
}

/** Highest label a destination may receive by default (FR-SEC-03). */
export function defaultMaxLabel(location: EgressDestination['location']): Label {
  return location === 'local' ? 'nda' : 'internal'
}

/**
 * K8: the single egress gateway (architecture §4.8, ADR-008). Every outbound
 * request, to an LLM provider, an MCP client or any network destination,
 * passes `check` with its content segments. A segment whose label exceeds the
 * destination's maximum, or any content containing an NDA fingerprint, is
 * blocked and recorded as an incident. NDA content can never be released to a
 * cloud destination: no approval overrides this (FR-SEC-05).
 */
export class EgressGateway {
  private readonly events: EventStore
  private readonly artifacts: ArtifactStore
  private readonly incidents: Incidents
  /**
   * Fingerprint hits per segment content hash. Agent conversations resend the
   * same messages on every step; without the cache each step would rescan the
   * whole conversation and block the event loop. Cleared when NDA data arrives.
   */
  private readonly scanCache = new Map<string, string[]>()

  constructor(events: EventStore, artifacts: ArtifactStore, incidents: Incidents) {
    this.events = events
    this.artifacts = artifacts
    this.incidents = incidents
    events.registerProjection(fingerprintProjection((hash) => artifacts.objectPath(hash)))
    events.registerProjection(declassProjection)
    events.subscribe((event) => {
      if (event.kind === 'artifact.created' && event.label === 'nda') this.scanCache.clear()
    })
  }

  /**
   * Checks outbound content and records the decision. Throws
   * `EgressBlockedError` when the content may not leave.
   */
  check(destination: EgressDestination, segments: Segment[], actor: Actor, project?: string | null): void {
    const reasons: Message[] = []
    const max = LABELS.indexOf(destination.maxLabel)
    for (const segment of segments) {
      if (LABELS.indexOf(segment.label) > max) {
        reasons.push({ code: 'LABEL_EXCEEDS_DESTINATION', params: { source: segment.source, label: segment.label, destination: destination.id, max: destination.maxLabel } })
      }
    }
    const audit: EgressSegment[] = segments.map((s) => ({ label: s.label, source: s.source, hash: sha256(s.text) }))
    if (destination.location === 'cloud' || LABELS.indexOf(destination.maxLabel) < LABELS.indexOf('nda')) {
      const hits = new Set<string>()
      segments.forEach((segment, i) => {
        const key = audit[i]!.hash
        let found = this.scanCache.get(key)
        if (!found) {
          found = this.scan(segment.text)
          if (this.scanCache.size > 50_000) this.scanCache.clear()
          this.scanCache.set(key, found)
        }
        for (const hit of found) hits.add(hit)
      })
      if (hits.size > 0) reasons.push({ code: 'NDA_FINGERPRINT', params: { destination: destination.id, matches: [...hits].sort().slice(0, 5) } })
    }
    const bytes = segments.reduce((n, s) => n + Buffer.byteLength(s.text), 0)
    const decision = reasons.length === 0 ? 'allowed' : 'blocked'
    const label = segments.reduce<Label>((top, s) => (LABELS.indexOf(s.label) > LABELS.indexOf(top) ? s.label : top), 'public')
    this.events.append({ kind: 'egress.decided', payload: { destination, segments: audit, decision, reasons, bytes }, actor, project: project ?? null, label })
    if (decision === 'blocked') {
      const incident = this.incidents.open({
        kind: 'egress_blocked',
        severity: 'critical',
        summary: { code: 'EGRESS_BLOCKED', params: { destination: destination.id, reasons: reasons.map((r) => r.code) } },
        actor,
        project: project ?? null,
      })
      throw new EgressBlockedError(reasons, incident.id)
    }
  }

  /** NDA fingerprints contained in the text (first matches only). */
  scan(text: string): string[] {
    const tokens = extractFingerprints(text)
    if (tokens.length === 0) return []
    const hits: string[] = []
    const query = this.events.db.prepare('SELECT 1 FROM fingerprints WHERE token = ? LIMIT 1')
    for (const token of tokens) {
      if (query.get(token)) hits.push(token)
      if (hits.length >= 20) break
    }
    return hits
  }

  setRule(rule: DeclassRule, actor: Actor): void {
    if (actor.kind !== 'human') throw new KernelError('POLICY_REQUIRES_HUMAN', 'only a human can set a declassification rule')
    this.events.append({ kind: 'declass.rule_set', payload: { rule }, actor })
  }

  rules(): DeclassRule[] {
    return (this.events.db.prepare('SELECT record FROM declass_rules ORDER BY id').all() as { record: string }[]).map((r) => JSON.parse(r.record))
  }

  /**
   * Extracts exactly the fields a declassification rule names from a JSON
   * artifact into a new, lower-labelled artifact (FR-SEC-04). Free text never
   * passes: every extracted value must be a number, boolean or null.
   */
  async declassify(ruleId: string, hash: string, actor: Actor): Promise<ArtifactMeta> {
    const rule = this.rules().find((r) => r.id === ruleId)
    if (!rule) throw new KernelError('DECLASS_RULE_NOT_FOUND', `no declassification rule ${ruleId}`, { rule: ruleId })
    const source = this.artifacts.require(hash)
    if (source.type !== rule.sourceType) throw new KernelError('DECLASS_TYPE_MISMATCH', `rule ${ruleId} applies to ${rule.sourceType}, not ${source.type}`)
    let document: unknown
    try {
      document = JSON.parse(readFileSync(this.artifacts.objectPath(hash), 'utf8'))
    } catch {
      throw new KernelError('DECLASS_NOT_JSON', `artifact ${hash} is not JSON`)
    }
    const extracted: Record<string, unknown> = {}
    for (const pointer of rule.fields) {
      const value = resolvePointer(document, pointer)
      if (value !== null && value !== undefined && typeof value !== 'number' && typeof value !== 'boolean') {
        throw new KernelError('DECLASS_FIELD_NOT_SCALAR', `field ${pointer} is not a number, boolean or null`, { field: pointer })
      }
      extracted[pointer] = value ?? null
    }
    return this.artifacts.put(JSON.stringify({ rule: rule.id, source: hash, fields: extracted }, null, 2), {
      type: `${rule.sourceType}.declassified`,
      mediaType: 'application/json',
      label: rule.targetLabel,
      inputs: [hash],
      declassifiedBy: rule.id,
      creator: actor,
    })
  }
}

function resolvePointer(document: unknown, pointer: string): unknown {
  let current = document
  for (const part of pointer.slice(1).split('/')) {
    const key = part.replaceAll('~1', '/').replaceAll('~0', '~')
    if (current === null || typeof current !== 'object') return undefined
    current = (current as Record<string, unknown>)[key]
  }
  return current
}
