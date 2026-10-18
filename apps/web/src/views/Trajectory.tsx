import { useState } from 'react'
import type { IntegrityReport } from '@llmchip/kernel-api'
import { useKernel, useQuery } from '../api.ts'
import { useI18n } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { eventLine, Label, Section, short, timeOf } from './common.tsx'

/** Trajectory (FR-UI-07): the event timeline and the lineage of the selected artifact. */
export function TrajectoryView({ selection, select }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t, message } = useI18n()
  const { client } = useKernel()
  const [kind, setKind] = useState('')
  const [report, setReport] = useState<IntegrityReport | null>(null)
  const { data: events } = useQuery((c) => c.call('events.list', { since: 0, limit: 1000, ...(kind ? { kind } : {}) }), [kind])
  const { data: lineage } = useQuery(
    (c) => (selection.artifact ? c.call('artifacts.lineage', { hash: selection.artifact, direction: 'up' }) : Promise.resolve(undefined)),
    [selection.artifact],
  )
  return (
    <>
      <Section title={t('trajectory.lineage')}>
        {!lineage && <p className="muted">{t('trajectory.pickArtifact')}</p>}
        {lineage && (
          <div className="lineage">
            {lineage.nodes.map((n) => (
              <div key={n.hash} className={n.hash === selection.artifact ? 'node root' : 'node'} onClick={() => select({ artifact: n.hash }, 'artifacts')}>
                <strong>{n.type}</strong> <Label label={n.label} />
                <div className="mono">{short(n.hash)}</div>
                <small>
                  {n.creator.kind}:{n.creator.id}
                  {n.run ? ` · run ${short(n.run)}` : ''}
                </small>
                <ul>
                  {lineage.edges
                    .filter((e) => e.to === n.hash)
                    .map((e) => (
                      <li key={e.from} className="mono">
                        ← {short(e.from)}
                      </li>
                    ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </Section>
      <Section
        title={t('trajectory.timeline')}
        actions={
          <>
            <input placeholder="kind prefix, e.g. agent." value={kind} onChange={(e) => setKind(e.target.value)} />
            <button onClick={async () => setReport(await client.call('events.verify'))}>{t('trajectory.verify')}</button>
          </>
        }
      >
        {report && (
          <p className={report.ok ? 'ok-text' : 'error'}>
            {report.ok ? t('trajectory.verifyOk', { n: report.checked }) : t('trajectory.verifyBad') + report.problems.map(message).join('; ')}
          </p>
        )}
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>{t('common.time')}</th>
              <th>{t('common.kind')}</th>
              <th>{t('common.actor')}</th>
              <th>{t('common.label')}</th>
            </tr>
          </thead>
          <tbody>
            {events?.events
              .slice()
              .reverse()
              .map((e) => (
                <tr key={e.seq}>
                  <td>{e.seq}</td>
                  <td>{timeOf(e.ts)}</td>
                  <td>{eventLine(e)}</td>
                  <td>
                    {e.actor.kind}:{e.actor.id.slice(-8)}
                  </td>
                  <td>
                    <Label label={e.label} />
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </Section>
    </>
  )
}
