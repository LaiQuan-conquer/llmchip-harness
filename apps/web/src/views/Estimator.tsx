import { useState } from 'react'
import { useKernel, useQuery } from '../api.ts'
import { useI18n, type Key } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { ErrorLine, Section } from './common.tsx'

type Range = { low: number; typ: number; high: number }
const fmt = (r: Range | null | undefined, digits = 3) => (r ? `${r.low.toFixed(digits)} / ${r.typ.toFixed(digits)} / ${r.high.toFixed(digits)}` : '—')

const STORAGE: Record<string, string[]> = {
  'sephonchitech-180bcd': ['sephonchitech-180bcd.foundry-rom', 'sephonchitech-180bcd.stdcell-rom', 'sephonchitech-180bcd.foundry-sram'],
  gf180mcu: ['gf180mcu.stdcell-rom', 'gf180mcu.sram-512x8', 'gf180mcu.openram-rom'],
}

/** Estimator (FR-UI-08, FR-EST): form, result ranges, calculation details and the four conclusions. */
export function EstimatorView(_: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t } = useI18n()
  const { client } = useKernel()
  const { data: plugins } = useQuery((c) => c.call('plugins.list'), [], false)
  const [form, setForm] = useState({ model: 'stories260K', bits: 4, process: 'sephonchitech-180bcd', storage: 'sephonchitech-180bcd.foundry-rom', context: 64, kv: 'offchip' })
  const [report, setReport] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const data = plugins?.plugins.flatMap((p) => p.data) ?? []
  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      const profile = data.find((d) => d.type === 'process.profile' && d.id === form.process)
      const library = data.find((d) => d.type === 'implementation.library')
      if (!profile || !library) throw new Error('process profile or implementation library not registered')
      const started = await client.call('runs.start', {
        plugin: 'estimator',
        handler: 'estimate',
        params: { model: { preset: form.model }, weightBits: form.bits, weightStorage: form.storage, contextLength: form.context, kvLocation: form.kv },
        inputs: [
          { name: 'profile', hash: profile.hash },
          { name: 'library', hash: library.hash },
        ],
      })
      const finished = await client.call('runs.wait', { run: started.id, timeoutMs: 120_000 })
      if (finished.execution !== 'exited_zero') throw new Error(finished.error ?? finished.execution)
      const hash = finished.outputs.find((o) => o.name === 'report')!.hash
      setReport(await (await client.download(hash)).json())
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const set = (change: Partial<typeof form>) => setForm((f) => ({ ...f, ...change }))
  return (
    <>
      <Section title={t('nav.estimator')}>
        <div className="form grid">
          <label>
            {t('estimator.model')}
            <select value={form.model} onChange={(e) => set({ model: e.target.value })}>
              <option value="stories260K">stories260K</option>
              <option value="qwen2.5-0.5b">Qwen2.5 0.5B</option>
              <option value="llama3.1-8b">Llama 3.1 8B</option>
            </select>
          </label>
          <label>
            {t('estimator.bits')}
            <select value={form.bits} onChange={(e) => set({ bits: Number(e.target.value) })}>
              {[2, 3, 4, 8, 16].map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t('estimator.process')}
            <select value={form.process} onChange={(e) => set({ process: e.target.value, storage: STORAGE[e.target.value]![0]! })}>
              {Object.keys(STORAGE).map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t('estimator.storage')}
            <select value={form.storage} onChange={(e) => set({ storage: e.target.value })}>
              {STORAGE[form.process]!.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t('estimator.context')}
            <input type="number" min={1} value={form.context} onChange={(e) => set({ context: Number(e.target.value) })} />
          </label>
          <label>
            {t('estimator.kv')}
            <select value={form.kv} onChange={(e) => set({ kv: e.target.value })}>
              <option value="offchip">{t('estimator.kv.offchip')}</option>
              <option value="onchip">{t('estimator.kv.onchip')}</option>
            </select>
          </label>
          <button onClick={run} disabled={busy}>
            {busy ? t('estimator.running') : t('estimator.run')}
          </button>
        </div>
        <ErrorLine error={error} />
      </Section>
      {report && <EstimateReport report={report} />}
    </>
  )
}

function EstimateReport({ report }: { report: any }) {
  const { t } = useI18n()
  const c = report.conclusion
  return (
    <>
      <Section title={`${t('estimator.conclusion')}: ${t(`feasibility.${c.feasibility}` as Key)}`}>
        <p className={`verdict ${c.feasibility}`}>{report.model.title} · {report.process.title}</p>
        {c.reasons.length > 0 && (
          <>
            <h3>{t('estimator.reasons')}</h3>
            <ul>{c.reasons.map((r: string) => <li key={r}>{r}</li>)}</ul>
          </>
        )}
        {c.conditions.length > 0 && (
          <>
            <h3>{t('estimator.conditions')}</h3>
            <ul>{c.conditions.map((r: string) => <li key={r}>{r}</li>)}</ul>
          </>
        )}
        {report.missing.length > 0 && (
          <>
            <h3>{t('estimator.missing')}</h3>
            <ul>{report.missing.map((m: any) => <li key={m.parameter}>{m.parameter}: {m.howToObtain}</li>)}</ul>
          </>
        )}
        {report.suggestions?.length > 0 && (
          <>
            <h3>{t('estimator.suggestions')}</h3>
            <ul>{report.suggestions.map((s: string) => <li key={s}>{s}</li>)}</ul>
          </>
        )}
      </Section>
      <Section title={t('estimator.area')}>
        <table>
          <thead>
            <tr>
              <th>{t('estimator.item')}</th>
              <th>mm²</th>
              <th>{t('estimator.source')}</th>
              <th>{t('estimator.confidence')}</th>
            </tr>
          </thead>
          <tbody>
            {report.items.map((i: any) => (
              <tr key={i.item}>
                <td>{i.item}</td>
                <td className="mono">{fmt(i.areaMm2)}</td>
                <td>{i.source}</td>
                <td>
                  <span className={`label ${i.confidence}`}>{i.confidence}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {report.area && (
          <p>
            <strong>{t('estimator.die')}</strong>: {fmt(report.area.dieMm2, 2)} mm²
          </p>
        )}
        {report.cost?.cny && (
          <p>
            <strong>{t('estimator.cost')}</strong>: {fmt(report.cost.cny, 0)} ({report.cost.rule})
            <br />
            <small className="muted">
              {t('estimator.notCovered')}: {report.cost.notCovered.join('; ')}
            </small>
          </p>
        )}
        {report.reverse && (
          <p>
            <strong>{t('estimator.reverse')}</strong>:{' '}
            {Object.entries(report.reverse.maxParams)
              .map(([bits, r]: [string, any]) => `${bits}-bit ${r.typ.toLocaleString()}`)
              .join(' · ')}
          </p>
        )}
      </Section>
      <Section title={t('estimator.performance')}>
        <p>
          {report.performance.tokensPerSecond.toFixed(1)} {t('estimator.tokensPerSecond')} · {report.performance.cyclesPerToken.toLocaleString()} cycles/token · {t('estimator.bottleneck')}: {report.performance.bottleneck}
        </p>
      </Section>
    </>
  )
}
