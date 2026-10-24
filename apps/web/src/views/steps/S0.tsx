import { useMemo, useState } from 'react'
import type { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'
import { useKernel, useQuery } from '../../api.ts'
import { useI18n, type Key } from '../../i18n.ts'
import { ErrorLine, Section } from '../common.tsx'

// S0 in detail: the question the stage answers, its inputs, how area and speed
// are computed, how large a model would fit, the verdict, and a form
// that reruns the real estimator with another model or bit width.

type Range = { low: number; typ: number; high: number }

interface Item {
  item: string
  areaMm2: Range | null
  source: string
  confidence: string | null
}

interface Report {
  model: { title?: string; params: number; dim?: number; layers?: number; heads?: number; kvHeads?: number; vocab?: number }
  assumptions: Record<string, unknown> & { weightBits: number; clockMHz: number; parallelMacs: number; contextLength: number; kvLocation: string; weightStorage: string }
  process: { id: string; title: string }
  items: Item[]
  missing: { item?: string; what?: string; how?: string }[]
  performance: { macsPerToken: number; cyclesPerToken: number; computeMsPerToken: number; offchipMsPerToken?: number; msPerToken: number; tokensPerSecond: number; bottleneck: string }
  area?: { coreMm2: Range; dieMm2: Range }
  cost: { cny: Range | null; rule: string }
  reverse: { areaLimitMm2: number; maxParams: Record<string, Range> }
  conclusion: { feasibility: string; conditions: string[]; reasons: string[] }
  suggestions: string[]
}

const IO_ITEM = 'IO ring, seal ring and unused die area'
const ITEM_KEYS: [string, Key][] = [
  ['weight storage', 's0.item.weights'],
  ['KV cache (off chip)', 's0.item.kvOffchip'],
  ['KV cache', 's0.item.kv'],
  ['activation buffers', 's0.item.activations'],
  ['compute units', 's0.item.compute'],
  ['control and interfaces', 's0.item.control'],
  ['logic placement and routing', 's0.item.routing'],
  ['power network', 's0.item.power'],
  [IO_ITEM, 's0.item.io'],
]
const COLORS = ['#2f6fed', '#7a5af8', '#12a594', '#e5883b', '#d6409f', '#8d99ae', '#b5838d']
const MODELS = ['project', 'stories260K', 'qwen2.5-0.5b', 'llama3.1-8b']
const BITS = [2, 3, 4, 8]

async function json<T>(client: KernelClient, hash: string): Promise<T> {
  return (await client.download(hash)).json() as Promise<T>
}

export function S0Detail({ project, onBack }: { project: string; onBack(): void; onProject?(project: string): void }) {
  const { t } = useI18n()
  const { data: runs } = useQuery((c) => c.call('runs.list', { project, limit: 1000 }), [project])
  const run = runs?.runs.filter((r) => r.plugin === 'estimator' && r.handler === 'estimate' && r.execution === 'exited_zero').at(-1)
  const imported = runs?.runs.filter((r) => r.plugin === 'model-llama' && r.handler === 'import' && r.execution === 'exited_zero').at(-1)
  const reportHash = run?.outputs.find((o) => o.name === 'report')?.hash
  const requestHash = run?.inputs.find((i) => i.name === 'request')?.hash
  const { data: report, error } = useQuery((c) => (reportHash ? json<Report>(c, reportHash) : Promise.resolve(undefined)), [reportHash], false)
  const { data: request } = useQuery((c) => (requestHash ? json<Record<string, unknown>>(c, requestHash) : Promise.resolve(undefined)), [requestHash], false)
  const { data: verdicts } = useQuery((c) => c.call('verdicts.list', { check: 'estimate.feasibility', limit: 1000 }), [])
  const verdict = verdicts?.verdicts.find((v) => v.run === run?.id)

  return (
    <>
      <button className="link step-back" onClick={onBack}>
        ← {t('s0.back')}
      </button>
      <ErrorLine error={error} />
      {!run && runs && <p className="muted">{t('flow.noResults')}</p>}
      {imported && <Census run={imported} />}
      {report && run && (
        <>
          <Answer report={report} />
          <Inputs report={report} />
          <AreaSection report={report} />
          <SpeedSection report={report} />
          <ReverseSection report={report} />
          <Section title={t('s0.verdictTitle')}>
            <ul className="flow-checks">
              <li>
                <span className={`pill ${verdict?.verdict === 'pass' ? 'ok' : verdict ? 'bad' : ''}`}>{t(`flow.verdict.${verdict?.verdict ?? 'none'}` as Key)}</span>
                {t('flow.check.feasibility')}
              </li>
            </ul>
            <Raw report={report} />
          </Section>
          <TryIt run={run} request={request} base={report} />
        </>
      )}
    </>
  )
}

const GROUPS: [string, Key, string][] = [
  ['embedding', 's0.g.embedding', '#12a594'],
  ['attention', 's0.g.attention', '#2f6fed'],
  ['mlp', 's0.g.mlp', '#7a5af8'],
  ['norm', 's0.g.norm', '#8d99ae'],
  ['lm_head', 's0.g.lm_head', '#e5883b'],
  ['unused', 's0.g.unused', '#c7ccd6'],
  ['other', 's0.g.other', '#d64545'],
]
const ROLE_KEYS: Record<string, Key> = {
  config: 's0.role.config', generation_config: 's0.role.generation_config', tokenizer: 's0.role.tokenizer', tokenizer_aux: 's0.role.tokenizer_aux',
  weights: 's0.role.weights', weights_index: 's0.role.weights_index', license: 's0.role.license',
}
const CHECK_KEYS: Record<string, [Key, Key]> = {
  classified: ['s0.check.classifiedOk', 's0.check.classifiedBad'],
  bytes: ['s0.check.bytesOk', 's0.check.bytesBad'],
  layers: ['s0.check.layersOk', 's0.check.layersBad'],
  config: ['s0.check.configOk', 's0.check.configBad'],
  output: ['s0.check.outputOk', 's0.check.outputBad'],
}

/** The imported files and their tensor census: what the weights are, before anything is computed. */
function Census({ run }: { run: RunRecord }) {
  const { t } = useI18n()
  const f = useFormat()
  const hash = (name: string) => run.outputs.find((o) => o.name === name)?.hash
  const { data: census } = useQuery((c) => json<any>(c, hash('census')!), [hash('census')], false)
  const { data: bundle } = useQuery((c) => json<any>(c, hash('bundle')!), [hash('bundle')], false)
  if (!census || !bundle) return null
  const totals = census.totals
  const stored = Object.values(totals.byGroup as Record<string, { params: number }>).reduce((s, g) => s + g.params, 0)
  const h = census.hyper
  const v = (k: string) => h[k]?.value
  const macs = census.macsPerToken
  return (
    <>
      <Section title={t('s0.import')}>
        <p className="flow-what">{t('s0.importIntro', { name: bundle.name, source: bundle.source, format: bundle.weightFormat })}</p>
        <table className="s0-table">
          <thead>
            <tr>
              <th>{t('s0.col.file')}</th>
              <th>{t('s0.col.role')}</th>
              <th>{t('s0.col.size')}</th>
            </tr>
          </thead>
          <tbody>
            {bundle.files.map((file: any) => (
              <tr key={file.name}>
                <td>
                  <code>{file.name}</code>
                </td>
                <td>{t(ROLE_KEYS[file.role] ?? 's0.role.other')}</td>
                <td>{file.bytes >= 1e6 ? `${f.num(file.bytes / 1e6, 1)} MB` : `${f.num(file.bytes / 1e3, 1)} KB`}</td>
              </tr>
            ))}
            {bundle.ignored.map((file: any) => (
              <tr key={file.name} className="muted">
                <td>
                  <code>{file.name}</code>
                </td>
                <td colSpan={2}>{t(file.reason === 'OTHER_FORMAT' ? 's0.ignore.otherFormat' : 's0.ignore.notModel')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      <Section title={t('s0.census')}>
        <p className="s0-formula">
          {t('s0.censusSummary', { tensors: f.int(totals.tensors), params: f.int(totals.parameters), layers: v('layers') ?? '?', hidden: v('hidden') ?? '?', vocab: v('vocab') ?? '?' })}
          {totals.duplicateOutput && ` ${t('s0.duplicate', { stored: f.int(totals.storedParameters), tensor: totals.duplicateOutput })}`}
        </p>
        <h4>{t('s0.byGroup')}</h4>
        <div className="s0-bar">
          {GROUPS.filter(([g]) => totals.byGroup[g]).map(([g, , color]) => (
            <div key={g} className="s0-seg" style={{ width: `${(100 * totals.byGroup[g].params) / stored}%`, background: color }} />
          ))}
        </div>
        <div className="s0-legend">
          {GROUPS.filter(([g]) => totals.byGroup[g]).map(([g, label, color]) => (
            <span key={g}>
              <span className="s0-swatch" style={{ background: color }} />
              {t(label)} {f.int(totals.byGroup[g].params)} ({f.num((100 * totals.byGroup[g].params) / stored, 1)}%)
            </span>
          ))}
        </div>
        <h4>{t('s0.structure')}</h4>
        <dl className="facts">
          {(
            [
              ['layers', 's1.h.layers'],
              ['hidden', 's1.h.hidden'],
              ['heads', 's1.h.heads'],
              ['kvHeads', 's1.h.kvHeads'],
              ['headDim', 's1.h.headDim'],
              ['intermediate', 's1.h.intermediate'],
              ['vocab', 's1.h.vocab'],
            ] as [string, Key][]
          )
            .filter(([k]) => v(k) !== undefined)
            .map(([k, label]) => (
              <div key={k} style={{ display: 'contents' }}>
                <dt>{t(label)}</dt>
                <dd>
                  <strong>{f.int(v(k))}</strong> <small className="muted">· {h[k].source}</small>
                </dd>
              </div>
            ))}
        </dl>
        <h4>{t('s0.macsTitle')}</h4>
        <p>{t('s0.macs', { gemm: f.int(macs.gemm), attention: macs.attention === null ? '?' : f.int(macs.attention), context: macs.context, total: f.int(macs.total) })}</p>
        <h4>{t('s0.checks')}</h4>
        <ul className="flow-checks">
          {census.checks.map((c: any) => (
            <li key={c.id}>
              <span className={`pill ${c.ok ? 'ok' : 'bad'}`}>{t(c.ok ? 'flow.verdict.pass' : 'flow.verdict.fail')}</span>
              {t(CHECK_KEYS[c.id]?.[c.ok ? 0 : 1] ?? 's0.check.other', { count: c.params.count ?? c.params.differ?.length ?? 0, layers: c.params.layers ?? '' })}
              {!c.ok && (c.params.examples ?? []).length > 0 && <small className="muted"> · {(c.params.examples as string[]).join('; ')}</small>}
            </li>
          ))}
        </ul>
      </Section>
    </>
  )
}

/** Big answer: does it fit, and why, in one sentence derived from the numbers. */
function Answer({ report, compact = false }: { report: Report; compact?: boolean }) {
  const { t } = useI18n()
  const f = useFormat()
  const die = report.area?.dieMm2
  const limit = report.reverse.areaLimitMm2
  const feasibility = report.conclusion.feasibility
  const reason = !die
    ? t('s0.why.missing', { count: report.missing.length })
    : feasibility === 'infeasible' && die.low > limit
      ? t('s0.why.tooBig', { low: f.mm2(die.low), limit: f.mm2(limit) })
      : feasibility === 'infeasible'
        ? t('s0.why.unavailable')
        : feasibility === 'feasible'
          ? t('s0.why.fits', { high: f.mm2(die.high), limit: f.mm2(limit) })
          : t('s0.why.conditional', { typ: f.mm2(die.typ), high: f.mm2(die.high), limit: f.mm2(limit) })
  const bits = report.assumptions.weightBits
  const max = report.reverse.maxParams[String(bits)]
  const body = (
    <div className={`s0-answer ${feasibility}`}>
      <strong>{t(`feasibility.${feasibility}` as Key)}</strong>
      <span>{reason}</span>
      {feasibility === 'infeasible' && max && <span>{t('s0.why.suggest', { bits, max: f.int(max.typ), params: f.int(report.model.params) })}</span>}
    </div>
  )
  if (compact) return body
  return (
    <Section title={t('s0.title')}>
      <p className="flow-what">{t('s0.question', { model: modelName(report, t), params: f.int(report.model.params), process: processName(report, t), limit: f.mm2(limit) })}</p>
      {body}
    </Section>
  )
}

function Inputs({ report }: { report: Report }) {
  const { t } = useI18n()
  const f = useFormat()
  const m = report.model
  const a = report.assumptions
  const groups: [Key, [Key, string | undefined][]][] = [
    [
      's0.in.model',
      [
        ['s0.in.name', modelName(report, t)],
        ['flow.fact.parameters', f.int(m.params)],
        ['s0.in.layers', m.layers === undefined ? undefined : f.int(m.layers)],
        ['s0.in.dim', m.dim === undefined ? undefined : f.int(m.dim)],
        ['s0.in.heads', m.heads === undefined ? undefined : `${m.heads} / ${m.kvHeads}`],
        ['s0.in.vocab', m.vocab === undefined ? undefined : f.int(m.vocab)],
      ],
    ],
    [
      's0.in.process',
      [
        ['s0.in.processName', processName(report, t)],
        ['s0.in.limit', `${f.mm2(report.reverse.areaLimitMm2)} mm²`],
        ['s0.in.clock', `${f.num(a.clockMHz, 0)} MHz`],
        ['s0.in.macs', f.int(a.parallelMacs)],
      ],
    ],
    [
      's0.in.assumptions',
      [
        ['s0.in.weightBits', `${a.weightBits} bit`],
        ['s0.in.storage', storageName(a.weightStorage, t)],
        ['s0.in.context', f.int(a.contextLength)],
        ['s0.in.kv', t(a.kvLocation === 'onchip' ? 's0.onchip' : 's0.offchip')],
      ],
    ],
  ]
  return (
    <Section title={t('s0.step1')}>
      <div className="s0-inputs">
        {groups.map(([title, rows]) => (
          <div key={title} className="flow-fact">
            <strong>{t(title)}</strong>
            <dl className="facts">
              {rows
                .filter(([, v]) => v !== undefined)
                .map(([k, v]) => (
                  <div key={k} style={{ display: 'contents' }}>
                    <dt>{t(k)}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
            </dl>
          </div>
        ))}
      </div>
    </Section>
  )
}

function AreaSection({ report, title = 's0.step2' }: { report: Report; title?: Key }) {
  const { t } = useI18n()
  const f = useFormat()
  if (!report.area) return null
  const weights = report.items.find((i) => i.item === 'weight storage')
  const bits = report.model.params * report.assumptions.weightBits
  const io = report.items.find((i) => i.item === IO_ITEM)?.areaMm2
  const core = report.items.filter((i) => i.item !== IO_ITEM && i.areaMm2 && i.areaMm2.typ > 0)
  const die = report.area.dieMm2
  const limit = report.reverse.areaLimitMm2
  const scale = Math.max(die.high, limit) * 1.08
  return (
    <Section title={t(title)}>
      {weights?.areaMm2 && (
        <p className="s0-formula">
          {t('s0.formula', {
            params: f.int(report.model.params),
            bits: report.assumptions.weightBits,
            total: f.int(bits),
            density: f.num((weights.areaMm2.typ * 1e6) / bits, 2),
            area: f.num(weights.areaMm2.typ, 3),
            confidence: t(confidenceKey(weights.confidence)),
          })}
        </p>
      )}
      <h4>{t('s0.dieBar', { typ: f.mm2(die.typ) })}</h4>
      <div className="s0-bar" title={`${f.mm2(die.low)} – ${f.mm2(die.high)} mm²`}>
        <div className="s0-range" style={{ left: `${(100 * die.low) / scale}%`, width: `${(100 * (die.high - die.low)) / scale}%` }} />
        <div className="s0-seg" style={{ width: `${(100 * report.area.coreMm2.typ) / scale}%`, background: COLORS[0] }} />
        {io && <div className="s0-seg" style={{ width: `${(100 * io.typ) / scale}%`, background: '#c7ccd6' }} />}
        <div className="s0-limit" style={{ left: `${(100 * limit) / scale}%` }} />
      </div>
      <div className="s0-legend">
        <span>
          <span className="s0-swatch" style={{ background: COLORS[0] }} />
          {t('s0.core')} {f.mm2(report.area.coreMm2.typ)} mm²
        </span>
        {io && (
          <span>
            <span className="s0-swatch" style={{ background: '#c7ccd6' }} />
            {t('s0.item.io')} {f.mm2(io.typ)} mm²
          </span>
        )}
        <span className="error">┆ {t('s0.limit', { limit: f.mm2(limit) })}</span>
        <span className="muted">
          <span className="s0-swatch" style={{ background: 'rgba(47, 111, 237, .25)' }} />
          {t('s0.range', { low: f.mm2(die.low), high: f.mm2(die.high) })}
        </span>
      </div>
      <p className="muted">{t('s0.dieNote')}</p>

      <h4>{t('s0.coreBar', { typ: f.mm2(report.area.coreMm2.typ) })}</h4>
      <div className="s0-bar">
        {core.map((i, n) => (
          <div key={i.item} className="s0-seg" style={{ width: `${(100 * i.areaMm2!.typ) / report.area!.coreMm2.typ}%`, background: COLORS[n % COLORS.length] }} title={`${t(itemKey(i.item))} ${f.num(i.areaMm2!.typ, 3)} mm²`} />
        ))}
      </div>
      <table className="s0-table">
        <thead>
          <tr>
            <th>{t('s0.col.item')}</th>
            <th>{t('s0.col.low')}</th>
            <th>{t('s0.col.typ')}</th>
            <th>{t('s0.col.high')}</th>
            <th>{t('s0.col.share')}</th>
            <th>{t('s0.col.confidence')}</th>
          </tr>
        </thead>
        <tbody>
          {report.items.map((i) => {
            const n = core.indexOf(i)
            return (
              <tr key={i.item} title={i.source}>
                <td>
                  <span className="s0-swatch" style={{ background: n >= 0 ? COLORS[n % COLORS.length] : '#c7ccd6' }} />
                  {t(itemKey(i.item))}
                </td>
                <td>{i.areaMm2 ? f.num(i.areaMm2.low, 3) : '—'}</td>
                <td>
                  <strong>{i.areaMm2 ? f.num(i.areaMm2.typ, 3) : '—'}</strong>
                </td>
                <td>{i.areaMm2 ? f.num(i.areaMm2.high, 3) : '—'}</td>
                <td>{i.areaMm2 ? `${f.num((100 * i.areaMm2.typ) / die.typ, 1)}%` : ''}</td>
                <td>
                  <span className={`label ${i.confidence ?? ''}`}>{t(confidenceKey(i.confidence))}</span>
                </td>
              </tr>
            )
          })}
          <tr>
            <td>
              <strong>{t('s0.dieTotal')}</strong>
            </td>
            <td>{f.num(die.low, 3)}</td>
            <td>
              <strong>{f.num(die.typ, 3)}</strong>
            </td>
            <td>{f.num(die.high, 3)}</td>
            <td>100%</td>
            <td />
          </tr>
        </tbody>
      </table>
      <p className="muted">{t('s0.unitNote')}</p>
    </Section>
  )
}

function SpeedSection({ report }: { report: Report }) {
  const { t } = useI18n()
  const f = useFormat()
  const p = report.performance
  const offchip = p.offchipMsPerToken ?? 0
  const max = Math.max(p.computeMsPerToken, offchip)
  return (
    <Section title={t('s0.step3')}>
      <p className="s0-formula">
        {t('s0.speedFormula', {
          macs: f.int(p.macsPerToken),
          units: report.assumptions.parallelMacs,
          cycles: f.int(p.cyclesPerToken),
          clock: f.num(report.assumptions.clockMHz, 0),
          ms: f.num(p.msPerToken, 3),
          tps: p.tokensPerSecond >= 10 ? f.num(p.tokensPerSecond, 0) : f.sig(p.tokensPerSecond),
        })}
      </p>
      <p className="muted">{t('s0.speedPreArch')}</p>
      {[
        ['s0.compute', p.computeMsPerToken, p.bottleneck === 'compute'],
        ['s0.offchipIo', offchip, p.bottleneck !== 'compute'],
      ].map(([label, ms, slow]) => (
        <div key={label as string} className="s0-row">
          <span>{t(label as Key)}</span>
          <div className="s0-bar small">
            <div className="s0-seg" style={{ width: `${(100 * (ms as number)) / max}%`, background: slow ? '#e5883b' : '#8d99ae' }} />
          </div>
          <span>
            {f.num(ms, 3)} ms {slow ? `· ${t('s0.bottleneck')}` : ''}
          </span>
        </div>
      ))}
    </Section>
  )
}

function ReverseSection({ report }: { report: Report }) {
  const { t } = useI18n()
  const f = useFormat()
  const params = report.model.params
  const rows = BITS.filter((b) => report.reverse.maxParams[String(b)]).map((b) => [b, report.reverse.maxParams[String(b)]!] as const)
  const scale = Math.max(params, ...rows.map(([, r]) => r.low)) * 1.08
  return (
    <Section title={t('s0.step4')}>
      <p className="flow-what">{t('s0.reverseIntro', { limit: f.mm2(report.reverse.areaLimitMm2) })}</p>
      {rows.map(([bits, r]) => {
        const fits = params <= r.typ
        return (
          <div key={bits} className="s0-row">
            <span>
              {bits} bit{bits === report.assumptions.weightBits ? ` · ${t('s0.current')}` : ''}
            </span>
            <div className="s0-bar small">
              <div className="s0-range" style={{ left: `${(100 * r.high) / scale}%`, width: `${(100 * Math.max(0, r.low - r.high)) / scale}%` }} />
              <div className="s0-seg" style={{ width: `${(100 * r.typ) / scale}%`, background: fits ? '#1f9d55' : '#d64545' }} />
              <div className="s0-limit model" style={{ left: `${(100 * params) / scale}%` }} />
            </div>
            <span className={fits ? 'ok-text' : 'error'}>
              {t('s0.maxParams', { typ: f.int(r.typ) })} · {t(fits ? 's0.fits' : 's0.doesNotFit')}
            </span>
          </div>
        )
      })}
      <p className="muted">{t('s0.reverseNote', { params: f.int(params) })}</p>
    </Section>
  )
}

/** The plugin's own wording, for whoever wants it. */
function Raw({ report }: { report: Report }) {
  const { t } = useI18n()
  const lines = [...report.conclusion.reasons, ...report.conclusion.conditions, ...report.suggestions, ...(report.cost.cny ? [] : [report.cost.rule])]
  if (lines.length === 0) return null
  return (
    <details className="s0-raw">
      <summary>{t('s0.raw')}</summary>
      <ul>
        {lines.map((l) => (
          <li key={l}>{l}</li>
        ))}
      </ul>
    </details>
  )
}

/** Reruns the estimator with another model or bit width; not bound to the project. */
function TryIt({ run, request, base }: { run: RunRecord; request: Record<string, unknown> | undefined; base: Report }) {
  const { t } = useI18n()
  const { client } = useKernel()
  const [model, setModel] = useState('stories260K')
  const [bits, setBits] = useState(base.assumptions.weightBits)
  const [result, setResult] = useState<Report | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const go = async () => {
    setBusy(true)
    setError(null)
    try {
      const params = { ...(request ?? {}), weightBits: bits, ...(model === 'project' ? {} : { model: { preset: model } }) }
      const inputs = run.inputs.filter((i) => i.name === 'profile' || i.name === 'library').map(({ name, hash }) => ({ name, hash }))
      const started = await client.call('runs.start', { plugin: 'estimator', handler: 'estimate', params, inputs })
      const finished = await client.call('runs.wait', { run: started.id, timeoutMs: 120_000 })
      if (finished.execution !== 'exited_zero') throw new Error(finished.error ?? finished.execution)
      setResult(await json<Report>(client, finished.outputs.find((o) => o.name === 'report')!.hash))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Section title={t('s0.try')}>
      <p className="flow-what">{t('s0.tryIntro')}</p>
      <div className="form inline">
        <label>
          {t('s0.in.model')}
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            {MODELS.map((m) => (
              <option key={m} value={m}>
                {m === 'project' ? t('s0.projectModel') : m}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('s0.in.weightBits')}
          <select value={bits} onChange={(e) => setBits(Number(e.target.value))}>
            {BITS.map((b) => (
              <option key={b} value={b}>
                {b} bit
              </option>
            ))}
          </select>
        </label>
        <button className="active" disabled={busy} onClick={go}>
          {busy ? t('playground.running') : t('s0.tryRun')}
        </button>
      </div>
      <ErrorLine error={error} />
      {result && (
        <div className="s0-try">
          <p>
            <strong>{modelName(result, t)}</strong> · {f(result)}
          </p>
          <Answer report={result} compact />
          {result.area && <AreaSection report={result} title="s0.tryArea" />}
        </div>
      )}
    </Section>
  )
  function f(r: Report) {
    return `${new Intl.NumberFormat().format(r.model.params)} · ${r.assumptions.weightBits} bit`
  }
}

function useFormat() {
  const { lang } = useI18n()
  return useMemo(() => {
    const locale = lang === 'zh' ? 'zh-CN' : 'en-US'
    const num = (v: unknown, digits = 1) => new Intl.NumberFormat(locale, { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(Number(v))
    const sig = (v: unknown) => new Intl.NumberFormat(locale, { maximumSignificantDigits: 3 }).format(Number(v))
    return { num, sig, int: (v: unknown) => new Intl.NumberFormat(locale).format(Number(v)), mm2: (v: number) => num(v, 2) }
  }, [lang])
}

function modelName(report: Report, t: (k: Key) => string): string {
  const title = report.model.title
  return !title || title === 'proxy' ? t('s0.proxyName') : title
}

const STORAGE_KEYS: [string, Key][] = [
  ['stdcell-rom', 's0.storage.stdcellRom'],
  ['openram-rom', 's0.storage.openramRom'],
  ['foundry-rom', 's0.storage.foundryRom'],
  ['sram', 's0.storage.sram'],
]

function storageName(id: string, t: (k: Key) => string): string {
  const key = STORAGE_KEYS.find(([suffix]) => id.split('.').pop()!.startsWith(suffix) || id.includes(suffix))?.[1]
  return key ? t(key) : id
}

function processName(report: Report, t: (k: Key) => string): string {
  return report.process.id === 'gf180mcu' ? t('s0.process.gf180mcu') : report.process.id === 'sephonchitech-180bcd' ? t('s0.process.sephonchitech') : report.process.title
}

function itemKey(item: string): Key {
  return ITEM_KEYS.find(([prefix]) => item.startsWith(prefix))?.[1] ?? 's0.item.other'
}

function confidenceKey(confidence: string | null): Key {
  return confidence === 'calibrated' ? 's0.conf.calibrated' : confidence === 'datasheet' ? 's0.conf.datasheet' : confidence === 'empirical' ? 's0.conf.empirical' : 's0.conf.na'
}
