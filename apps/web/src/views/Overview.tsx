import { useState } from 'react'
import { useKernel, useQuery } from '../api.ts'
import { useI18n, type Key } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { ErrorLine, Section, short } from './common.tsx'

export function OverviewView({ selection, select }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t, message } = useI18n()
  const { client } = useKernel()
  const { data: state, error } = useQuery(
    (c) => (selection.project ? c.call('projects.get', { project: selection.project }) : Promise.resolve(undefined)),
    [selection.project],
  )
  const { data: incidents } = useQuery((c) => c.call('incidents.list', { open: true }), [])
  const { data: runs } = useQuery((c) => c.call('runs.list', selection.project ? { project: selection.project } : {}), [selection.project])
  const [id, setId] = useState('')
  const [title, setTitle] = useState('')
  const [createError, setCreateError] = useState<string | null>(null)

  if (!selection.project) {
    return (
      <Section title={t('side.newProject')}>
        <p className="muted">{t('overview.noProject')}</p>
        <form
          className="form"
          onSubmit={async (e) => {
            e.preventDefault()
            try {
              await client.call('projects.create', { id, title })
              select({ project: id })
            } catch (err) {
              setCreateError((err as Error).message)
            }
          }}
        >
          <label>
            {t('overview.create.id')}
            <input value={id} onChange={(e) => setId(e.target.value)} pattern="[a-z][a-z0-9-]*" required />
          </label>
          <label>
            {t('overview.create.title')}
            <input value={title} onChange={(e) => setTitle(e.target.value)} required />
          </label>
          <button type="submit">{t('common.submit')}</button>
          <ErrorLine error={createError} />
        </form>
      </Section>
    )
  }
  const open = incidents?.incidents.filter((i) => i.project === selection.project) ?? []
  return (
    <>
      <ErrorLine error={error} />
      {state && (
        <Section title={`${state.project.title} (${state.project.id}) · ${state.branch}`}>
          <h3>{t('overview.stages')}</h3>
          <div className="stages">
            {state.stages.map((s) => (
              <span key={s.id} className={s.entered ? 'stage entered' : 'stage'} title={s.entered ? t('overview.entered') : t('overview.notEntered')}>
                {s.id}
              </span>
            ))}
          </div>
          <h3>{t('context.gates')}</h3>
          <table>
            <tbody>
              {state.gates.map((g) => (
                <tr key={g.gate}>
                  <td>
                    <span className={g.released ? 'pill ok' : 'pill'}>{g.gate}</span>
                  </td>
                  <td>{t(`status.${g.status}` as Key)}</td>
                  <td className="muted">{g.reasons.map(message).join('; ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>{t('overview.slots')}</h3>
          <table>
            <tbody>
              {Object.entries(state.slots).map(([slot, hash]) => (
                <tr key={slot} className="clickable" onClick={() => select({ artifact: hash }, 'artifacts')}>
                  <td>{slot}</td>
                  <td className="mono">{short(hash)}</td>
                </tr>
              ))}
              {Object.keys(state.slots).length === 0 && (
                <tr>
                  <td className="muted">{t('common.empty')}</td>
                </tr>
              )}
            </tbody>
          </table>
        </Section>
      )}
      <Section title={`${t('overview.openIncidents')} (${open.length})`}>
        <ul className="list compact">
          {open.map((i) => (
            <li key={i.id} className="clickable" onClick={() => select({}, 'incidents')}>
              <span className={`pill ${i.severity === 'critical' ? 'bad' : 'warn'}`}>{i.kind}</span> {message(i.summary)}
            </li>
          ))}
          {open.length === 0 && <li className="muted">{t('common.empty')}</li>}
        </ul>
      </Section>
      <Section title={t('overview.runs')}>
        <table>
          <tbody>
            {runs?.runs
              .slice(-15)
              .reverse()
              .map((r) => (
                <tr key={r.id}>
                  <td className="mono">{short(r.id)}</td>
                  <td>
                    {r.plugin}/{r.handler}
                  </td>
                  <td>{r.state}</td>
                  <td>{r.execution}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </Section>
    </>
  )
}
