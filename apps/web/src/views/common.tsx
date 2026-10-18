import type { ReactNode } from 'react'
import type { EventRecord } from '@llmchip/kernel-api'
import { useI18n } from '../i18n.ts'

export function timeOf(ts: string): string {
  return new Date(ts).toLocaleTimeString()
}

export function short(hash: string | null | undefined): string {
  return hash ? hash.slice(0, 10) : ''
}

/** One-line description of an event for timelines. */
export function eventLine(e: EventRecord): string {
  const p = e.payload as Record<string, any>
  switch (e.kind) {
    case 'artifact.created':
      return `artifact ${p.artifact.type} ${short(p.artifact.hash)} [${p.artifact.label}]`
    case 'run.queued':
      return `run ${p.run.plugin}/${p.run.handler} queued`
    case 'run.finished':
      return `run ${short(p.run)} ${p.execution}`
    case 'verdict.recorded':
      return `verdict ${p.verdict.check}: ${p.verdict.verdict}`
    case 'agent.step':
      return `agent step ${p.step}: ${(p.toolCalls as { name: string }[]).map((c) => c.name).join(', ') || 'text'}`
    case 'incident.opened':
      return `incident ${p.incident.kind}`
    case 'egress.decided':
      return `egress ${p.destination.id}: ${p.decision}`
    case 'approval.recorded':
      return `approval ${p.approval.gate}: ${p.approval.decision}`
    case 'task.state':
      return `task ${short(p.task)} ${p.state}`
    default:
      return e.kind
  }
}

export function Section({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      <div className="card-head">
        <h2>{title}</h2>
        <div>{actions}</div>
      </div>
      {children}
    </section>
  )
}

export function ErrorLine({ error }: { error: string | null | undefined }) {
  const { t } = useI18n()
  return error ? <p className="error">{t('common.error')}{error}</p> : null
}

export function Label({ label }: { label: string }) {
  return <span className={`label ${label}`}>{label}</span>
}
