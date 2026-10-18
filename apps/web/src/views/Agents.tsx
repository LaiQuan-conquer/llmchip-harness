import { useState } from 'react'
import { useKernel, useQuery } from '../api.ts'
import { useI18n } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { ErrorLine, Label, Section, timeOf } from './common.tsx'

interface StepPayload {
  step: number
  agent: string
  provider: string
  text: string
  toolCalls: { id: string; name: string; arguments: Record<string, unknown> }[]
  results: { id: string; ok: boolean; content: string }[]
  tokens: number
}

/** The agent workflow (FR-UI-05 centre column): every step, tool call and result, live. */
export function AgentsView({ selection }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t } = useI18n()
  const { client } = useKernel()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { data: task } = useQuery((c) => (selection.task ? c.call('tasks.get', { task: selection.task }) : Promise.resolve(undefined)), [selection.task])
  const { data: steps } = useQuery(
    async (c) => (selection.task ? (await c.call('events.list', { kind: 'agent.step', limit: 1000 })).events.filter((e) => e.payload.task === selection.task) : []),
    [selection.task],
  )
  if (!task) return <p className="muted">{t('agents.noTask')}</p>
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <Section
        title={task.spec.title}
        actions={
          <>
            <button disabled={busy || task.state === 'finished'} onClick={() => act(() => client.call('tasks.run', { task: task.id, maxSteps: 5 }))}>
              {t('agents.run')}
            </button>
            <button disabled={busy || task.state !== 'running'} onClick={() => act(() => client.call('tasks.pause', { task: task.id }))}>
              {t('agents.pause')}
            </button>
          </>
        }
      >
        <dl className="facts">
          <dt>{t('agents.goal')}</dt>
          <dd>{task.spec.goal}</dd>
          <dt>{t('common.state')}</dt>
          <dd>
            <span className={`dot ${task.state}`} /> {task.state} {task.error && <span className="muted">({task.error})</span>}
          </dd>
          <dt>{t('agents.provider')}</dt>
          <dd>{task.spec.provider}</dd>
          <dt>{t('agents.steps')}</dt>
          <dd>
            {task.steps} / {task.spec.maxSteps}
          </dd>
          <dt>{t('agents.tokens')}</dt>
          <dd>{task.tokens}</dd>
        </dl>
        <ErrorLine error={error} />
      </Section>
      <div className="timeline">
        {steps?.map((e) => {
          const p = e.payload as unknown as StepPayload
          return (
            <article key={e.seq} className="step">
              <header>
                <strong>{t('agents.step', { n: p.step })}</strong>{' '}
                <span className="muted">
                  {timeOf(e.ts)} · {p.provider} · {p.agent.slice(-6)}
                </span>{' '}
                <Label label={e.label} />
              </header>
              {p.text && <p className="agent-text">{p.text}</p>}
              {p.toolCalls.map((call) => {
                const result = p.results.find((r) => r.id === call.id)
                return (
                  <div key={call.id} className={`tool ${result?.ok ? 'ok' : 'failed'}`}>
                    <code>
                      {call.name}({JSON.stringify(call.arguments).slice(0, 200)})
                    </code>
                    {result && <pre>{result.content.slice(0, 1200)}</pre>}
                  </div>
                )
              })}
            </article>
          )
        })}
      </div>
    </>
  )
}
