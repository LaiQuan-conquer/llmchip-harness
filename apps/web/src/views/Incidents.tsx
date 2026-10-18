import { useState } from 'react'
import { useKernel, useQuery } from '../api.ts'
import { useI18n } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { ErrorLine, Section, timeOf } from './common.tsx'

/** Incident center (FR-UI-10, FR-AGT-25..27). */
export function IncidentsView(_: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t, message } = useI18n()
  const { client } = useKernel()
  const [all, setAll] = useState(false)
  const [closing, setClosing] = useState<string | null>(null)
  const [cause, setCause] = useState('')
  const [action, setAction] = useState('')
  const [error, setError] = useState<string | null>(null)
  const { data } = useQuery((c) => c.call('incidents.list', all ? { limit: 500 } : { open: true, limit: 500 }), [all])
  return (
    <Section
      title={t('nav.incidents')}
      actions={
        <>
          <button className={!all ? 'active' : ''} onClick={() => setAll(false)}>
            {t('incidents.open')}
          </button>
          <button className={all ? 'active' : ''} onClick={() => setAll(true)}>
            {t('incidents.all')}
          </button>
        </>
      }
    >
      <ErrorLine error={error} />
      <table>
        <thead>
          <tr>
            <th>{t('common.time')}</th>
            <th>{t('incidents.severity')}</th>
            <th>{t('common.kind')}</th>
            <th>{t('incidents.summary')}</th>
            <th>{t('common.actor')}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {data?.incidents.map((i) => (
            <tr key={i.id}>
              <td>{timeOf(i.openedAt)}</td>
              <td>
                <span className={`pill ${i.severity === 'critical' ? 'bad' : i.severity === 'warning' ? 'warn' : ''}`}>{i.severity}</span>
              </td>
              <td>{i.kind}</td>
              <td>{message(i.summary)}</td>
              <td>
                {i.actor.kind}:{i.actor.id.slice(-8)}
              </td>
              <td>
                {i.closed ? (
                  <span className="muted" title={`${i.closed.cause} / ${i.closed.action}`}>
                    {t('incidents.closed')}
                  </span>
                ) : (
                  <button onClick={() => setClosing(i.id)}>{t('common.close')}</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {data?.incidents.length === 0 && <p className="muted">{t('common.empty')}</p>}
      {closing && (
        <form
          className="form"
          onSubmit={async (e) => {
            e.preventDefault()
            try {
              await client.call('incidents.close', { incident: closing, cause, action })
              setClosing(null)
              setCause('')
              setAction('')
            } catch (err) {
              setError((err as Error).message)
            }
          }}
        >
          <h3>{t('incidents.closeTitle')}</h3>
          <label>
            {t('incidents.cause')}
            <input value={cause} onChange={(e) => setCause(e.target.value)} required />
          </label>
          <label>
            {t('incidents.action')}
            <input value={action} onChange={(e) => setAction(e.target.value)} required />
          </label>
          <div className="form inline">
            <button type="submit">{t('common.submit')}</button>
            <button type="button" onClick={() => setClosing(null)}>
              {t('common.cancel')}
            </button>
          </div>
        </form>
      )}
    </Section>
  )
}
