// The two entries of a stage (requirements FR-UI-14, FR-PRJ-07/08): run its development
// pipeline, or run all its required tests; for `all`, every stage in order. And the pipelines
// of a project with their nodes, so the owner sees what ran, what failed and why, and can
// cancel a pipeline or resume it after fixing the cause.

import { useState } from 'react'
import type { PipelineKind, PipelineNode, PipelineRecord } from '@llmchip/kernel-api'
import { useKernel, useQuery } from '../api.ts'
import { useI18n, type Key } from '../i18n.ts'
import { ErrorLine } from './common.tsx'

const KINDS: PipelineKind[] = ['develop', 'test']

/**
 * The two buttons of a stage (or of `all` stages), each disabled with the reason when it cannot
 * start. The plans are asked again only when `refreshKey` changes (bound slots, entered stages,
 * pipelines), not on every kernel event.
 */
export function PipelineEntries({ project, stage, refreshKey }: { project: string; stage: string; refreshKey: string }) {
  const { t } = useI18n()
  const { client } = useKernel()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const plans = useQuery(
    (c) => Promise.all(KINDS.map((kind) => c.call('pipelines.plan', { project, stage, kind }).catch(() => undefined))),
    [project, stage, refreshKey],
    false,
  ).data
  const start = async (kind: PipelineKind) => {
    setBusy(true)
    setError(null)
    try {
      await client.call('pipelines.start', { project, stage, kind })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const all = stage === 'all'
  return (
    <div className="pipeline-entries">
      {KINDS.map((kind, i) => {
        const plan = plans?.[i]
        const declared = !!plan && plan.nodes > 0
        const why = !plan ? '' : !declared ? t('pipe.notDeclared') : plan.missing.length ? t('pipe.missing', { list: plan.missing.map((m) => `${m.node} ← ${m.ref}`).join('; ') }) : ''
        return (
          <button key={kind} data-pipeline={`${stage}:${kind}`} disabled={busy || !plan?.ready} title={why} onClick={() => void start(kind)}>
            {t(`pipe.${all ? 'all.' : ''}${kind}` as Key)}
          </button>
        )
      })}
      {plans?.some((p) => p && p.nodes > 0 && p.missing.length > 0) && (
        <small className="muted">{t('pipe.missing', { list: [...new Set(plans.flatMap((p) => p?.missing.map((m) => m.ref) ?? []))].join(', ') })}</small>
      )}
      <ErrorLine error={error} />
    </div>
  )
}

/** The latest pipeline that ran this stage, in one line. */
export function StagePipelineStatus({ pipelines, stage }: { pipelines: PipelineRecord[]; stage: string }) {
  const { t } = useI18n()
  const latest = pipelines.find((p) => p.stages.includes(stage))
  if (!latest) return null
  return (
    <p className="muted pipeline-status">
      <span className={`pill ${tone(latest.state)}`}>{t(`pipe.kind.${latest.kind}` as Key)} · {t(`pipe.state.${latest.state}` as Key)}</span> <Summary pipeline={latest} />
    </p>
  )
}

/** The project's recent pipelines (newest first); the latest one is open. */
export function PipelinePanel({ pipelines: all, error }: { pipelines: PipelineRecord[]; error: string | null }) {
  const { t } = useI18n()
  const [open, setOpen] = useState<string | null>(null)
  const pipelines = all.slice(0, 10)
  const shown = open ?? pipelines[0]?.id ?? null
  return (
    <div className="pipeline-panel">
      <ErrorLine error={error} />
      {pipelines.length === 0 && <p className="muted">{t('pipe.empty')}</p>}
      {pipelines.map((p) => (
        <PipelineView key={p.id} pipeline={p} expanded={p.id === shown} onToggle={() => setOpen(p.id === shown ? '' : p.id)} />
      ))}
    </div>
  )
}

function PipelineView({ pipeline, expanded, onToggle }: { pipeline: PipelineRecord; expanded: boolean; onToggle(): void }) {
  const { t, message } = useI18n()
  const { client } = useKernel()
  const [error, setError] = useState<string | null>(null)
  const act = async (method: 'pipelines.cancel' | 'pipelines.resume') => {
    setError(null)
    try {
      await client.call(method, { pipeline: pipeline.id })
    } catch (e) {
      setError((e as Error).message)
    }
  }
  return (
    <div className={`pipeline ${pipeline.state}`} data-pipeline-id={pipeline.id}>
      <div className="pipeline-head">
        <button className="link" onClick={onToggle}>
          {expanded ? '▾' : '▸'} {t(`pipe.kind.${pipeline.kind}` as Key)} · {pipeline.stages.join(', ')}
        </button>{' '}
        <span className={`pill ${tone(pipeline.state)}`}>{t(`pipe.state.${pipeline.state}` as Key)}</span> <Summary pipeline={pipeline} />{' '}
        <small className="muted">{new Date(pipeline.startedAt).toLocaleString()}</small>{' '}
        {pipeline.state === 'running' && <button onClick={() => void act('pipelines.cancel')}>{t('pipe.cancel')}</button>}
        {(pipeline.state === 'failed' || pipeline.state === 'cancelled') && <button onClick={() => void act('pipelines.resume')}>{t('pipe.resume')}</button>}
      </div>
      <ErrorLine error={error} />
      {expanded && (
        <table className="pipeline-nodes">
          <thead>
            <tr>
              <th>{t('pipe.col.node')}</th>
              <th>{t('pipe.col.state')}</th>
              <th>{t('pipe.col.run')}</th>
              <th>{t('pipe.col.result')}</th>
            </tr>
          </thead>
          <tbody>
            {pipeline.nodes.map((n) => (
              <tr key={n.id} className={n.state}>
                <td>{n.id}</td>
                <td>
                  <span className={`pill ${nodeTone(n)}`}>{t(`pipe.node.${n.state}` as Key)}</span>
                </td>
                <td className="mono">{n.reused ? t('pipe.reused', { run: short(n.reused) }) : n.runs.map(short).join(' → ')}</td>
                <td>
                  {n.verdicts.map((v) => (
                    <span key={v.id} className={`pill ${v.verdict === 'pass' ? 'ok' : v.verdict === 'fail' ? 'bad' : 'warn'}`}>
                      {v.check}: {t(`flow.verdict.${v.verdict}` as Key)}
                    </span>
                  ))}
                  {n.reason && <small className="muted"> {message(n.reason)}</small>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function Summary({ pipeline }: { pipeline: PipelineRecord }) {
  const { t } = useI18n()
  const s = pipeline.summary
  return (
    <small className="muted">
      {t('pipe.summary', { done: s.done, nodes: s.nodes, failed: s.failed, skipped: s.skipped })}
      {pipeline.kind === 'test' && ` · ${t('pipe.tests', { pass: s.pass, fail: s.fail, indeterminate: s.indeterminate })}`}
    </small>
  )
}

const short = (id: string) => id.slice(-8)

function tone(state: PipelineRecord['state']): string {
  return state === 'passed' ? 'ok' : state === 'running' ? 'warn' : 'bad'
}

function nodeTone(node: PipelineNode): string {
  return node.state === 'done' ? 'ok' : node.state === 'failed' ? 'bad' : node.state === 'running' ? 'warn' : ''
}
