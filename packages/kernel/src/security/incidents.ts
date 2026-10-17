import type { Actor, IncidentRecord, Message } from '@llmchip/kernel-api'
import type { EventStore, Projection } from '../events/store.ts'
import { KernelError } from '../util/errors.ts'
import { ulid } from '../util/ulid.ts'

/** Projection of incidents (requirements §3.10). */
export const incidentsProjection: Projection = {
  name: 'incidents',
  version: 1,
  setup(db) {
    db.exec('CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, seq INTEGER NOT NULL, kind TEXT NOT NULL, open INTEGER NOT NULL, record TEXT NOT NULL)')
  },
  reset(db) {
    db.exec('DROP TABLE IF EXISTS incidents')
  },
  apply(event, db) {
    if (event.kind === 'incident.opened') {
      const incident = (event.payload as { incident: IncidentRecord }).incident
      db.prepare('INSERT INTO incidents (id, seq, kind, open, record) VALUES (?, ?, ?, 1, ?)').run(incident.id, event.seq, incident.kind, JSON.stringify(incident))
    } else if (event.kind === 'incident.closed') {
      const { incident: id, cause, action } = event.payload as { incident: string; cause: string; action: string }
      const row = db.prepare('SELECT record FROM incidents WHERE id = ?').get(id) as { record: string } | undefined
      if (!row) return
      const incident: IncidentRecord = { ...JSON.parse(row.record), closed: { by: event.actor, at: event.ts, cause, action } }
      db.prepare('UPDATE incidents SET open = 0, record = ? WHERE id = ?').run(JSON.stringify(incident), id)
    }
  },
}

export interface OpenIncident {
  kind: string
  severity?: IncidentRecord['severity']
  summary: Message
  actor: Actor
  project?: string | null
  refs?: IncidentRecord['refs']
}

/** Records incidents and answers queries about them. */
export class Incidents {
  private readonly events: EventStore

  constructor(events: EventStore) {
    this.events = events
    events.registerProjection(incidentsProjection)
  }

  open(input: OpenIncident): IncidentRecord {
    const incident: IncidentRecord = {
      id: ulid(),
      kind: input.kind,
      severity: input.severity ?? 'warning',
      summary: input.summary,
      project: input.project ?? null,
      actor: input.actor,
      refs: input.refs ?? {},
      openedAt: new Date().toISOString(),
      closed: null,
    }
    this.events.append({ kind: 'incident.opened', payload: { incident }, actor: { kind: 'kernel', id: 'incidents' }, project: incident.project })
    return incident
  }

  /** Closing requires the cause and the action taken (FR-AGT-27). */
  close(id: string, cause: string, action: string, actor: Actor): IncidentRecord {
    if (actor.kind !== 'human') throw new KernelError('INCIDENT_CLOSE_REQUIRES_HUMAN', 'only a human can close an incident')
    this.get(id)
    this.events.append({ kind: 'incident.closed', payload: { incident: id, cause, action }, actor })
    return this.get(id)
  }

  get(id: string): IncidentRecord {
    const row = this.events.db.prepare('SELECT record FROM incidents WHERE id = ?').get(id) as { record: string } | undefined
    if (!row) throw new KernelError('INCIDENT_NOT_FOUND', `incident ${id} does not exist`, { incident: id })
    return JSON.parse(row.record)
  }

  list(options: { open?: boolean; kind?: string; limit?: number; project?: string } = {}): IncidentRecord[] {
    const where: string[] = []
    const args: (string | number)[] = []
    if (options.open !== undefined) {
      where.push('open = ?')
      args.push(options.open ? 1 : 0)
    }
    if (options.kind) {
      where.push('kind = ?')
      args.push(options.kind)
    }
    args.push(options.limit ?? 100)
    const sql = `SELECT record FROM incidents ${where.length ? `WHERE ${where.join(' AND ')}` : ''} ORDER BY seq DESC LIMIT ?`
    const records = (this.events.db.prepare(sql).all(...args) as { record: string }[]).map((r) => JSON.parse(r.record) as IncidentRecord)
    return options.project ? records.filter((r) => r.project === options.project) : records
  }
}
