import type { DatabaseSync } from 'node:sqlite'
import type { ConstraintProposal, ProjectRecord, VerdictRecord } from '@llmchip/kernel-api'
import type { Projection } from '../events/store.ts'

/**
 * Projection of the workflow events (K3, K6): projects, branches, slot
 * bindings with their history (for branching at a checkpoint), entered
 * stages, verdicts and constraint proposals. Version 3 dropped approvals and
 * waivers (owner decision 2026-10-24); their events in older logs are ignored.
 */
export const workflowProjection: Projection = {
  name: 'workflow',
  version: 3,
  setup(db) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, record TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS branches (project TEXT NOT NULL, branch TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY (project, branch));
      CREATE TABLE IF NOT EXISTS slot_history (project TEXT NOT NULL, branch TEXT NOT NULL, slot TEXT NOT NULL, hash TEXT NOT NULL, seq INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS slot_history_key ON slot_history (project, branch, slot, seq);
      CREATE TABLE IF NOT EXISTS stages (project TEXT NOT NULL, branch TEXT NOT NULL, stage TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY (project, branch, stage));
      CREATE TABLE IF NOT EXISTS verdicts (id TEXT PRIMARY KEY, check_id TEXT NOT NULL, seq INTEGER NOT NULL, record TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS verdicts_check ON verdicts (check_id, seq);
      CREATE TABLE IF NOT EXISTS constraint_proposals (id TEXT PRIMARY KEY, project TEXT NOT NULL, check_id TEXT NOT NULL, status TEXT NOT NULL, seq INTEGER NOT NULL, record TEXT NOT NULL);
    `)
  },
  reset(db) {
    db.exec(`
      DROP TABLE IF EXISTS projects; DROP TABLE IF EXISTS branches; DROP TABLE IF EXISTS slot_history;
      DROP TABLE IF EXISTS stages; DROP TABLE IF EXISTS verdicts; DROP TABLE IF EXISTS approvals; DROP TABLE IF EXISTS waivers; DROP TABLE IF EXISTS constraint_proposals;
    `)
  },
  apply(event, db) {
    const p = event.payload as Record<string, any>
    switch (event.kind) {
      case 'project.created': {
        const project = p.project as ProjectRecord
        db.prepare('INSERT INTO projects (id, record) VALUES (?, ?)').run(project.id, JSON.stringify(project))
        db.prepare('INSERT INTO branches (project, branch, seq) VALUES (?, ?, ?)').run(project.id, 'main', event.seq)
        break
      }
      case 'project.branched': {
        db.prepare('INSERT INTO branches (project, branch, seq) VALUES (?, ?, ?)').run(p.project, p.branch, event.seq)
        // The new branch starts with the source branch's bindings as of the checkpoint.
        for (const { slot, hash } of slotsAt(db, p.project, p.from, p.atSeq)) {
          db.prepare('INSERT INTO slot_history (project, branch, slot, hash, seq) VALUES (?, ?, ?, ?, ?)').run(p.project, p.branch, slot, hash, event.seq)
        }
        break
      }
      case 'project.slot_bound':
        db.prepare('INSERT INTO slot_history (project, branch, slot, hash, seq) VALUES (?, ?, ?, ?, ?)').run(p.project, p.branch, p.slot, p.hash, event.seq)
        break
      case 'stage.entered':
        db.prepare('INSERT OR IGNORE INTO stages (project, branch, stage, seq) VALUES (?, ?, ?, ?)').run(p.project, p.branch, p.stage, event.seq)
        break
      case 'verdict.recorded': {
        const verdict = p.verdict as VerdictRecord
        db.prepare('INSERT INTO verdicts (id, check_id, seq, record) VALUES (?, ?, ?, ?)').run(verdict.id, verdict.check, event.seq, JSON.stringify(verdict))
        break
      }
      case 'constraint.proposed': {
        const proposal = p.proposal as ConstraintProposal
        db.prepare('INSERT INTO constraint_proposals (id, project, check_id, status, seq, record) VALUES (?, ?, ?, ?, ?, ?)').run(
          proposal.id,
          proposal.project,
          proposal.check,
          proposal.status,
          event.seq,
          JSON.stringify(proposal),
        )
        break
      }
      case 'constraint.decided': {
        const row = db.prepare('SELECT record FROM constraint_proposals WHERE id = ?').get(p.proposal) as { record: string } | undefined
        if (!row) break
        const proposal = { ...JSON.parse(row.record), status: p.decision } as ConstraintProposal
        db.prepare('UPDATE constraint_proposals SET status = ?, seq = ?, record = ? WHERE id = ?').run(p.decision, event.seq, JSON.stringify(proposal), proposal.id)
        break
      }
    }
  },
}

/** Slot bindings of a branch as of event `seq` (inclusive); latest binding per slot. */
export function slotsAt(db: DatabaseSync, project: string, branch: string, seq = Number.MAX_SAFE_INTEGER): { slot: string; hash: string }[] {
  return db
    .prepare(
      `SELECT slot, hash FROM slot_history h WHERE project = ? AND branch = ? AND seq <= ?
       AND seq = (SELECT MAX(seq) FROM slot_history WHERE project = h.project AND branch = h.branch AND slot = h.slot AND seq <= ?)
       ORDER BY slot`,
    )
    .all(project, branch, seq, seq) as { slot: string; hash: string }[]
}
