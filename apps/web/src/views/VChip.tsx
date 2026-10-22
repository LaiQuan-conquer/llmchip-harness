import { useEffect, useState } from 'react'
import type { RunRecord, VerdictRecord } from '@llmchip/kernel-api'
import { useKernel, useQuery } from '../api.ts'
import { CATALOGS, useI18n, type Key } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { ErrorLine, Section } from './common.tsx'

type Mode = 'try' | 'accept' | 'conform'

/** Plugin inputs of the vchip handlers and the project slots they come from. */
const INPUTS: { name: string; slot: string; needed: (mode: Mode) => boolean }[] = [
  { name: 'tokenizer', slot: 'tokenizer', needed: () => true },
  { name: 'spec', slot: 'numeric-spec', needed: () => true },
  { name: 'weights', slot: 'l2-weights', needed: () => true },
  { name: 'evalset', slot: 'eval-set', needed: (mode) => mode !== 'try' },
  { name: 'rtl', slot: 'rtl', needed: (mode) => mode === 'accept' }, // the acceptance is of hardware
  { name: 'ir', slot: 'model-ir', needed: () => false },
  { name: 'chip', slot: 'chip-spec', needed: (mode) => mode === 'accept' }, // the design's cycles and clock
]

/** The check that judges each kind of run (flow-default). */
const CHECK: Record<string, string> = { bench: 'vchip.l3', conform: 'vchip.conformance' }

interface Sample {
  id: string
  prompt: string
  text: string
  tokens: number[]
  stop_reason: string
  error: { code: string; message: string } | null
}

interface ConformanceCheck {
  id: string
  title: string
  verdict: 'pass' | 'fail' | 'n/a' | 'not_run'
  detail: string
}

/**
 * Virtual chips (owner's draft design, L4): the project's L2 golden, float reference and RTL
 * as virtual chips of the test harness, run by the kernel plugin `vchip`. Its runs and reports
 * are artifacts; the flow's checks vchip.l3 and vchip.conformance judge them.
 */
export function VChipView({ selection, select }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t } = useI18n()
  const { client } = useKernel()
  const project = selection.project
  const { data: state } = useQuery((c) => (project ? c.call('projects.get', { project }) : Promise.resolve(undefined)), [project])
  const { data: runs } = useQuery((c) => (project ? c.call('runs.list', { project, limit: 1000 }) : Promise.resolve(undefined)), [project])
  const { data: verdicts } = useQuery(async (c) => {
    const lists = await Promise.all(Object.values(CHECK).map((check) => c.call('verdicts.list', { check, limit: 1000 })))
    return lists.flatMap((l) => l.verdicts)
  }, [])
  const [mode, setMode] = useState<Mode>('try')
  const [prompts, setPrompts] = useState('once upon a time\ntom had a')
  const [maxTokens, setMaxTokens] = useState(32)
  const [quick, setQuick] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [chosen, setChosen] = useState<string | null>(null)

  if (!project) return <Section title={t('nav.vchip')}>{<p className="muted">{t('vchip.selectProject')}</p>}</Section>
  const slots = state?.slots ?? {}
  const missing = INPUTS.filter((i) => i.needed(mode) && !slots[i.slot]).map((i) => i.slot)
  const ours = (runs?.runs ?? []).filter((r) => r.plugin === 'vchip').sort((a, b) => b.queuedAt.localeCompare(a.queuedAt))
  const verdictOf = (r: RunRecord) =>
    (verdicts ?? []).filter((v) => v.run === r.id && v.check === CHECK[r.handler]).sort((a, b) => b.createdAt.localeCompare(a.createdAt))[0]
  const selected = ours.find((r) => r.id === chosen) ?? ours[0]

  const start = async () => {
    setError(null)
    try {
      const inputs = INPUTS.filter((i) => slots[i.slot] && (mode !== 'try' || i.name !== 'evalset')).map((i) => ({ name: i.name, hash: slots[i.slot]! }))
      const lines = prompts.split('\n').map((l) => l.trim()).filter(Boolean)
      const [handler, params] =
        mode === 'try'
          ? ['bench', { prompts: lines.slice(0, 20), maxTokens }]
          : mode === 'accept'
            ? ['bench', { maxTokens: 64 }]
            : ['conform', { quick }]
      const run = await client.call('runs.start', { plugin: 'vchip', handler, params, inputs, project })
      setChosen(run.id)
      setBusy(run.id)
      let finished = await client.call('runs.wait', { run: run.id, timeoutMs: 600_000 })
      while (finished.state !== 'finished') finished = await client.call('runs.wait', { run: run.id, timeoutMs: 600_000 })
      if (finished.execution !== 'exited_zero') throw new Error(finished.error ?? finished.execution)
      // Acceptance and conformance are judged at once; a typed-in try is judged only on request.
      if (mode !== 'try') await client.call('verdicts.evaluate', { project, check: CHECK[handler]!, run: run.id })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <Section title={t('nav.vchip')}>
        <p className="muted">{t('vchip.intro')}</p>
        <ChipsOfProject slots={slots} />
        <div className="levels vchip-modes">
          {(['try', 'accept', 'conform'] as Mode[]).map((m) => (
            <label key={m} className="check">
              <input type="radio" name="vchip-mode" checked={mode === m} onChange={() => setMode(m)} data-mode={m} />
              {t(`vchip.mode.${m}` as Key)}
            </label>
          ))}
        </div>
        <p className="muted">{t(`vchip.mode.${mode}.about` as Key)}</p>
        <div className="form grid">
          {mode === 'try' && (
            <>
              <label className="wide">
                {t('vchip.prompts')}
                <textarea value={prompts} rows={3} onChange={(e) => setPrompts(e.target.value)} />
              </label>
              <label>
                {t('vchip.maxTokens')}
                <input type="number" min={1} max={4096} value={maxTokens} onChange={(e) => setMaxTokens(Number(e.target.value))} />
              </label>
            </>
          )}
          {mode === 'conform' && (
            <label className="check">
              <input type="checkbox" checked={quick} onChange={(e) => setQuick(e.target.checked)} />
              {t('vchip.quick')}
            </label>
          )}
          <button onClick={start} disabled={busy !== null || missing.length > 0 || (mode === 'try' && !prompts.trim())}>
            {busy ? t('vchip.running') : t('vchip.run')}
          </button>
        </div>
        {missing.length > 0 && <p className="error">{t('vchip.missing', { slots: missing.join(', ') })}</p>}
        <ErrorLine error={error} />
      </Section>
      <Section title={t('vchip.runs')}>
        {ours.length === 0 ? (
          <p className="muted">{t('vchip.noRuns')}</p>
        ) : (
          <table className="vchip-runs">
            <thead>
              <tr>
                <th>{t('common.time')}</th>
                <th>{t('vchip.col.kind')}</th>
                <th>{t('vchip.col.chip')}</th>
                <th>{t('vchip.col.result')}</th>
                <th>{t('vchip.col.verdict')}</th>
              </tr>
            </thead>
            <tbody>
              {ours.map((r) => (
                <tr key={r.id} className={r.id === selected?.id ? 'selected' : ''} onClick={() => setChosen(r.id)}>
                  <td className="muted">{new Date(r.queuedAt).toLocaleString()}</td>
                  <td>{t(r.handler === 'conform' ? 'vchip.kind.conform' : 'vchip.kind.bench', { prompts: Number(r.metrics.prompts ?? 0) })}</td>
                  <td>{r.metrics.chip_backend ? t(backendKey(String(r.metrics.chip_backend))) : ''}</td>
                  <td>
                    <RunState run={r} />
                  </td>
                  <td>
                    <VerdictPill verdict={verdictOf(r)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      {selected && <RunDetail run={selected} verdict={verdictOf(selected)} select={select} />}
    </>
  )
}

function backendKey(backend: string): Key {
  return backend === 'rtl' ? 'vchip.backend.rtl' : backend === 'numeric' ? 'vchip.backend.numeric' : 'vchip.backend.other'
}

/** The chips a run builds from the project's slots: the chip under test is the RTL when there is one. */
function ChipsOfProject({ slots }: { slots: Record<string, string | null | undefined> }) {
  const { t } = useI18n()
  const chips: { key: Key; slots: string[]; role: Key }[] = [
    { key: 'vchip.chip.rtl', slots: ['rtl'], role: slots.rtl ? 'vchip.role.dut' : 'vchip.role.absent' },
    { key: 'vchip.chip.l2', slots: ['numeric-spec', 'l2-weights', 'tokenizer'], role: slots.rtl ? 'vchip.role.golden' : 'vchip.role.dut' },
    { key: 'vchip.chip.ref', slots: ['model-ir'], role: slots['model-ir'] ? 'vchip.role.reference' : 'vchip.role.absent' },
  ]
  return (
    <div className="flow-facts vchip-chips">
      {chips.map((c) => {
        const present = c.slots.every((s) => slots[s])
        return (
          <div key={c.key} className={`flow-fact ${present ? 'ok' : ''}`}>
            <small>{t(c.key)}</small>
            <strong>{present ? t(c.role) : t('vchip.role.absent')}</strong>
            <small className="mono">{c.slots.join(' · ')}</small>
          </div>
        )
      })}
    </div>
  )
}

function RunState({ run }: { run: RunRecord }) {
  const { t } = useI18n()
  if (run.state !== 'finished') return <span className="pill">{t('vchip.state.running')}</span>
  if (run.execution !== 'exited_zero') return <span className="pill bad" title={run.error ?? ''}>{t('vchip.state.failed')}</span>
  const m = run.metrics
  if (run.handler === 'conform') return <span>{t('vchip.result.conform', { passed: Number(m.checks_passed), failed: Number(m.checks_failed), notRun: Number(m.checks_not_run) })}</span>
  const parts = [
    m.bit_exact === true ? t('vchip.result.bitExact') : m.bit_exact === false ? t('vchip.result.notBitExact') : null,
    typeof m.tok_s === 'number' ? t('vchip.result.tokS', { value: Math.round(m.tok_s).toLocaleString() }) : null,
    typeof m.next_token_agreement === 'number' ? t('vchip.result.agreement', { value: (m.next_token_agreement * 100).toFixed(1) }) : null,
  ].filter(Boolean)
  return <span>{parts.join(' · ') || t('vchip.result.done')}</span>
}

function VerdictPill({ verdict }: { verdict: VerdictRecord | undefined }) {
  const { t, message } = useI18n()
  if (!verdict) return <span className="muted">{t('vchip.notJudged')}</span>
  const tone = verdict.verdict === 'pass' ? 'ok' : verdict.verdict === 'fail' ? 'bad' : 'warn'
  return (
    <span className={`pill ${tone}`} title={verdict.reasons.map(message).join('\n')}>
      {t(`flow.verdict.${verdict.verdict}` as Key)}
    </span>
  )
}

function RunDetail({ run, verdict, select }: { run: RunRecord; verdict: VerdictRecord | undefined; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t, message } = useI18n()
  const { client } = useKernel()
  const [samples, setSamples] = useState<Sample[] | null>(null)
  const [checks, setChecks] = useState<ConformanceCheck[] | null>(null)
  const output = (name: string) => run.outputs.find((o) => o.name === name)?.hash
  const outputsHash = output('outputs')
  const reportHash = output('report')
  useEffect(() => {
    setSamples(null)
    setChecks(null)
    if (run.handler === 'bench' && outputsHash) {
      void client
        .download(outputsHash)
        .then((r) => r.text())
        .then((text) => setSamples(text.trim().split('\n').filter(Boolean).map((l) => JSON.parse(l) as Sample)))
    }
    if (run.handler === 'conform' && reportHash) {
      void client
        .download(reportHash)
        .then((r) => r.json())
        .then((body: { checks: ConformanceCheck[] }) => setChecks(body.checks))
    }
  }, [client, run.id, run.handler, outputsHash, reportHash])
  const checkTitle = (c: ConformanceCheck) => {
    const key = `vchip.t.${c.id.replace('-', '')}`
    return key in CATALOGS.zh ? t(key as Key) : c.title
  }
  const m = run.metrics
  const num = (v: unknown, digits = 0) => (typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: digits }) : '—')
  const yesNo = (v: unknown) => (v === true ? t('common.yes') : v === false ? t('common.no') : '—')
  const facts: { label: Key; value: string; good?: boolean }[] =
    run.handler === 'bench'
      ? [
          { label: 'vchip.fact.chip', value: m.chip_backend ? t(backendKey(String(m.chip_backend))) : '—' },
          { label: 'vchip.fact.prompts', value: t('vchip.fact.promptsValue', { prompts: num(m.prompts), tokens: num(m.max_tokens) }) },
          { label: 'vchip.fact.bitExact', value: yesNo(m.bit_exact), good: typeof m.bit_exact === 'boolean' ? m.bit_exact : undefined },
          { label: 'vchip.fact.tokS', value: typeof m.tok_s === 'number' ? t('vchip.fact.tokSValue', { value: num(m.tok_s), clock: num(m.clock_mhz, 2) }) : '—' },
          {
            label: 'vchip.fact.cycles',
            value:
              typeof m.cycles_per_token_min === 'number'
                ? t('vchip.fact.cyclesValue', { min: num(m.cycles_per_token_min), max: num(m.cycles_per_token_max), design: num(m.design_cycles_per_token) })
                : '—',
            good: typeof m.cycles_match_design === 'boolean' ? m.cycles_match_design : undefined,
          },
          { label: 'vchip.fact.hostCycles', value: num(m.host_cycles_per_token) },
          { label: 'vchip.fact.trusted', value: yesNo(m.measurement_trusted), good: m.chip_backend === 'rtl' ? m.measurement_trusted === true : undefined },
          { label: 'vchip.fact.agreement', value: typeof m.next_token_agreement === 'number' ? `${(m.next_token_agreement * 100).toFixed(1)}%` : '—' },
          { label: 'vchip.fact.perplexity', value: typeof m.perplexity === 'number' ? t('vchip.fact.perplexityValue', { chip: num(m.perplexity, 3), reference: num(m.reference_perplexity, 3) }) : '—' },
          { label: 'vchip.fact.errors', value: num(m.errors), good: typeof m.errors === 'number' ? m.errors === 0 : undefined },
        ]
      : [
          { label: 'vchip.fact.chip', value: m.chip_backend ? t(backendKey(String(m.chip_backend))) : '—' },
          { label: 'vchip.fact.passed', value: num(m.checks_passed), good: true },
          { label: 'vchip.fact.failed', value: num(m.checks_failed), good: typeof m.checks_failed === 'number' ? m.checks_failed === 0 : undefined },
          { label: 'vchip.fact.notRun', value: num(m.checks_not_run), good: typeof m.checks_not_run === 'number' ? m.checks_not_run === 0 : undefined },
          { label: 'vchip.fact.quick', value: yesNo(m.quick) },
        ]
  return (
    <Section title={t(run.handler === 'conform' ? 'vchip.detail.conform' : 'vchip.detail.bench')}>
      {run.state === 'finished' && run.execution !== 'exited_zero' && <p className="error">{run.error}</p>}
      {run.state === 'finished' && run.execution === 'exited_zero' && (
        <>
          <div className="flow-facts">
            {facts.map((f) => (
              <div key={f.label} className={`flow-fact ${f.good === true ? 'ok' : f.good === false ? 'bad' : ''}`}>
                <small>{t(f.label)}</small>
                <strong>{f.value}</strong>
              </div>
            ))}
          </div>
          <p>
            {t('vchip.judgedBy', { check: CHECK[run.handler] ?? '' })} <VerdictPill verdict={verdict} />
          </p>
          {verdict && verdict.reasons.length > 0 && (
            <ul className="muted">
              {verdict.reasons.map((r, i) => (
                <li key={i}>{message(r)}</li>
              ))}
            </ul>
          )}
          <div className="flow-outputs">
            {run.outputs.map((o) => (
              <button key={o.name} onClick={() => select({ artifact: o.hash }, 'artifacts')}>
                {t(o.name === 'outputs' ? 'vchip.open.outputs' : 'vchip.open.report')}
              </button>
            ))}
          </div>
          {samples && (
            <div className="vchip-samples">
              {samples.slice(0, 20).map((s) => (
                <div key={s.id} className="vchip-sample">
                  <p>
                    <strong>{s.prompt}</strong>
                    <span className="generated vchip-continuation">{s.text}</span>
                  </p>
                  <small className="muted">
                    {s.error ? t('vchip.sample.error', { message: s.error.message }) : t('vchip.sample.stats', { tokens: s.tokens.length, stop: t(stopKey(s.stop_reason)) })}
                  </small>
                </div>
              ))}
            </div>
          )}
          {checks && (
            <table className="vchip-checks">
              <thead>
                <tr>
                  <th>{t('vchip.col.check')}</th>
                  <th>{t('vchip.col.title')}</th>
                  <th>{t('vchip.col.outcome')}</th>
                  <th>{t('vchip.col.detail')}</th>
                </tr>
              </thead>
              <tbody>
                {checks.map((c) => (
                  <tr key={c.id}>
                    <td className="mono nowrap">{c.id}</td>
                    <td>{checkTitle(c)}</td>
                    <td>
                      <span className={`pill ${c.verdict === 'pass' ? 'ok' : c.verdict === 'fail' ? 'bad' : c.verdict === 'not_run' ? 'warn' : ''}`}>{t(outcomeKey(c.verdict))}</span>
                    </td>
                    <td className="muted">{c.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </Section>
  )
}

function stopKey(reason: string): Key {
  return reason === 'stop_token' ? 'vchip.stop.token' : reason === 'context' ? 'vchip.stop.context' : reason === 'error' ? 'vchip.stop.error' : 'vchip.stop.maxTokens'
}

function outcomeKey(v: ConformanceCheck['verdict']): Key {
  return v === 'pass' ? 'vchip.outcome.pass' : v === 'fail' ? 'vchip.outcome.fail' : v === 'not_run' ? 'vchip.outcome.notRun' : 'vchip.outcome.na'
}
