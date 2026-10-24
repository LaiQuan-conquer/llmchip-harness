import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useKernel, useQuery } from './api.ts'
import { useI18n, type Key } from './i18n.ts'
import { AgentsView } from './views/Agents.tsx'
import { ArtifactsView } from './views/Artifacts.tsx'
import { EstimatorView } from './views/Estimator.tsx'
import { DETAIL_STAGES, FlowView } from './views/Flow.tsx'
import { IncidentsView } from './views/Incidents.tsx'
import { OverviewView } from './views/Overview.tsx'
import { PlaygroundView } from './views/Playground.tsx'
import { SettingsView } from './views/Settings.tsx'
import { TrajectoryView } from './views/Trajectory.tsx'
import { VChipView } from './views/VChip.tsx'
import { activityLine, runNamesOf } from './views/activity.ts'
import { timeOf } from './views/common.tsx'
import { STEPS } from './views/flowSteps.ts'

export type Page = 'flow' | 'overview' | 'agents' | 'trajectory' | 'estimator' | 'playground' | 'vchip' | 'incidents' | 'artifacts' | 'settings'

export interface Selection {
  project: string | null
  task: string | null
  artifact: string | null
  /** The stage whose detailed demo is open on the flow page. */
  stage: string | null
}

/** Where the owner is: a page and what is selected on it. */
interface Place {
  page: Page
  selection: Selection
  scroll: number
}

const PAGES: Page[] = ['flow', 'overview', 'agents', 'trajectory', 'estimator', 'playground', 'vchip', 'incidents', 'artifacts', 'settings']

/** Main layout (FR-UI-05): projects and tasks on the left, the work in the middle, context on the right. */
export function App() {
  const { t } = useI18n()
  // Every move to another page, detail view or artifact is remembered, so that the
  // back bar (and the browser's back button) returns to where the owner came from.
  const nav = useRef<{ place: Place; trail: Place[] }>({ place: { page: 'flow', selection: { project: null, task: null, artifact: null, stage: null }, scroll: 0 }, trail: [] })
  const [, setVersion] = useState(0)
  const center = useRef<HTMLElement>(null)
  const scrollTo = useRef<number | null>(null)
  const select = useCallback((change: Partial<Selection>, to?: Page) => {
    const from = { ...nav.current.place, scroll: center.current?.scrollTop ?? 0 }
    const next = { page: to ?? from.page, selection: { ...from.selection, ...change }, scroll: 0 }
    const a = from.selection
    const b = next.selection
    const moved = next.page !== from.page || b.stage !== a.stage || b.artifact !== a.artifact || (a.project !== null && b.project !== a.project)
    nav.current.place = next
    if (moved) {
      nav.current.trail = [...nav.current.trail.slice(-49), from]
      history.pushState(null, '')
      scrollTo.current = 0
    }
    setVersion((v) => v + 1)
  }, [])
  useEffect(() => {
    const onPop = () => {
      const previous = nav.current.trail.at(-1)
      if (!previous) return
      nav.current = { place: previous, trail: nav.current.trail.slice(0, -1) }
      scrollTo.current = previous.scroll
      setVersion((v) => v + 1)
    }
    addEventListener('popstate', onPop)
    return () => removeEventListener('popstate', onPop)
  }, [])
  // Restore the scroll position once the page has loaded enough content for it.
  useLayoutEffect(() => {
    const target = scrollTo.current
    const el = center.current
    if (target === null || !el) return
    scrollTo.current = null
    let frames = 0
    const apply = () => {
      el.scrollTop = target
      if (el.scrollTop < target - 1 && frames++ < 60) requestAnimationFrame(apply)
    }
    apply()
  })
  const { page, selection } = nav.current.place
  const previous = nav.current.trail.at(-1)
  /** Back from a stage's detail to the flow: the previous place when that is the flow, else the flow itself. */
  const up = () => {
    if (previous?.page === 'flow' && previous.selection.stage === null && previous.selection.project === selection.project) {
      history.back()
      return true
    }
    select({ stage: null })
    return false
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
            <button key={p} className={p === page ? 'active' : ''} onClick={() => select({ stage: null }, p)} data-page={p}>
              {t(`nav.${p}` as Key)}
            </button>
          ))}
        </nav>
        <Status />
      </header>
      <aside className="left">
        <Sidebar {...props} page={page} />
      </aside>
      <main className="center" ref={center}>
        {previous && (
          <div className="nav-back-bar">
            <button className="nav-back" onClick={() => history.back()}>
              ← {t('nav.back', { place: placeName(previous, t) })}
            </button>
          </div>
        )}
        {page === 'flow' && <FlowView {...props} onUp={up} />}
        {page === 'overview' && <OverviewView {...props} />}
        {page === 'agents' && <AgentsView {...props} />}
        {page === 'trajectory' && <TrajectoryView {...props} />}
        {page === 'estimator' && <EstimatorView {...props} />}
        {page === 'playground' && <PlaygroundView {...props} />}
        {page === 'vchip' && <VChipView {...props} />}
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

function placeName({ page, selection }: Place, t: (k: Key, p?: Record<string, unknown>) => string): string {
  const step = page === 'flow' && selection.stage ? STEPS.find((s) => s.stage === selection.stage) : undefined
  const name = step ? `${step.stage} · ${t(step.title)} · ${t('flow.openDetail')}` : t(`nav.${page}` as Key)
  return selection.project && (page === 'flow' || page === 'overview') ? t('nav.placeOfProject', { place: name, project: selection.project }) : name
}

function Sidebar({ selection, select, page }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void; page: Page }) {
  const { t } = useI18n()
  const { data: projects } = useQuery((c) => c.call('projects.list'), [])
  const { data: tasks } = useQuery((c) => c.call('tasks.list'), [])
  // What a selected project opens: its flow, the stages with a detailed demo, and its overview.
  const entries: { label: string; page: Page; stage: string | null }[] = [
    { label: t('nav.flow'), page: 'flow', stage: null },
    ...DETAIL_STAGES.map((stage) => ({ label: `${stage} · ${t(STEPS.find((s) => s.stage === stage)!.title)}`, page: 'flow' as Page, stage })),
    { label: t('nav.overview'), page: 'overview', stage: null },
  ]
  return (
    <>
      <h3>{t('side.projects')}</h3>
      <ul className="list">
        {projects?.projects.length === 0 && <li className="muted">{t('side.none')}</li>}
        {projects?.projects.map((p) => (
          <li key={p.id} className={selection.project === p.id ? 'selected' : ''} data-project={p.id}>
            <div className="side-project" onClick={() => select({ project: p.id, stage: null }, 'flow')}>
              <strong>{p.id}</strong>
              <small>{p.title}</small>
            </div>
            {selection.project === p.id && (
              <ul className="side-entries">
                {entries.map((e) => (
                  <li
                    key={e.label}
                    className={page === e.page && (e.page !== 'flow' || selection.stage === e.stage) ? 'current' : ''}
                    onClick={() => select({ project: p.id, stage: e.stage }, e.page)}
                  >
                    {e.label}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
      <button className="link" onClick={() => select({ project: null, stage: null }, 'overview')}>
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
  const runNames = runNamesOf(events, t)
  const { data: state } = useQuery((c) => (selection.project ? c.call('projects.get', { project: selection.project }) : Promise.resolve(undefined)), [selection.project])
  return (
    <>
      <h3>{t('context.title')}</h3>
      {state && (
        <>
          <h4>{t('context.stages')}</h4>
          <ul className="list compact">
            <li>
              {state.stages.map((s) => (
                <span key={s.id} className={s.entered ? 'pill ok' : 'pill'} title={s.entered ? t('overview.entered') : t('overview.notEntered')}>
                  {s.id}
                </span>
              ))}
            </li>
          </ul>
        </>
      )}
      <h4>{t('context.recent')}</h4>
      <ul className="list compact events">
        {events
          .map((e) => ({ e, line: activityLine(e, runNames, t, message) }))
          .filter((a) => a.line !== null)
          .slice(-30)
          .reverse()
          .map(({ e, line }) => (
            <li key={e.seq}>
              <small>{timeOf(e.ts)}</small>
              <div className={line!.tone === 'ok' ? 'ok-text' : line!.tone === 'bad' ? 'error' : ''}>{line!.text}</div>
            </li>
          ))}
      </ul>
    </>
  )
}
