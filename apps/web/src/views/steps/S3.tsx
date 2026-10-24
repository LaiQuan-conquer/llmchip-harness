import { useMemo, useState } from 'react'
import type { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'
import { useKernel, useQuery } from '../../api.ts'
import { useI18n, type Key } from '../../i18n.ts'
import { ErrorLine, Section } from '../common.tsx'
import { designArch, exploreArch, SPATIAL, SPATIAL_BASE, type ArchPoint, type SpatialPoint, type StepState } from '../modelPipeline.ts'
import { useModelChoices } from './S1.tsx'
import { SpatialAnswer, SpatialArea, SpatialCompute, SpatialSpace, SpatialSpeed } from './S3Spatial.tsx'

// S3 in detail: how the chip computes and stores (lanes, operand buffers, the external
// memory interface, the clock), its area against the die, and its speed. The speed is the
// cycle count of the microcode the chip runs (arch-taalas, llmchip_rtl.perf), shown next to
// what the RTL simulation (S5), the gate-level simulation and static timing (S7) measured.
// The design space shows every alternative with the same exact model. An imported model that
// cannot become a chip gets the same analysis, with what blocks it.

type Json = any

const OPS: Record<string, Key> = {
  ATTN: 's3.op.ATTN', MATVEC: 's3.op.MATVEC', RMS: 's3.op.RMS', SILU: 's3.op.SILU', ADD: 's3.op.ADD', ROPE: 's3.op.ROPE',
  TRACE: 's3.op.TRACE', ARGMAX: 's3.op.ARGMAX', EMB: 's3.op.EMB', END: 's3.op.END',
}
const KINDS = ['memory', 'multiply', 'divide', 'control'] as const
const BOTTLENECKS: Record<string, Key> = {
  'external memory': 's3.bottleneck.external_memory', multiplier: 's3.bottleneck.multiplier', control: 's3.bottleneck.control',
  compute: 's3.bottleneck.control', 'off-chip interface': 's3.bottleneck.external_memory',
}
const KIND_COLOR: Record<string, string> = { memory: '#e5883b', multiply: '#2f6fed', divide: '#7a5af8', control: '#8d99ae' }
const ITEMS: Record<string, Key> = {
  'weight storage': 's3.item.weights', 'KV cache (off chip)': 's3.item.kv', 'activation buffers': 's3.item.buffers', 'compute units': 's3.item.compute',
  'control and interfaces': 's3.item.control', 'power network': 's3.item.power', 'IO ring, seal ring and unused die area': 's3.item.io',
}
const RUN_STEPS: [string, Key][] = [['source', 's3.run.source'], ['explore', 's3.run.explore'], ['design', 's3.run.design'], ['bind', 's3.run.bind'], ['verify', 's3.run.verify'], ['archCheck', 's3.run.archCheck']]

async function json(client: KernelClient, hash: string | undefined): Promise<Json> {
  return hash ? (await client.download(hash)).json() : undefined
}
const out = (r: RunRecord | undefined, name: string) => r?.outputs.find((o) => o.name === name)?.hash
const inp = (r: RunRecord | undefined, name: string) => r?.inputs.find((i) => i.name === name)?.hash
const latest = (runs: RunRecord[], plugin: string, handler: string) => runs.filter((r) => r.plugin === plugin && r.handler === handler && r.execution === 'exited_zero').at(-1)

function useFormat() {
  const { lang } = useI18n()
  return useMemo(() => {
    const locale = lang === 'zh' ? 'zh-CN' : 'en-US'
    const num = (v: number, d = 1) => new Intl.NumberFormat(locale, { maximumFractionDigits: d }).format(v)
    return {
      num,
      int: (v: number) => new Intl.NumberFormat(locale).format(Math.round(v)),
      pct: (v: number) => `${num(100 * v, 1)}%`,
      tps: (v: number) => (v >= 0.1 ? `${num(v, 1)} tok/s` : `${num(v * 60, 2)} tok/min`),
      bytes: (b: number) => (b >= 1e9 ? `${num(b / 1e9, 2)} GB` : b >= 1e6 ? `${num(b / 1e6, 1)} MB` : `${num(b / 1e3, 1)} KB`),
      mm2: (v: number | undefined) => (v === undefined ? '—' : v >= 100 ? `${num(v, 0)} mm²` : `${num(v, 3)} mm²`),
    }
  }, [lang])
}

type Buffers = 'none' | 'attention' | 'all'
/** The buffer choice of a chip spec or point (older specs say true / false). */
const buffersOf = (v: unknown): Buffers => (v === true ? 'all' : v === false || v === undefined ? 'none' : (v as Buffers))

function pointText(t: (k: Key, p?: Record<string, unknown>) => string, p: { lanes?: number; parallelMacs?: number; memoryInterface: string; operandBuffers: unknown; clockMHz: number }): string {
  return [t('s3.pt.lanes', { n: p.lanes ?? p.parallelMacs }), t(p.memoryInterface === 'sqi' ? 's3.pt.sqi' : 's3.pt.spi'), t(`s3.pt.buf.${buffersOf(p.operandBuffers)}` as Key), `${p.clockMHz} MHz`].join(' · ')
}

export function S3Detail({ project, onBack, onProject }: { project: string; onBack(): void; onProject(project: string): void }) {
  const { t, message } = useI18n()
  const { client } = useKernel()
  const choices = useModelChoices()
  const { data: runsData } = useQuery((c) => c.call('runs.list', { project, limit: 1000 }), [project])
  const { data: state } = useQuery((c) => c.call('projects.get', { project }), [project])
  const { data: verdicts } = useQuery((c) => c.call('verdicts.list', { limit: 1000 }), [])
  const runs = runsData?.runs ?? []
  // The design shown: the latest; whether it is the one bound to the project is said next to it.
  const design = latest(runs, 'arch-taalas', 'design')
  const bound = !!design && out(design, 'chip') === state?.slots['chip-spec']
  const explored = latest(runs, 'arch-taalas', 'explore')
  // Measurements of exactly this design, by lineage: chip spec -> RTL -> verification; chip spec
  // -> layout -> timing and gate-level simulation.
  const chipHash = out(design, 'chip')
  const okRun = (plugin: string, handler: string, pred: (r: RunRecord) => boolean) =>
    runs.filter((r) => r.plugin === plugin && r.handler === handler && r.execution === 'exited_zero' && pred(r)).at(-1)
  const generated = okRun('rtl-taalas', 'generate', (r) => inp(r, 'chip') === chipHash)
  const verify = okRun('sim-verilator', 'verify', (r) => !!generated && inp(r, 'rtl') === out(generated, 'rtl'))
  const implement = okRun('eda-gf180', 'implement', (r) => !!chipHash && inp(r, 'chip') === chipHash)
  const sta = okRun('eda-gf180', 'sta', (r) => !!implement && inp(r, 'layout') === out(implement, 'layout'))
  const gatesim = okRun('sim-verilator', 'gatesim', (r) => !!implement && inp(r, 'layout') === out(implement, 'layout'))
  const archCheck = okRun('arch-taalas', 'verify_spec', (r) => inp(r, 'chip') === chipHash)
  const { data: chip } = useQuery((c) => json(c, out(design, 'chip')), [out(design, 'chip')], false)
  const { data: ppa } = useQuery((c) => json(c, out(design, 'ppa')), [out(design, 'ppa')], false)
  const { data: exploration } = useQuery((c) => json(c, out(explored, 'exploration')), [out(explored, 'exploration')], false)
  const { data: verifyReport } = useQuery((c) => json(c, out(verify, 'report')), [out(verify, 'report')], false)
  const hasChipSpec = runs.some((r) => r.plugin === 'numeric' && r.handler === 'quantize' && r.execution === 'exited_zero')
  const hasGraphSpec = runs.some((r) => r.plugin === 'numeric' && r.handler === 'quantize_graph' && r.execution === 'exited_zero')
  const analysis = !hasChipSpec && hasGraphSpec
  // The spatial engine (taalas-spatial/1) has its own views: circuits instead of a program.
  const spatial = chip?.architecture === SPATIAL
  const spatialSpace = exploration?.architecture === SPATIAL
  const archVerdict = verdicts?.verdicts.filter((v) => v.check === 'arch.spec' && v.run === archCheck?.id).at(-1)

  return (
    <>
      <button className="link step-back" onClick={onBack}>
        ← {t('s0.back')}
      </button>
      <Section title={t('s3.title')}>
        <p className="flow-what">{t('s3.intro')}</p>
        <h4>{t('s2.pick')}</h4>
        <div className="s1-picker">
          {!choices.some((c) => c.project === project) && <button className="active">{t('s1.pickOwn')}</button>}
          {choices.map((c) => (
            <button key={c.project} className={c.project === project ? 'active' : ''} onClick={() => onProject(c.project)} title={c.project}>
              {c.label}
            </button>
          ))}
        </div>
        <p className="muted">{t(hasChipSpec ? 's3.pathChip' : hasGraphSpec ? 's3.pathGraph' : 's3.pathNone')}</p>
        {chip && ppa && !analysis && !spatial && <Answer chip={chip} ppa={ppa} verify={verify} bound={bound} />}
        {chip && ppa && !analysis && spatial && <SpatialAnswer chip={chip} ppa={ppa} verify={verify} bound={bound} />}
        {chip && ppa && !analysis && !spatial && !ppa.microarchitecture && <p className="flow-note">{t('s3.oldDesign')}</p>}
        {analysis && exploration && !spatialSpace && <AnalysisAnswer exploration={exploration} />}
      </Section>

      {(hasChipSpec || hasGraphSpec) && <Bench key={out(design, 'chip') ?? 'none'} project={project} chipPath={hasChipSpec} current={chip} />}
      {exploration && !spatialSpace && <DesignSpace exploration={exploration} chosen={chip ? `L${chip.dataflow.lanes}-${chip.dataflow.memoryInterface ?? 'spi'}-${buffersOf(chip.dataflow.operandBuffers)}-${chip.targets.clockMHz}MHz` : undefined} />}
      {exploration && spatialSpace && <SpatialSpace exploration={exploration} chosen={spatial ? `D${chip.dataflow.dividers ?? 16}-${chip.targets.clockMHz}MHz` : undefined} />}

      {chip && ppa && !analysis && spatial && (
        <>
          <SpatialCompute chip={chip} ppa={ppa} />
          <SpatialArea ppa={ppa} implement={implement} />
          <SpatialSpeed ppa={ppa} verify={verify} gatesim={gatesim} sta={sta} chip={chip} />
          <ul className="flow-checks">
            <li>
              <span className={`pill ${archVerdict?.verdict === 'pass' ? 'ok' : archVerdict ? 'bad' : ''}`}>{t(`flow.verdict.${archVerdict?.verdict ?? 'none'}` as Key)}</span>
              {t('flow.check.arch')}
              {archVerdict && archVerdict.verdict !== 'pass' && <small className="muted"> · {archVerdict.reasons.map(message).join('; ')}</small>}
            </li>
          </ul>
        </>
      )}
      {chip && ppa && !analysis && !spatial && (
        <>
          {ppa.microarchitecture && <Compute chip={chip} ppa={ppa} />}
          {ppa.microarchitecture && <Store chip={chip} ppa={ppa} />}
          <Area ppa={ppa} implement={implement} />
          <Speed ppa={ppa} verifyReport={verifyReport} verify={verify} gatesim={gatesim} sta={sta} chip={chip} />
          <ul className="flow-checks">
            <li>
              <span className={`pill ${archVerdict?.verdict === 'pass' ? 'ok' : archVerdict ? 'bad' : ''}`}>{t(`flow.verdict.${archVerdict?.verdict ?? 'none'}` as Key)}</span>
              {t('flow.check.arch')}
              {archVerdict && archVerdict.verdict !== 'pass' && <small className="muted"> · {archVerdict.reasons.map(message).join('; ')}</small>}
            </li>
          </ul>
        </>
      )}
      {analysis && exploration && <AnalysisDetail exploration={exploration} />}
    </>
  )
}

function Answer({ chip, ppa, verify, bound }: { chip: Json; ppa: Json; verify: RunRecord | undefined; bound: boolean }) {
  const { t } = useI18n()
  const f = useFormat()
  const p = ppa.performance
  const die = ppa.area?.dieMm2
  const limit = chip.targets.areaLimitMm2
  const measured = verify?.metrics.busy_cycles_per_token as number | undefined
  return (
    <div className={`s0-answer ${die && limit && die.typ > limit ? 'infeasible' : ''}`}>
      <span>
        <span className="pill">{t(bound ? 's3.bound' : 's3.notBound')}</span>{' '}
        {t('s3.answer', { tps: f.tps(p.tokensPerSecond), clock: chip.targets.clockMHz, cycles: f.int(p.cyclesPerToken), die: f.mm2(die?.typ), limit: f.mm2(limit) })}
      </span>
      <small>
        {pointText(t, { lanes: chip.dataflow.lanes, memoryInterface: chip.dataflow.memoryInterface ?? 'spi', operandBuffers: chip.dataflow.operandBuffers, clockMHz: chip.targets.clockMHz })}
        {' · '}
        {t(BOTTLENECKS[p.bottleneck] ?? 's3.bottleneck.external_memory', { share: f.pct(p.memoryShare ?? 0) })}
        {' · '}
        {measured ? t('s3.answerMeasured', { cycles: f.int(measured) }) : t('s3.answerNotMeasured')}
      </small>
    </div>
  )
}

function AnalysisAnswer({ exploration }: { exploration: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const c = exploration.chosen
  const weights = c.items.find((i: Json) => i.item === 'weight storage')?.areaMm2
  return (
    <div className="s0-answer infeasible">
      <span>{t('s3.analysisAnswer', { die: f.mm2(c.dieMm2?.typ), limit: f.mm2(c.areaLimitMm2), weights: f.mm2(weights), tps: f.tps(c.tokensPerSecond), clock: c.clockMHz })}</span>
      <small>{t('s3.analysisNote')}</small>
    </div>
  )
}

function Progress({ progress }: { progress: Record<string, { state: StepState; detail?: string }> }) {
  const { t } = useI18n()
  if (Object.keys(progress).length === 0) return null
  return (
    <ul className="s1-progress">
      {RUN_STEPS.filter(([id]) => progress[id]).map(([id, label]) => (
        <li key={id} className={progress[id]!.state}>
          {progress[id]!.state === 'running' ? '…' : progress[id]!.state === 'done' ? '✓' : '✗'} {t(label)}
          {progress[id]!.detail && <small className="muted"> {['pass', 'fail', 'indeterminate'].includes(progress[id]!.detail!) ? t(`flow.verdict.${progress[id]!.detail}` as Key) : progress[id]!.detail}</small>}
        </li>
      ))}
    </ul>
  )
}

/** Choose a point: explore around it, or design it (and make it the project's design). */
function Bench({ project, chipPath, current }: { project: string; chipPath: boolean; current: Json }) {
  const { t } = useI18n()
  const { client } = useKernel()
  const [template, setTemplate] = useState<'hardwired' | 'spatial'>(current?.architecture === SPATIAL ? 'spatial' : 'hardwired')
  const [spatialPoint, setSpatialPoint] = useState<SpatialPoint>({
    architecture: SPATIAL,
    dividers: current?.dataflow?.dividers ?? 16,
    clockMHz: current?.targets?.clockMHz ?? 8,
    die: current?.architecture === SPATIAL && current.targets?.die ? { xMm: current.targets.die.xMm, yMm: current.targets.die.yMm } : SPATIAL_BASE.die,
  })
  const [point, setPoint] = useState<ArchPoint>({
    parallelMacs: current?.dataflow?.lanes ?? 2,
    clockMHz: current?.targets?.clockMHz ?? 8,
    memoryInterface: current?.dataflow?.memoryInterface ?? 'sqi',
    operandBuffers: current?.dataflow ? buffersOf(current.dataflow.operandBuffers) : 'none',
  })
  const [progress, setProgress] = useState<Record<string, { state: StepState; detail?: string }>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const report = (step: string, state: StepState, detail?: string) => setProgress((p) => ({ ...p, [step]: { state, ...(detail ? { detail } : {}) } }))
  const go = async (work: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    setProgress({})
    try {
      await work()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Section title={t('s3.bench')}>
      <p className="flow-what">{t(chipPath ? 's3.benchIntro' : 's3.benchIntroAnalysis')}</p>
      <div className="s1-picker">
        <button className={template === 'spatial' ? 'active' : ''} onClick={() => setTemplate('spatial')}>
          {t('s3sp.template.spatial')}
        </button>
        <button className={template === 'hardwired' ? 'active' : ''} onClick={() => setTemplate('hardwired')}>
          {t('s3sp.template.hardwired')}
        </button>
      </div>
      <p className="muted">{t(template === 'spatial' ? 's3sp.template.spatialNote' : 's3sp.template.hardwiredNote')}</p>
      {template === 'spatial' && (
        <div className="form grid">
          <label>
            {t('s3sp.in.dividers')}
            <select value={spatialPoint.dividers} onChange={(e) => setSpatialPoint({ ...spatialPoint, dividers: Number(e.target.value) })}>
              {[4, 8, 16, 32, 64].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t('s3.in.clock')}
            <input type="number" min={1} max={40} step={0.5} value={spatialPoint.clockMHz} onChange={(e) => setSpatialPoint({ ...spatialPoint, clockMHz: Number(e.target.value) })} />
          </label>
          <label>
            {t('s3sp.in.dieX')}
            <input type="number" min={1} max={20} step={0.1} value={spatialPoint.die.xMm} onChange={(e) => setSpatialPoint({ ...spatialPoint, die: { ...spatialPoint.die, xMm: Number(e.target.value) } })} />
          </label>
          <label>
            {t('s3sp.in.dieY')}
            <select value={spatialPoint.die.yMm} onChange={(e) => setSpatialPoint({ ...spatialPoint, die: { ...spatialPoint.die, yMm: Number(e.target.value) } })}>
              {[2, 3, 5].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
          <button disabled={busy} onClick={() => go(() => exploreArch(client, project, spatialPoint, report))}>
            {t('s3.explore')}
          </button>
          {chipPath && (
            <>
              <button disabled={busy} onClick={() => go(() => designArch(client, project, spatialPoint, false, report))}>
                {t('s3.design')}
              </button>
              <button className="active" disabled={busy} onClick={() => go(() => designArch(client, project, spatialPoint, true, report))}>
                {t('s3.adopt')}
              </button>
            </>
          )}
        </div>
      )}
      {template === 'hardwired' && (
      <div className="form grid">
        <label>
          {t('s3.in.lanes')}
          <select value={point.parallelMacs} onChange={(e) => setPoint({ ...point, parallelMacs: Number(e.target.value) })}>
            {[1, 2, 4, 8, 16].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('s3.in.memory')}
          <select value={point.memoryInterface} onChange={(e) => setPoint({ ...point, memoryInterface: e.target.value as 'spi' | 'sqi' })}>
            <option value="sqi">{t('s3.pt.sqi')}</option>
            <option value="spi">{t('s3.pt.spi')}</option>
          </select>
        </label>
        <label>
          {t('s3.in.buffers')}
          <select value={point.operandBuffers} onChange={(e) => setPoint({ ...point, operandBuffers: e.target.value as Buffers })}>
            {(['attention', 'all', 'none'] as const).map((b) => (
              <option key={b} value={b}>
                {t(`s3.pt.buf.${b}` as Key)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t('s3.in.clock')}
          <input type="number" min={1} max={40} step={0.5} value={point.clockMHz} onChange={(e) => setPoint({ ...point, clockMHz: Number(e.target.value) })} />
        </label>
        <button disabled={busy} onClick={() => go(() => exploreArch(client, project, point, report))}>
          {t('s3.explore')}
        </button>
        {chipPath && (
          <>
            <button disabled={busy} onClick={() => go(() => designArch(client, project, point, false, report))}>
              {t('s3.design')}
            </button>
            <button className="active" disabled={busy} onClick={() => go(() => designArch(client, project, point, true, report))}>
              {t('s3.adopt')}
            </button>
          </>
        )}
      </div>
      )}
      <p className="muted">{t(chipPath ? 's3.benchNote' : 's3.benchNoteAnalysis')}</p>
      <Progress progress={progress} />
      <ErrorLine error={error} />
    </Section>
  )
}

/** Every point of the exploration: a scatter of area against speed and the table. */
function DesignSpace({ exploration, chosen }: { exploration: Json; chosen: string | undefined }) {
  const { t } = useI18n()
  const f = useFormat()
  const pts = exploration.points as Json[]
  const W = 640
  const H = 260
  const areas = pts.map((p) => p.dieMm2?.typ ?? 0)
  const speeds = pts.map((p) => p.tokensPerSecond)
  const [a0, a1] = [Math.min(...areas), Math.max(...areas)]
  const [s0, s1] = [0, Math.max(...speeds) * 1.1]
  const x = (a: number) => 50 + ((a - a0) / Math.max(a1 - a0, 1e-9)) * (W - 70)
  const y = (s: number) => H - 30 - ((s - s0) / Math.max(s1 - s0, 1e-9)) * (H - 50)
  const limit = pts[0]?.areaLimitMm2
  return (
    <Section title={t('s3.space')}>
      <p className="flow-what">{t('s3.spaceIntro', { n: pts.length })}</p>
      <svg className="s3-scatter" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={t('s3.space')}>
        <line x1={50} y1={H - 30} x2={W - 10} y2={H - 30} stroke="#c7ccd6" />
        <line x1={50} y1={10} x2={50} y2={H - 30} stroke="#c7ccd6" />
        <text x={W / 2} y={H - 6} textAnchor="middle" className="s3-axis">
          {t('s3.axisArea', { lo: f.mm2(a0), hi: f.mm2(a1) })}
        </text>
        <text x={14} y={H / 2} textAnchor="middle" className="s3-axis" transform={`rotate(-90 14 ${H / 2})`}>
          tok/s
        </text>
        {limit !== undefined && limit >= a0 && limit <= a1 && <line x1={x(limit)} y1={10} x2={x(limit)} y2={H - 30} stroke="#d14343" strokeDasharray="4 3" />}
        {pts.map((p) => (
          <g key={p.id}>
            <circle cx={x(p.dieMm2?.typ ?? 0)} cy={y(p.tokensPerSecond)} r={p.id === chosen ? 7 : 5} className={`s3-dot ${p.fits ? (p.pareto ? 'pareto' : 'fits') : 'nofit'}${p.id === chosen ? ' chosen' : ''}`}>
              <title>{`${pointText(t, p)}: ${f.tps(p.tokensPerSecond)}, ${f.mm2(p.dieMm2?.typ)}`}</title>
            </circle>
          </g>
        ))}
      </svg>
      <p className="muted">{t('s3.legend')}</p>
      <div className="s2-scroll">
        <table className="s0-table s3-points">
          <thead>
            <tr>
              <th>{t('s3.col.point')}</th>
              <th>{t('s3.col.cycles')}</th>
              <th>{t('s3.col.speed')}</th>
              <th>{t('s3.col.die')}</th>
              <th>{t('s3.col.buffers')}</th>
              <th>{t('s3.col.bottleneck')}</th>
              <th>{t('s3.col.fits')}</th>
            </tr>
          </thead>
          <tbody>
            {[...pts]
              .sort((a, b) => b.tokensPerSecond - a.tokensPerSecond)
              .map((p) => (
                <tr key={p.id} className={p.id === chosen ? 'selected' : ''}>
                  <td>
                    {pointText(t, p)} {p.pareto && <span className="pill ok">{t('s3.pareto')}</span>} {p.id === chosen && <span className="pill">{t('s3.chosen')}</span>}
                  </td>
                  <td>{f.int(p.cyclesPerToken)}</td>
                  <td>{f.tps(p.tokensPerSecond)}</td>
                  <td>{f.mm2(p.dieMm2?.typ)}</td>
                  <td>{p.onchipBufferBits ? `${f.int(p.onchipBufferBits)} bit` : '—'}</td>
                  <td>{t(BOTTLENECKS[p.bottleneck] ?? 's3.bottleneck.external_memory', { share: '' })}</td>
                  <td>{p.fits ? <span className="pill ok">{t('common.yes')}</span> : <span className="pill bad">{t('common.no')}</span>}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </Section>
  )
}

/** How it computes: the microarchitecture and where the cycles of one token go. */
function Compute({ chip, ppa }: { chip: Json; ppa: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const p = ppa.performance
  const m = ppa.microarchitecture ?? {}
  const total = KINDS.reduce((a, k) => a + (p.breakdown?.[k] ?? 0), 0)
  return (
    <Section title={t('s3.compute')}>
      <p className="flow-what">{t('s3.computeIntro')}</p>
      <dl className="facts">
        <dt>{t('s3.f.lanes')}</dt>
        <dd>{t('s3.f.lanesText', { n: chip.dataflow.lanes, bits: chip.storage.weights.wordWidth })}</dd>
        <dt>{t('s3.f.multiplier')}</dt>
        <dd>{t('s3.f.multiplierText', { dw: m.datapathBits ?? '—' })}</dd>
        <dt>{t('s3.f.buffers')}</dt>
        <dd>{buffersOf(m.operandBuffers) !== 'none' ? t(`s3.f.buffers.${buffersOf(m.operandBuffers)}` as Key, { words: m.vectorBufferWords, acc: m.attentionAccumulators, bits: f.int(m.onchipBufferBits) }) : t('s3.f.buffersOff')}</dd>
        <dt>{t('s3.f.memory')}</dt>
        <dd>{t(m.memoryInterface === 'sqi' ? 's3.f.memorySqi' : 's3.f.memorySpi')}</dd>
        <dt>{t('s3.f.program')}</dt>
        <dd>{t('s3.f.programText', { n: m.programInstructions ?? '—' })}</dd>
      </dl>
      <h4>{t('s3.byOp', { pos: p.breakdownPosition, total: f.int(total) })}</h4>
      <div className="s3-stack">
        {KINDS.map((k) => (
          <span key={k} style={{ width: `${(100 * (p.breakdown?.[k] ?? 0)) / Math.max(total, 1)}%`, background: KIND_COLOR[k] }} title={`${t(`s3.kind.${k}` as Key)} ${f.pct((p.breakdown?.[k] ?? 0) / Math.max(total, 1))}`} />
        ))}
      </div>
      <p className="s3-legend">
        {KINDS.map((k) => (
          <span key={k}>
            <i style={{ background: KIND_COLOR[k] }} /> {t(`s3.kind.${k}` as Key)} {f.pct((p.breakdown?.[k] ?? 0) / Math.max(total, 1))}
          </span>
        ))}
      </p>
      <table className="s0-table">
        <thead>
          <tr>
            <th>{t('s3.col.op')}</th>
            <th>{t('s3.col.count')}</th>
            <th>{t('s3.col.accesses')}</th>
            <th>{t('s3.col.cyclesOp')}</th>
            <th>{t('s3.col.share')}</th>
            <th>{t('s3.col.split')}</th>
          </tr>
        </thead>
        <tbody>
          {(p.byOp ?? []).map((o: Json) => {
            const sum = KINDS.reduce((a, k) => a + o[k], 0)
            return (
              <tr key={o.op}>
                <td>{t(OPS[o.op] ?? 's3.op.END')}</td>
                <td>{o.count}</td>
                <td>{f.int(o.accesses)}</td>
                <td>{f.int(sum)}</td>
                <td>{f.pct(sum / Math.max(total, 1))}</td>
                <td>
                  <div className="s3-stack small">
                    {KINDS.map((k) => (
                      <span key={k} style={{ width: `${(100 * o[k]) / Math.max(sum, 1)}%`, background: KIND_COLOR[k] }} />
                    ))}
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </Section>
  )
}

/** How it stores: what lives on chip and what in the external SRAM. */
function Store({ chip, ppa }: { chip: Json; ppa: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const s = chip.storage
  const m = ppa.microarchitecture ?? {}
  const area = (name: string) => ppa.items?.find((i: Json) => i.item === name)?.areaMm2?.typ
  const rows: [Key, string, string, string][] = [
    ['s3.st.weights', t('s3.st.rom'), `${f.int(s.weights.bits)} bit`, f.mm2(area('weight storage'))],
    ['s3.st.scales', t('s3.st.rom'), `${f.int(s.scales.bits + s.norms.bits + s.tables.bits)} bit`, '—'],
    ['s3.st.buffers', t('s3.st.flops'), m.onchipBufferBits ? `${f.int(m.onchipBufferBits)} bit` : '—', f.mm2(area('activation buffers'))],
    ['s3.st.kv', t(chip.dataflow.memoryInterface === 'sqi' ? 's3.st.sqi' : 's3.st.spi'), f.bytes(s.kvCache.bytes), t('s3.st.offchip')],
    ['s3.st.work', t(chip.dataflow.memoryInterface === 'sqi' ? 's3.st.sqi' : 's3.st.spi'), f.bytes(4 * (m.externalMemoryWords ?? 0) - s.kvCache.bytes), t('s3.st.offchip')],
  ]
  return (
    <Section title={t('s3.store')}>
      <p className="flow-what">{t('s3.storeIntro')}</p>
      <table className="s0-table">
        <thead>
          <tr>
            <th>{t('s3.col.what')}</th>
            <th>{t('s3.col.where')}</th>
            <th>{t('s3.col.size')}</th>
            <th>{t('s3.col.area')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([k, where, size, a]) => (
            <tr key={k}>
              <td>{t(k)}</td>
              <td>{where}</td>
              <td>{size}</td>
              <td>{a}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  )
}

function Area({ ppa, implement }: { ppa: Json; implement: RunRecord | undefined }) {
  const { t } = useI18n()
  const f = useFormat()
  const items = (ppa.items ?? []).filter((i: Json) => i.areaMm2?.typ)
  const die = ppa.area?.dieMm2?.typ ?? 0
  const limit = ppa.reverse?.areaLimitMm2
  return (
    <Section title={t('s3.area')}>
      <p className="flow-what">{t('s3.areaIntro', { die: f.mm2(die), core: f.mm2(ppa.area?.coreMm2?.typ), limit: f.mm2(limit) })}</p>
      <table className="s0-table">
        <tbody>
          {items.map((i: Json) => (
            <tr key={i.item}>
              <td>{t(ITEMS[i.item] ?? 's3.item.other')}</td>
              <td>{f.mm2(i.areaMm2.typ)}</td>
              <td className="s3-barcell">
                <span style={{ width: `${(100 * i.areaMm2.typ) / Math.max(die, 1e-9)}%` }} />
              </td>
              <td className="muted">{i.confidence && i.confidence !== 'n/a' ? t(`s3.conf.${i.confidence}` as Key) : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {implement ? (
        <p>{t('s3.areaMeasured', { core: f.mm2((implement.metrics.core_area_um2 as number) / 1e6), die: f.mm2(implement.metrics.die_area_mm2 as number) })}</p>
      ) : (
        <p className="muted">{t('s3.areaNotMeasured')}</p>
      )}
    </Section>
  )
}

/** The exact model against the RTL simulation, the gate-level simulation and static timing. */
function Speed({ ppa, verifyReport, verify, gatesim, sta, chip }: { ppa: Json; verifyReport: Json; verify: RunRecord | undefined; gatesim: RunRecord | undefined; sta: RunRecord | undefined; chip: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const p = ppa.performance
  const traced: number[] = p.cyclesWithTracesPerPosition ?? []
  const item = (verifyReport?.items ?? []).find((i: Json) => i.id === 'regression.evalset')
  const measured = (item?.details?.busyCyclesByPosition ?? {}) as Record<string, number>
  const compared = Object.entries(measured).filter(([pos]) => traced[Number(pos)] !== undefined)
  const equal = compared.filter(([pos, c]) => traced[Number(pos)] === c).length
  const sameDesign = verify && chip && verifyReport
  const period = 1000 / chip.targets.clockMHz
  const slack = sta?.metrics.worst_setup_slack_ns as number | undefined
  const fmax = slack !== undefined ? 1000 / (period - slack) : undefined
  return (
    <Section title={t('s3.speed')}>
      <p className="flow-what">{t('s3.speedIntro')}</p>
      <div className="flow-facts">
        <div className="flow-fact">
          <small>{t('s3.fact.model')}</small>
          <strong>{f.int(p.cyclesPerToken)}</strong>
        </div>
        <div className="flow-fact">
          <small>{t('s3.fact.range')}</small>
          <strong>
            {f.int(p.cyclesFirst)} → {f.int(p.cyclesLast)}
          </strong>
        </div>
        <div className="flow-fact">
          <small>{t('s3.fact.speed', { clock: chip.targets.clockMHz })}</small>
          <strong>
            {f.tps(p.tokensPerSecond)} ({f.tps(p.tokensPerSecondLast)} {t('s3.fact.atEnd')})
          </strong>
        </div>
        <div className={`flow-fact ${compared.length ? (equal === compared.length ? 'ok' : 'bad') : ''}`}>
          <small>{t('s3.fact.rtl')}</small>
          <strong>{compared.length ? t('s3.fact.rtlEqual', { equal, n: compared.length }) : t('s3.fact.none')}</strong>
        </div>
        <div className="flow-fact">
          <small>{t('s3.fact.gate')}</small>
          <strong>{gatesim ? f.int(gatesim.metrics.gl_cycles_per_token as number) : t('s3.fact.none')}</strong>
        </div>
        <div className={`flow-fact ${slack !== undefined ? (slack >= 0 ? 'ok' : 'bad') : ''}`}>
          <small>{t('s3.fact.fmax')}</small>
          <strong>{fmax !== undefined ? `${f.num(fmax, 1)} MHz` : t('s3.fact.none')}</strong>
        </div>
      </div>
      <p className="muted">{t('s3.speedNote', { sck: f.num(chip.targets.clockMHz / 2, 1), sckMax: p.clockLimit?.sramSckMaxMHz ?? 20, maxClock: p.clockLimit?.maxClockBySramMHz ?? 40 })}</p>
      {sameDesign && compared.length > 0 && (
        <details>
          <summary>{t('s3.perPosition', { n: compared.length })}</summary>
          <table className="s0-table">
            <thead>
              <tr>
                <th>{t('s3.col.pos')}</th>
                <th>{t('s3.col.model')}</th>
                <th>{t('s3.col.rtl')}</th>
              </tr>
            </thead>
            <tbody>
              {compared.slice(0, 64).map(([pos, c]) => (
                <tr key={pos}>
                  <td>{pos}</td>
                  <td>{f.int(traced[Number(pos)]!)}</td>
                  <td className={traced[Number(pos)] === c ? '' : 'error'}>{f.int(c)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </Section>
  )
}

/** An imported model: what the engine would need, and what blocks it. */
function AnalysisDetail({ exploration }: { exploration: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const c = exploration.chosen
  const total = KINDS.reduce((a, k) => a + (c.breakdown?.[k] ?? 0), 0)
  return (
    <Section title={t('s3.analysis')}>
      <p className="flow-what">{t('s3.analysisIntro')}</p>
      <table className="s0-table">
        <tbody>
          {c.items
            .filter((i: Json) => i.areaMm2)
            .map((i: Json) => (
              <tr key={i.item}>
                <td>{t(ITEMS[i.item] ?? 's3.item.other')}</td>
                <td>{f.mm2(i.areaMm2)}</td>
              </tr>
            ))}
        </tbody>
      </table>
      <h4>{t('s3.findings')}</h4>
      <ul>
        {!c.fits && <li>{t('s3.findingArea', { die: f.mm2(c.dieMm2?.typ), limit: f.mm2(c.areaLimitMm2) })}</li>}
        {c.limits.map((l: string) => (
          <li key={l}>{l.includes('24-bit') ? t('s3.findingMemory', { bytes: f.bytes(c.externalMemoryBytes) }) : l}</li>
        ))}
        <li>{t('s3.findingSpeed', { cycles: f.int(c.cyclesPerToken), tps: f.tps(c.tokensPerSecond), clock: c.clockMHz, mem: f.pct((c.breakdown?.memory ?? 0) / Math.max(total, 1)) })}</li>
      </ul>
    </Section>
  )
}
