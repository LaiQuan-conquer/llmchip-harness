import { useState } from 'react'
import { useKernel, useQuery } from '../api.ts'
import { useI18n, type Key } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { ErrorLine, Section, short, timeOf } from './common.tsx'

/** Approval center (FR-UI-09): gates with their snapshot and evidence; decisions bind the snapshot. */
export function ApprovalsView({ selection }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t, message } = useI18n()
  const { client } = useKernel()
  const [comment, setComment] = useState('')
  const [error, setError] = useState<string | null>(null)
  const project = selection.project
  const { data } = useQuery(
    async (c) => {
      if (!project) return undefined
      const state = await c.call('projects.get', { project })
      const snapshots = await Promise.all(state.gates.map((g) => c.call('gates.snapshot', { project, gate: g.gate })))
      const { approvals } = await c.call('approvals.list', { project })
      return { state, snapshots, approvals }
    },
    [project],
  )
  if (!project) return <p className="muted">{t('overview.noProject')}</p>
  const decide = async (gate: string, decision: 'approved' | 'rejected') => {
    setError(null)
    try {
      await client.call('approvals.decide', { project, gate, decision, comment })
      setComment('')
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <>
      <ErrorLine error={error} />
      {data?.state.gates.map((g, i) => {
        const snapshot = data.snapshots[i]!
        return (
          <Section key={g.gate} title={`${t('approvals.gate')} ${g.gate}`}>
            <p>
              {t('approvals.status')}: <span className={g.released ? 'pill ok' : 'pill'}>{g.released ? t('approvals.released') : t('approvals.notReleased')}</span>{' '}
              {t(`status.${g.status}` as Key)} <span className="muted">{g.reasons.map(message).join('; ')}</span>
            </p>
            <p>
              {t('approvals.snapshot')}: <span className="mono">{short(snapshot.hash)}</span> ·{' '}
              {snapshot.members.map((m) => (
                <span key={m.slot} className="pill">
                  {m.slot}={short(m.hash)}
                </span>
              ))}
            </p>
            {snapshot.missing.length > 0 && (
              <p className="muted">
                {t('approvals.missing')}: {snapshot.missing.join(', ')}
              </p>
            )}
            <p>
              {t('approvals.evidence')}: {snapshot.evidence.length ? snapshot.evidence.map(short).join(', ') : '—'}
            </p>
            <div className="form inline">
              <input placeholder={t('approvals.comment')} value={comment} onChange={(e) => setComment(e.target.value)} />
              <button onClick={() => decide(g.gate, 'approved')}>{t('approvals.approve')}</button>
              <button className="danger" onClick={() => decide(g.gate, 'rejected')}>
                {t('approvals.reject')}
              </button>
            </div>
          </Section>
        )
      })}
      <Section title={t('approvals.history')}>
        <table>
          <tbody>
            {data?.approvals
              .slice()
              .reverse()
              .map((a) => (
                <tr key={a.id}>
                  <td>{timeOf(a.at)}</td>
                  <td>{a.gate}</td>
                  <td>{a.decision}</td>
                  <td className="mono">{short(a.snapshotHash)}</td>
                  <td>{a.comment}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </Section>
    </>
  )
}
