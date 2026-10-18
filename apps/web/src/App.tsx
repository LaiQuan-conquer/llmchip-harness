import { useState } from 'react'
import { useKernel, useQuery } from './api.ts'
import { useI18n, type Key } from './i18n.ts'
import { ApprovalsView } from './views/Approvals.tsx'
import { AgentsView } from './views/Agents.tsx'
import { ArtifactsView } from './views/Artifacts.tsx'
import { EstimatorView } from './views/Estimator.tsx'
import { IncidentsView } from './views/Incidents.tsx'
import { OverviewView } from './views/Overview.tsx'
import { PlaygroundView } from './views/Playground.tsx'
import { SettingsView } from './views/Settings.tsx'
import { TrajectoryView } from './views/Trajectory.tsx'
import { eventLine, timeOf } from './views/common.tsx'

export type Page = 'overview' | 'agents' | 'trajectory' | 'estimator' | 'playground' | 'approvals' | 'incidents' | 'artifacts' | 'settings'

export interface Selection {
  project: string | null
  task: string | null
  artifact: string | null
}

const PAGES: Page[] = ['overview', 'agents', 'trajectory', 'estimator', 'playground', 'approvals', 'incidents', 'artifacts', 'settings']

/** Main layout (FR-UI-05): projects and tasks on the left, the work in the middle, context on the right. */
export function App() {
  const { t } = useI18n()
  const [page, setPage] = useState<Page>('overview')
  const [selection, setSelection] = useState<Selection>({ project: null, task: null, artifact: null })
  const select = (change: Partial<Selection>, to?: Page) => {
    setSelection((s) => ({ ...s, ...change }))
    if (to) setPage(to)
  }
  const props = { selection, select }
  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <strong>{t('app.title')}</strong>
          <span>{t('app.subtitle')}</span>
        </div>
        <nav>
          {PAGES.map((p) => (
            <button key={p} className={p === page ? 'active' : ''} onClick={() => setPage(p)} data-page={p}>
              {t(`nav.${p}` as Key)}
            </button>
          ))}
        </nav>
        <Status />
      </header>
      <aside className="left">
        <Sidebar {...props} onPage={setPage} />
      </aside>
      <main className="center">
        {page === 'overview' && <OverviewView {...props} />}
        {page === 'agents' && <AgentsView {...props} />}
        {page === 'trajectory' && <TrajectoryView {...props} />}
        {page === 'estimator' && <EstimatorView {...props} />}
        {page === 'playground' && <PlaygroundView {...props} />}
        {page === 'approvals' && <ApprovalsView {...props} />}
        {page === 'incidents' && <IncidentsView {...props} />}
        {page === 'artifacts' && <ArtifactsView {...props} />}
        {page === 'settings' && <SettingsView />}
      </main>
      <aside className="right">
        <ContextPanel selection={selection} />
      </aside>
    </div>
  )
}

function Status() {
  const { t } = useI18n()
  const { data } = useQuery((c) => c.call('kernel.info'), [])
  const { data: incidents } = useQuery((c) => c.call('incidents.list', { open: true }), [])
  return (
    <div className="status">
      <span className={data?.paused ? 'pill warn' : 'pill ok'}>{data?.paused ? t('settings.paused') : t('conn.connected')}</span>
      {incidents && incidents.incidents.length > 0 && <span className="pill bad">{t('overview.openIncidents')}: {incidents.incidents.length}</span>}
    </div>
  )
}

function Sidebar({ selection, select, onPage }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void; onPage: (p: Page) => void }) {
  const { t } = useI18n()
  const { data: projects } = useQuery((c) => c.call('projects.list'), [])
  const { data: tasks } = useQuery((c) => c.call('tasks.list'), [])
  return (
    <>
      <h3>{t('side.projects')}</h3>
      <ul className="list">
        {projects?.projects.length === 0 && <li className="muted">{t('side.none')}</li>}
        {projects?.projects.map((p) => (
          <li key={p.id} className={selection.project === p.id ? 'selected' : ''} onClick={() => select({ project: p.id }, 'overview')}>
            <strong>{p.id}</strong>
            <small>{p.title}</small>
          </li>
        ))}
      </ul>
      <button className="link" onClick={() => (select({ project: null }), onPage('overview'))}>
        + {t('side.newProject')}
      </button>
      <h3>{t('side.tasks')}</h3>
      <ul className="list">
        {tasks?.tasks.length === 0 && <li className="muted">{t('side.none')}</li>}
        {tasks?.tasks.map((task) => (
          <li key={task.id} className={selection.task === task.id ? 'selected' : ''} onClick={() => select({ task: task.id, project: task.spec.project }, 'agents')}>
            <strong>{task.spec.title}</strong>
            <small>
              <span className={`dot ${task.state}`} /> {task.state} · {task.spec.goal} · {task.spec.provider}
            </small>
          </li>
        ))}
      </ul>
    </>
  )
}

function ContextPanel({ selection }: { selection: Selection }) {
  const { t, message } = useI18n()
  const { events } = useKernel()
  const { data: state } = useQuery((c) => (selection.project ? c.call('projects.get', { project: selection.project }) : Promise.resolve(undefined)), [selection.project])
  return (
    <>
      <h3>{t('context.title')}</h3>
      {state && (
        <>
          <h4>{t('context.gates')}</h4>
          <ul className="list compact">
            {state.gates.map((g) => (
              <li key={g.gate} title={g.reasons.map(message).join('\n')}>
                <span className={g.released ? 'pill ok' : 'pill'}>{g.gate}</span> {t(`status.${g.status}` as Key)}
              </li>
            ))}
          </ul>
        </>
      )}
      <h4>{t('context.recent')}</h4>
      <ul className="list compact events">
        {events
          .slice(-30)
          .reverse()
          .map((e) => (
            <li key={e.seq}>
              <small>
                #{e.seq} {timeOf(e.ts)}
              </small>
              <div>{eventLine(e)}</div>
            </li>
          ))}
      </ul>
    </>
  )
}
