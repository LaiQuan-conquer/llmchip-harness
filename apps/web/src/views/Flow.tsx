import { useEffect, useMemo, useState } from 'react'
import type { RunRecord, VerdictRecord } from '@llmchip/kernel-api'
import { useKernel, useQuery } from '../api.ts'
import { useI18n, type Key } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { ErrorLine, Section } from './common.tsx'
import { CHECK_TITLES, RUN_TITLES, STEPS, type Format, type StepDef } from './flowSteps.ts'
import { PROCESSES, projectIdFor, startFromWeights, type ProcessId, type StepState as RunStepState } from './modelPipeline.ts'
import { S0Detail } from './steps/S0.tsx'
import { S1Detail } from './steps/S1.tsx'

/** Stages with a detailed demo view. */
const DETAILS: Record<string, (props: { project: string; onBack(): void; onProject(project: string): void }) => React.JSX.Element> = { S0: S0Detail, S1: S1Detail }
export const DETAIL_STAGES = Object.keys(DETAILS)

type StepState = 'pending' | 'active' | 'running' | 'done' | 'attention' | 'notImplemented'

interface StepView {
  def: StepDef
  state: StepState
  runs: RunRecord[]
  /** Latest successful run of each `plugin/handler`. */
  latest: Map<string, RunRecord>
  /** Latest verdict of each of the step's checks. */
  verdicts: Map<string, VerdictRecord>
}

const key = (r: RunRecord) => `${r.plugin}/${r.handler}`

/**
 * The demo flow (S0-S9) of a project as a path: per stage what it does, its
 * state, key results in plain language, checks, gate, deliverables and runs.
 */
export function FlowView({ selection, select, onUp }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void; onUp(): boolean }) {
  const { t, lang, message } = useI18n()
  const project = selection.project
  const { data: projects } = useQuery((c) => c.call('projects.list'), [])
  // Open the demo project (or the only one) without a click.
  useEffect(() => {
    if (project || !projects?.projects.length) return
    select({ project: (projects.projects.find((p) => p.id === 'demo') ?? projects.projects[0]!).id })
  }, [project, projects, select])
  const { data: state, error } = useQuery((c) => (project ? c.call('projects.get', { project }) : Promise.resolve(undefined)), [project])
  const { data: runs } = useQuery((c) => (project ? c.call('runs.list', { project, limit: 1000 }) : Promise.resolve(undefined)), [project])
  const { data: verdicts } = useQuery((c) => c.call('verdicts.list', { limit: 1000 }), [])
  const [open, setOpen] = useState<string | null>(null)
  const detail = selection.stage

  const format: Format = useMemo(() => {
    const locale = lang === 'zh' ? 'zh-CN' : 'en-US'
    return {
      t,
      int: (v) => new Intl.NumberFormat(locale).format(Number(v)),
      num: (v, digits = 1) => new Intl.NumberFormat(locale, { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(Number(v)),
      yes: (v) => t(v ? 'common.yes' : 'common.no'),
    }
  }, [t, lang])

  const steps = useMemo(() => (state && runs && verdicts ? viewSteps(state, runs.runs, verdicts.verdicts) : []), [state, runs, verdicts])
  const proxy = runs?.runs.some((r) => key(r) === 'model-llama/synthesize' && r.metrics.proxy === true && r.metrics.trained === false) ?? false

  if (projects && projects.projects.length === 0) {
    return (
      <>
        <StartFromWeights onStarted={(id) => select({ project: id })} open />
        <Section title={t('flow.title')}>
          <p className="muted">{t('flow.noProjects')}</p>
        </Section>
      </>
    )
  }
  if (!state || steps.length === 0) return <ErrorLine error={error} />
  const Detail = detail ? DETAILS[detail] : undefined
  if (Detail && project) {
    return (
      <Detail
        project={project}
        onProject={(id) => select({ project: id })}
        onBack={() => {
          // Going back restores the scroll position; otherwise show the stage's card.
          setOpen(detail)
          if (!onUp()) setTimeout(() => document.getElementById(`flow-${detail}`)?.scrollIntoView({ block: 'start' }), 50)
        }}
      />
    )
  }

  const implemented = steps.filter((s) => s.def.implemented)
  const done = implemented.filter((s) => s.state === 'done').length
  const current = implemented.find((s) => s.state !== 'done')
  const released = state.gates.filter((g) => g.released).length
  const goTo = (stage: string) => {
    setOpen(stage)
    document.getElementById(`flow-${stage}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <>
      <StartFromWeights onStarted={(id) => select({ project: id })} />
      <Section
        title={t('flow.title')}
        actions={
          <select value={project ?? ''} onChange={(e) => select({ project: e.target.value })} aria-label={t('flow.project')}>
            {projects?.projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.title}
              </option>
            ))}
          </select>
        }
      >
        <p className="flow-summary">
          {t('flow.progress', { done, total: implemented.length, gates: released, gatesTotal: state.gates.length })}{' '}
          {current ? t('flow.current', { stage: `${current.def.stage} ${t(current.def.title)}` }) : t('flow.allDone')}
        </p>
        <div className="flow-bar">
          <div style={{ width: `${(100 * done) / implemented.length}%` }} />
        </div>
        {proxy && <p className="flow-note">{t('flow.caveat.proxy')}</p>}
        <div className="flow-path">
          {steps.map((s) => {
            const gate = s.def.gate ? state.gates.find((g) => g.gate === s.def.gate) : undefined
            return (
              <div className="flow-segment" key={s.def.stage}>
                <button className={`flow-node ${s.state}${open === s.def.stage ? ' selected' : ''}`} onClick={() => goTo(s.def.stage)}>
                  <span className="flow-id">{s.def.stage}</span>
                  <span className="flow-name">{t(s.def.title)}</span>
                  <span className="flow-state">{t(`flow.state.${s.state}` as Key)}</span>
                </button>
                {gate && (
                  <div className={`flow-gate ${gate.released ? 'released' : gate.status}`} title={gate.reasons.map(message).join('\n')}>
                    <span>{gate.gate}</span>
                    <small>{t(`status.${gate.status}` as Key)}</small>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </Section>

      {steps.map((s) => (
        <StepCard key={s.def.stage} step={s} format={format} expanded={open === s.def.stage} onToggle={() => setOpen(open === s.def.stage ? null : s.def.stage)} gate={s.def.gate ? state.gates.find((g) => g.gate === s.def.gate) : undefined} proxy={proxy} select={select} onDetail={DETAILS[s.def.stage] ? () => select({ stage: s.def.stage }) : undefined} />
      ))}
    </>
  )
}

const START_STEPS: [string, Key][] = [
  ['project', 'flow.start.step.project'],
  ['import', 'flow.start.step.import'],
  ['census', 'flow.start.step.census'],
  ['bind', 'flow.start.step.bind'],
  ['estimate', 'flow.start.step.estimate'],
  ['feasibility', 'flow.start.step.feasibility'],
]

/** S0 from a downloaded model: path on this machine -> project, import, census, estimate. */
function StartFromWeights({ onStarted, open: initiallyOpen = false }: { onStarted(project: string): void; open?: boolean }) {
  const { t } = useI18n()
  const { client } = useKernel()
  const [open, setOpen] = useState(initiallyOpen)
  const [path, setPath] = useState('')
  const [project, setProject] = useState('')
  const [bits, setBits] = useState(4)
  const [processId, setProcess] = useState<ProcessId>('gf180mcu')
  const [progress, setProgress] = useState<Record<string, { state: RunStepState; detail?: string }>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const id = project || projectIdFor(path)
  const go = async () => {
    setBusy(true)
    setError(null)
    setProgress({})
    try {
      const title = path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || id
      await startFromWeights(client, { path, project: id, title, bits, process: processId }, (step, state, detail) =>
        setProgress((p) => ({ ...p, [step]: { state, ...(detail ? { detail } : {}) } })),
      )
      onStarted(id)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  if (!open) {
    return (
      <div className="flow-start-toggle">
        <button onClick={() => setOpen(true)}>＋ {t('flow.start.title')}</button>
      </div>
    )
  }
  return (
    <Section title={t('flow.start.title')} actions={<button className="link" onClick={() => setOpen(false)}>{t('common.close')}</button>}>
      <p className="flow-what">{t('flow.start.intro')}</p>
      <div className="form grid">
        <label className="wide">
          {t('flow.start.path')}
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="/path/to/Qwen3-0.6B" />
        </label>
        <label>
          {t('flow.start.project')}
          <input value={id} onChange={(e) => setProject(e.target.value)} pattern="[a-z][a-z0-9-]*" />
        </label>
        <label>
          {t('s0.in.weightBits')}
          <select value={bits} onChange={(e) => setBits(Number(e.target.value))}>
            {[2, 3, 4, 8].map((b) => (
              <option key={b} value={b}>
                {b} bit
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('s0.in.processName')}
          <select value={processId} onChange={(e) => setProcess(e.target.value as ProcessId)}>
            {(Object.keys(PROCESSES) as ProcessId[]).map((p) => (
              <option key={p} value={p}>
                {t(p === 'gf180mcu' ? 's0.process.gf180mcu' : 's0.process.sephonchitech')}
              </option>
            ))}
          </select>
        </label>
        <button className="active" disabled={busy || !path} onClick={go}>
          {busy ? t('playground.running') : t('flow.start.go')}
        </button>
      </div>
      {Object.keys(progress).length > 0 && (
        <ul className="s1-progress">
          {START_STEPS.filter(([s]) => progress[s]).map(([s, label]) => (
            <li key={s} className={progress[s]!.state}>
              {progress[s]!.state === 'running' ? '…' : progress[s]!.state === 'done' ? '✓' : '✗'} {t(label)}
              {progress[s]!.detail && <small className="muted"> {['pass', 'fail', 'indeterminate', 'waived'].includes(progress[s]!.detail!) ? t(`flow.verdict.${progress[s]!.detail}` as Key) : progress[s]!.detail}</small>}
            </li>
          ))}
        </ul>
      )}
      <ErrorLine error={error} />
    </Section>
  )
}

function StepCard({
  step,
  format,
  expanded,
  onToggle,
  gate,
  proxy,
  select,
  onDetail,
}: {
  step: StepView
  format: Format
  expanded: boolean
  onToggle(): void
  gate: { gate: string; status: string; released: boolean; reasons: { code: string; params?: Record<string, unknown> }[] } | undefined
  proxy: boolean
  select: (s: Partial<Selection>, to?: Page) => void
  onDetail: (() => void) | undefined
}) {
  const { t, message } = useI18n()
  const { def } = step
  const facts = def.facts
    .map((fact) => {
      const run = step.latest.get(fact.run)
      const value = run ? fact.show(run.metrics, format) : undefined
      return { fact, value, good: run && value !== undefined ? fact.good?.(run.metrics) : undefined }
    })
    .filter((f) => f.value !== undefined)
  const deliverables = def.deliverables
    .map((d) => ({ d, hash: step.latest.get(d.run)?.outputs.find((o) => o.name === d.output)?.hash }))
    .filter((d): d is { d: (typeof def.deliverables)[number]; hash: string } => d.hash !== undefined)
  return (
    <section id={`flow-${def.stage}`} className={`card flow-card ${step.state}${expanded ? ' selected' : ''}`}>
      <div className="card-head">
        <h2>
          {def.stage} · {t(def.title)}
        </h2>
        <div>
          {def.research && <span className="pill">{t(def.research)}</span>}
          <span className={`pill ${tone(step.state)}`}>{t(`flow.state.${step.state}` as Key)}</span>
        </div>
      </div>
      <p className="flow-what">{t(def.what)}</p>
      {onDetail && (
        <button className="active flow-detail" onClick={onDetail}>
          {t('flow.openDetail')} →
        </button>
      )}
      {proxy && (def.stage === 'S1' || def.stage === 'S2') && <p className="flow-note">{t('flow.caveat.proxy')}</p>}

      {def.implemented && (
        <>
          <h4>{t('flow.results')}</h4>
          {facts.length > 0 ? (
            <div className="flow-facts">
              {facts.map(({ fact, value, good }) => (
                <div key={fact.label} className={`flow-fact ${good === true ? 'ok' : good === false ? 'bad' : ''}`}>
                  <small>{t(fact.label)}</small>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted">{t('flow.noResults')}</p>
          )}

          <h4>{t('flow.checks')}</h4>
          {def.checks.length === 0 ? (
            <p className="muted">{t('flow.noChecks')}</p>
          ) : (
            <ul className="flow-checks">
              {def.checks.map((check) => {
                const verdict = step.verdicts.get(check)
                return (
                  <li key={check}>
                    <span className={`pill ${verdictTone(verdict?.verdict)}`}>{t(`flow.verdict.${verdict?.verdict ?? 'none'}` as Key)}</span>
                    {t(CHECK_TITLES[check] ?? 'flow.check.other')}
                    {verdict && verdict.verdict !== 'pass' && verdict.reasons.length > 0 && <small className="muted"> · {verdict.reasons.map(message).join('; ')}</small>}
                  </li>
                )
              })}
            </ul>
          )}

          {gate && (
            <p className="flow-gate-line">
              <span className={`pill ${gate.released ? 'ok' : gate.status === 'invalidated' || gate.status === 'rejected' ? 'bad' : ''}`}>{t('flow.gate', { gate: gate.gate })}</span>
              {t(`status.${gate.status}` as Key)}
              {!gate.released && gate.status !== 'none' && gate.reasons.length > 0 && <small className="muted"> · {gate.reasons.map(message).join('; ')}</small>}
            </p>
          )}

          {deliverables.length > 0 && (
            <>
              <h4>{t('flow.deliverables')}</h4>
              <div className="flow-outputs">
                {deliverables.map(({ d, hash }) => (
                  <button key={d.label} onClick={() => select({ artifact: hash }, 'artifacts')}>
                    {t(d.label)}
                  </button>
                ))}
              </div>
            </>
          )}

          {step.runs.length > 0 && (
            <button className="link" onClick={onToggle}>
              {expanded ? t('flow.hideDetails') : t('flow.details', { count: step.runs.length })}
            </button>
          )}
          {expanded && (
            <table className="flow-runs">
              <thead>
                <tr>
                  <th>{t('flow.col.step')}</th>
                  <th>{t('flow.col.state')}</th>
                  <th>{t('flow.col.duration')}</th>
                  <th>{t('flow.col.time')}</th>
                </tr>
              </thead>
              <tbody>
                {step.runs.map((r) => (
                  <tr key={r.id}>
                    <td>{t(RUN_TITLES[key(r)] ?? 'flow.run.other')}</td>
                    <td>
                      <span className={`pill ${r.state !== 'finished' ? '' : r.execution === 'exited_zero' ? 'ok' : 'bad'}`}>{t(runState(r))}</span>
                      {r.error && <small className="muted"> {r.error}</small>}
                    </td>
                    <td>{duration(r, t)}</td>
                    <td className="muted">{r.endedAt ? new Date(r.endedAt).toLocaleTimeString() : ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  )
}

/** Assigns the project's runs to stages and derives each stage's state. */
function viewSteps(
  state: { stages: { id: string; entered: boolean }[]; gates: { gate: string; released: boolean }[] },
  runs: RunRecord[],
  verdicts: VerdictRecord[],
): StepView[] {
  const ids = new Set(runs.map((r) => r.id))
  const ours = verdicts.filter((v): v is VerdictRecord & { run: string } => v.run !== null && ids.has(v.run)).sort((a, b) => a.createdAt.localeCompare(b.createdAt))
  // A run judged by a stage's check belongs to that stage (e.g. the storage views checked
  // before S4, S6 and S7); any other run to the first stage that lists its handler.
  const byCheck = new Map<string, string>()
  for (const v of ours) {
    const step = STEPS.find((s) => s.checks.includes(v.check))
    if (step) byCheck.set(v.run, step.stage)
  }
  const stageOf = (r: RunRecord) => byCheck.get(r.id) ?? STEPS.find((s) => s.runs.includes(key(r)))?.stage
  const entered = new Set(state.stages.filter((s) => s.entered).map((s) => s.id))
  return STEPS.map((def, i) => {
    const mine = runs.filter((r) => stageOf(r) === def.stage)
    const latest = new Map<string, RunRecord>()
    for (const r of mine) if (r.state === 'finished' && r.execution === 'exited_zero') latest.set(key(r), r)
    const judged = new Map<string, VerdictRecord>()
    for (const v of ours) if (def.checks.includes(v.check)) judged.set(v.check, v)
    const next = STEPS[i + 1]
    let s: StepState
    if (!def.implemented) s = 'notImplemented'
    else if (mine.some((r) => r.state !== 'finished')) s = 'running'
    else if (def.gate ? state.gates.find((g) => g.gate === def.gate)?.released : next && entered.has(next.stage)) s = 'done'
    else if ([...judged.values()].some((v) => v.verdict === 'fail' || v.verdict === 'indeterminate') || mine.some((r) => r.execution !== 'exited_zero')) s = 'attention'
    else if (mine.length > 0 || entered.has(def.stage)) s = 'active'
    else s = 'pending'
    return { def, state: s, runs: mine, latest, verdicts: judged }
  })
}

function tone(state: StepState): string {
  return state === 'done' ? 'ok' : state === 'attention' ? 'bad' : state === 'running' || state === 'active' ? 'warn' : ''
}

function verdictTone(verdict: string | undefined): string {
  return verdict === 'pass' || verdict === 'waived' ? 'ok' : verdict === 'fail' ? 'bad' : verdict === 'indeterminate' ? 'warn' : ''
}

function runState(r: RunRecord): Key {
  if (r.state === 'queued') return 'flow.runState.queued'
  if (r.state === 'running') return 'flow.runState.running'
  return r.execution === 'exited_zero' ? 'flow.runState.ok' : 'flow.runState.failed'
}

function duration(r: RunRecord, t: (key: Key, params?: Record<string, unknown>) => string): string {
  if (!r.startedAt) return ''
  const seconds = Math.max(0, Math.round(((r.endedAt ? Date.parse(r.endedAt) : Date.now()) - Date.parse(r.startedAt)) / 1000))
  return seconds < 60 ? t('flow.dur.s', { s: seconds }) : t('flow.dur.min', { m: Math.floor(seconds / 60), s: seconds % 60 })
}
