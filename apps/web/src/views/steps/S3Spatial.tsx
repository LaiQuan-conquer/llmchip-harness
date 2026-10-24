import { useMemo } from 'react'
import type { RunRecord } from '@llmchip/kernel-api'
import { useI18n, type Key } from '../../i18n.ts'
import { Section } from '../common.tsx'

// S3 of the spatial engine (taalas-spatial/1): the model as circuits. Every matrix row is a
// bit-serial adder tree wired from its weights, every layer has its own circuits; attention
// is a shared unit, the KV cache a register file on chip. Speed is the fixed schedule of the
// generated state machine (equal at every position); area is the generated structure times
// cell areas measured by synthesis; the die is the smallest within the size rules.

type Json = any

const BLOCKS: Record<string, Key> = {
  'matrix circuits: serial adders': 's3sp.block.adders',
  'matrix circuits: delay lines (scale digits)': 's3sp.block.delays',
  'matrix circuits: row outputs (rounding, narrowing)': 's3sp.block.outputs',
  'KV cache (register file)': 's3sp.block.kv',
  'attention multipliers': 's3sp.block.multipliers',
  'tables (embedding, rsqrt, exp, sigmoid, RoPE, norms)': 's3sp.block.tables',
  'core registers': 's3sp.block.registers',
  'core logic (sequencing, elementwise units, selection)': 's3sp.block.logic',
  'softmax dividers': 's3sp.block.dividers',
  'host interface and chip top': 's3sp.block.host',
}
const MATRICES: Record<string, Key> = { wq: 's3sp.m.wq', wk: 's3sp.m.wk', wv: 's3sp.m.wv', wo: 's3sp.m.wo', w1: 's3sp.m.w1', w2: 's3sp.m.w2', w3: 's3sp.m.w3', cls: 's3sp.m.cls' }
/** Stage groups of the schedule, for the cycle bar. */
const GROUPS: [string, Key, string][] = [
  ['attention', 's3sp.g.attention', '#2f6fed'],
  ['circuits', 's3sp.g.circuits', '#e5883b'],
  ['elementwise', 's3sp.g.elementwise', '#7a5af8'],
  ['other', 's3sp.g.other', '#8d99ae'],
]

function groupOf(stage: string): string {
  if (stage.includes('attention')) return 'attention'
  if (/ (q, k, v|wo|w1, w3|w2)$/.test(stage) || stage === 'classifier') return 'circuits'
  if (/rmsnorm|rope|silu|residual/.test(stage)) return 'elementwise'
  return 'other'
}

function useFormat() {
  const { lang } = useI18n()
  return useMemo(() => {
    const locale = lang === 'zh' ? 'zh-CN' : 'en-US'
    const num = (v: number, d = 1) => new Intl.NumberFormat(locale, { maximumFractionDigits: d }).format(v)
    return {
      num,
      int: (v: number) => new Intl.NumberFormat(locale).format(Math.round(v)),
      pct: (v: number) => `${num(100 * v, 1)}%`,
      tps: (v: number) => `${num(v, 0)} tok/s`,
      mm2: (v: number | undefined) => (v === undefined ? '—' : v >= 100 ? `${num(v, 0)} mm²` : `${num(v, 2)} mm²`),
      bytes: (b: number) => (b >= 1e6 ? `${num(b / 1e6, 1)} MB` : `${num(b / 1e3, 1)} KB`),
    }
  }, [lang])
}

export function SpatialAnswer({ chip, ppa, verify, bound }: { chip: Json; ppa: Json; verify: RunRecord | undefined; bound: boolean }) {
  const { t } = useI18n()
  const f = useFormat()
  const p = ppa.performance
  const die = ppa.area?.dieMm2
  const limit = chip.targets.areaLimitMm2
  const chosen = ppa.area?.chosenDie
  const measured = verify?.metrics.busy_cycles_per_token as number | undefined
  return (
    <div className={`s0-answer ${die && limit && die.typ > limit ? 'infeasible' : ''}`}>
      <span>
        <span className="pill">{t(bound ? 's3.bound' : 's3.notBound')}</span>{' '}
        {t('s3sp.answer', { tps: f.tps(p.tokensPerSecond), clock: chip.targets.clockMHz, cycles: f.int(p.cyclesPerToken), cells: f.mm2(ppa.area?.cellsMm2?.typ), x: chosen?.xMm, y: chosen?.yMm })}
      </span>
      <small>
        {t('s3sp.answerHost', { tps: f.tps(p.tokensPerSecondWithHost ?? p.tokensPerSecond), frames: f.int(p.hostFrameCyclesPerToken ?? 0) })}
        {' · '}
        {measured ? t('s3.answerMeasured', { cycles: f.int(measured) }) : t('s3.answerNotMeasured')}
      </small>
    </div>
  )
}

/** How it computes: the circuits of every matrix, the attention unit, the KV cache. */
export function SpatialCompute({ chip, ppa }: { chip: Json; ppa: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const s = ppa.structure ?? {}
  const m = ppa.microarchitecture ?? {}
  return (
    <Section title={t('s3.compute')}>
      <p className="flow-what">{t('s3sp.computeIntro')}</p>
      <dl className="facts">
        <dt>{t('s3sp.f.matrices')}</dt>
        <dd>{t('s3sp.f.matricesText', { adders: f.int(s.serialAdders ?? 0), rows: f.int((s.outputRows16 ?? 0) + (s.outputRows32 ?? 0)), delays: f.int(s.delayBits ?? 0) })}</dd>
        <dt>{t('s3sp.f.attention')}</dt>
        <dd>{t('s3sp.f.attentionText', { mul: m.attentionMultipliers ?? '—', div: m.dividers ?? chip.dataflow.dividers ?? '—' })}</dd>
        <dt>{t('s3sp.f.kv')}</dt>
        <dd>{t('s3sp.f.kvText', { bytes: f.bytes((s.kvBits ?? 0) / 8), rings: 2 * (chip.dataflow.schedule ? (chip.dataflow.schedule as string[]).filter((x) => x.includes('scores and maximum')).length : 0), rows: chip.targets.contextTokens })}</dd>
        <dt>{t('s3sp.f.memory')}</dt>
        <dd>{t('s3sp.f.memoryText')}</dd>
      </dl>
      {ppa.circuits && (
        <table className="s0-table">
          <thead>
            <tr>
              <th>{t('s3sp.col.matrix')}</th>
              <th>{t('s3sp.col.rows')}</th>
              <th>{t('s3sp.col.adders')}</th>
              <th>{t('s3sp.col.delays')}</th>
              <th>{t('s3sp.col.stream')}</th>
            </tr>
          </thead>
          <tbody>
            {(ppa.circuits as Json[]).map((c) => {
              const [layer, name] = c.matrix.includes('.') ? c.matrix.split('.') : ['', c.matrix]
              return (
                <tr key={c.matrix}>
                  <td>
                    {layer !== '' ? t('s3sp.layer', { n: layer }) + ' · ' : ''}
                    {t(MATRICES[name] ?? 's3sp.m.cls')}
                  </td>
                  <td>{c.rows}</td>
                  <td>{f.int(c.adders)}</td>
                  <td>{f.int(c.delayBits)}</td>
                  <td>{c.streamBits}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Section>
  )
}

/** Area: the blocks of the measured-cell model against the die. */
export function SpatialArea({ ppa, implement }: { ppa: Json; implement: RunRecord | undefined }) {
  const { t } = useI18n()
  const f = useFormat()
  const a = ppa.area ?? {}
  const blocks = (a.blocks ?? []) as Json[]
  const cells = a.cellsMm2?.typ ?? 0
  return (
    <Section title={t('s3.area')}>
      <p className="flow-what">
        {t('s3sp.areaIntro', { cells: f.mm2(cells), core: f.mm2(a.coreMm2?.typ), die: f.mm2(a.dieMm2?.typ), x: a.chosenDie?.xMm, y: a.chosenDie?.yMm, limit: f.mm2(ppa.reverse?.areaLimitMm2), util: f.pct(a.utilization?.typ ?? 0) })}
      </p>
      <table className="s0-table">
        <tbody>
          {blocks.map((b) => (
            <tr key={b.item}>
              <td>{t(BLOCKS[b.item] ?? 's3.item.other')}</td>
              <td>{f.mm2(b.areaMm2.typ)}</td>
              <td className="s3-barcell">
                <span style={{ width: `${(100 * b.areaMm2.typ) / Math.max(cells, 1e-9)}%` }} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted">{t('s3sp.areaSource', { source: a.source ?? '—' })}</p>
      {implement ? (
        <p>{t('s3.areaMeasured', { core: f.mm2((implement.metrics.core_area_um2 as number) / 1e6), die: f.mm2(implement.metrics.die_area_mm2 as number) })}</p>
      ) : (
        <p className="muted">{t('s3.areaNotMeasured')}</p>
      )}
    </Section>
  )
}

/** Speed: the fixed schedule, against the RTL, the gate-level simulation and static timing. */
export function SpatialSpeed({ ppa, verify, gatesim, sta, chip }: { ppa: Json; verify: RunRecord | undefined; gatesim: RunRecord | undefined; sta: RunRecord | undefined; chip: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const p = ppa.performance
  const stages = (p.stages ?? []) as { stage: string; cycles: number }[]
  const total = stages.reduce((a, s) => a + s.cycles, 0)
  const sums = Object.fromEntries(GROUPS.map(([g]) => [g, stages.filter((s) => groupOf(s.stage) === g).reduce((a, s) => a + s.cycles, 0)]))
  const measured = verify?.metrics.busy_cycles_per_token as number | undefined
  const dependent = verify?.metrics.busy_cycles_data_dependent as boolean | undefined
  const period = 1000 / chip.targets.clockMHz
  const slack = sta?.metrics.worst_setup_slack_ns as number | undefined
  const fmax = slack !== undefined ? 1000 / (period - slack) : undefined
  const target = chip.targets.cyclesPerToken as number | undefined
  return (
    <Section title={t('s3.speed')}>
      <p className="flow-what">{t('s3sp.speedIntro')}</p>
      <div className="flow-facts">
        <div className="flow-fact">
          <small>{t('s3.fact.model')}</small>
          <strong>{f.int(p.cyclesPerToken)}</strong>
        </div>
        <div className={`flow-fact ${target ? (p.cyclesPerToken <= target ? 'ok' : 'bad') : ''}`}>
          <small>{t('s3sp.fact.target')}</small>
          <strong>{target ? f.int(target) : t('s3.fact.none')}</strong>
        </div>
        <div className="flow-fact">
          <small>{t('s3.fact.speed', { clock: chip.targets.clockMHz })}</small>
          <strong>{f.tps(p.tokensPerSecond)}</strong>
        </div>
        <div className={`flow-fact ${measured !== undefined ? (measured === p.cyclesPerToken && !dependent ? 'ok' : 'bad') : ''}`}>
          <small>{t('s3.fact.rtl')}</small>
          <strong>{measured !== undefined ? t(dependent ? 's3sp.fact.rtlVaries' : 's3sp.fact.rtlEvery', { cycles: f.int(measured) }) : t('s3.fact.none')}</strong>
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
      <h4>{t('s3sp.byStage', { total: f.int(total) })}</h4>
      <div className="s3-stack">
        {GROUPS.map(([g, , color]) => (
          <span key={g} style={{ width: `${(100 * sums[g]!) / Math.max(total, 1)}%`, background: color }} />
        ))}
      </div>
      <p className="s3-legend">
        {GROUPS.map(([g, k, color]) => (
          <span key={g}>
            <i style={{ background: color }} /> {t(k)} {f.pct(sums[g]! / Math.max(total, 1))}
          </span>
        ))}
      </p>
      <details>
        <summary>{t('s3sp.stages', { n: stages.length })}</summary>
        <table className="s0-table">
          <thead>
            <tr>
              <th>{t('s3sp.col.stage')}</th>
              <th>{t('s3.col.cycles')}</th>
            </tr>
          </thead>
          <tbody>
            {stages.map((s, i) => (
              <tr key={`${i}-${s.stage}`}>
                <td>{s.stage}</td>
                <td>{f.int(s.cycles)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
      <p className="muted">{t('s3sp.speedNote', { frames: f.int(p.hostFrameCyclesPerToken ?? 0), read: f.int(p.observationReadCycles ?? 0) })}</p>
    </Section>
  )
}

/** The exploration of the spatial template: dividers x clock, area against speed. */
export function SpatialSpace({ exploration, chosen }: { exploration: Json; chosen: string | undefined }) {
  const { t } = useI18n()
  const f = useFormat()
  const pts = exploration.points as Json[]
  const W = 640
  const H = 260
  const areas = pts.map((p) => p.dieMm2.typ)
  const speeds = pts.map((p) => p.tokensPerSecond)
  const [a0, a1] = [Math.min(...areas), Math.max(...areas)]
  const s1 = Math.max(...speeds, 1) * 1.1
  const x = (a: number) => 50 + ((a - a0) / Math.max(a1 - a0, 1e-9)) * (W - 70)
  const y = (s: number) => H - 30 - (s / s1) * (H - 50)
  const label = (p: Json) => t('s3sp.pt', { div: p.dividers, clock: p.clockMHz })
  return (
    <Section title={t('s3.space')}>
      <p className="flow-what">{t(exploration.analysis ? 's3sp.spaceAnalysis' : 's3sp.spaceIntro', { n: pts.length })}</p>
      {pts.length > 0 && (
        <svg className="s3-scatter" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={t('s3.space')}>
          <line x1={50} y1={H - 30} x2={W - 10} y2={H - 30} stroke="#c7ccd6" />
          <line x1={50} y1={10} x2={50} y2={H - 30} stroke="#c7ccd6" />
          <text x={W / 2} y={H - 6} textAnchor="middle" className="s3-axis">
            {t('s3.axisArea', { lo: f.mm2(a0), hi: f.mm2(a1) })}
          </text>
          <text x={14} y={H / 2} textAnchor="middle" className="s3-axis" transform={`rotate(-90 14 ${H / 2})`}>
            tok/s
          </text>
          {pts.map((p) => (
            <circle key={p.id} cx={x(p.dieMm2.typ)} cy={y(p.tokensPerSecond)} r={p.id === chosen ? 7 : 5} className={`s3-dot ${p.fits ? (p.pareto ? 'pareto' : 'fits') : 'nofit'}${p.id === chosen ? ' chosen' : ''}`}>
              <title>{`${label(p)}: ${f.tps(p.tokensPerSecond)}, ${f.mm2(p.dieMm2.typ)}`}</title>
            </circle>
          ))}
        </svg>
      )}
      <p className="muted">{t('s3.legend')}</p>
      <table className="s0-table s3-points">
        <thead>
          <tr>
            <th>{t('s3.col.point')}</th>
            <th>{t('s3.col.cycles')}</th>
            <th>{t('s3.col.speed')}</th>
            <th>{t('s3sp.col.cells')}</th>
            <th>{t('s3.col.die')}</th>
            <th>{t('s3.col.fits')}</th>
          </tr>
        </thead>
        <tbody>
          {[...pts]
            .sort((a, b) => b.tokensPerSecond - a.tokensPerSecond)
            .map((p) => (
              <tr key={p.id} className={p.id === chosen ? 'selected' : ''}>
                <td>
                  {label(p)} {p.pareto && <span className="pill ok">{t('s3.pareto')}</span>} {p.id === chosen && <span className="pill">{t('s3.chosen')}</span>}
                </td>
                <td>{f.int(p.cyclesPerToken)}</td>
                <td>{f.tps(p.tokensPerSecond)}</td>
                <td>{f.mm2(p.cellsMm2.typ)}</td>
                <td>{f.mm2(p.dieMm2.typ)}</td>
                <td>{p.fits ? <span className="pill ok">{t('common.yes')}</span> : <span className="pill bad">{t('common.no')}</span>}</td>
              </tr>
            ))}
        </tbody>
      </table>
      {(exploration.limits ?? []).length > 0 && (
        <ul>
          {(exploration.limits as string[]).map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
      )}
    </Section>
  )
}
