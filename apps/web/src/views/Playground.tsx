import { useState } from 'react'
import { useKernel, useQuery } from '../api.ts'
import { useI18n, type Key } from '../i18n.ts'
import type { Page, Selection } from '../App.tsx'
import { ErrorLine, Section } from './common.tsx'

type Level = 'L0' | 'L2' | 'L3'
interface Generation {
  promptTokens?: number[]
  tokens: number[]
  text: string
  cyclesPerToken?: number[]
}

/** Each level: the plugin handler that runs it and the project slots it needs. */
const LEVELS: Record<Level, { plugin: string; handler: string; slots: Record<string, string> }> = {
  L0: { plugin: 'model-llama', handler: 'reference', slots: { ir: 'model-ir', tokenizer: 'tokenizer' } },
  L2: { plugin: 'numeric', handler: 'infer', slots: { spec: 'numeric-spec', weights: 'l2-weights', tokenizer: 'tokenizer' } },
  L3: { plugin: 'sim-verilator', handler: 'generate', slots: { rtl: 'rtl', tokenizer: 'tokenizer' } },
}

/**
 * Chip Playground (FR-PLY-01..03): one prompt through the chosen chain levels
 * of the selected project, side by side. L0 ≈ L2 shows where the tokens part;
 * L3 ≡ L2 marks every position that differs.
 */
export function PlaygroundView({ selection }: { selection: Selection; select: (s: Partial<Selection>, to?: Page) => void }) {
  const { t } = useI18n()
  const { client } = useKernel()
  const { data: state } = useQuery((c) => (selection.project ? c.call('projects.get', { project: selection.project }) : Promise.resolve(undefined)), [selection.project])
  const [prompt, setPrompt] = useState('Once upon a time')
  const [steps, setSteps] = useState(32)
  const [levels, setLevels] = useState<Record<Level, boolean>>({ L0: true, L2: true, L3: true })
  const [results, setResults] = useState<Partial<Record<Level, Generation | string>>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (!selection.project) return <Section title={t('nav.playground')}>{<p className="muted">{t('playground.selectProject')}</p>}</Section>
  const slots = state?.slots ?? {}
  const missing = (level: Level) => Object.values(LEVELS[level].slots).filter((slot) => !slots[slot])

  const runLevels = async () => {
    setBusy(true)
    setError(null)
    setResults({})
    try {
      const evalset = await client.upload(JSON.stringify({ format: 'llmchip-evalset/1', prompts: [prompt], steps }), { type: 'eval.set', mediaType: 'application/json' })
      const chosen = (Object.keys(LEVELS) as Level[]).filter((l) => levels[l] && missing(l).length === 0)
      await Promise.all(
        chosen.map(async (level) => {
          try {
            const { plugin, handler, slots: needs } = LEVELS[level]
            const inputs = [...Object.entries(needs).map(([name, slot]) => ({ name, hash: slots[slot]! })), { name: 'evalset', hash: evalset.hash }]
            const started = await client.call('runs.start', { plugin, handler, params: {}, inputs, project: selection.project! })
            let finished = await client.call('runs.wait', { run: started.id, timeoutMs: 600_000 })
            while (finished.state !== 'finished') finished = await client.call('runs.wait', { run: started.id, timeoutMs: 600_000 })
            if (finished.execution !== 'exited_zero') throw new Error(finished.error ?? finished.execution)
            const hash = finished.outputs.find((o) => o.name === 'outputs')!.hash
            const body = await (await client.download(hash)).json()
            setResults((r) => ({ ...r, [level]: body.generations[0] as Generation }))
          } catch (e) {
            setResults((r) => ({ ...r, [level]: (e as Error).message }))
          }
        }),
      )
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const l2 = typeof results.L2 === 'object' ? results.L2 : undefined
  return (
    <>
      <Section title={t('nav.playground')}>
        <div className="form grid">
          <label className="wide">
            {t('playground.prompt')}
            <textarea value={prompt} rows={2} onChange={(e) => setPrompt(e.target.value)} />
          </label>
          <label>
            {t('playground.steps')}
            <input type="number" min={1} max={4096} value={steps} onChange={(e) => setSteps(Number(e.target.value))} />
          </label>
          <div className="levels">
            {(Object.keys(LEVELS) as Level[]).map((level) => (
              <label key={level} className="check" title={missing(level).length ? `${t('playground.missing')}: ${missing(level).join(', ')}` : ''}>
                <input type="checkbox" checked={levels[level] && missing(level).length === 0} disabled={missing(level).length > 0} onChange={(e) => setLevels((l) => ({ ...l, [level]: e.target.checked }))} />
                {t(`playground.${level}` as Key)}
              </label>
            ))}
          </div>
          <button onClick={runLevels} disabled={busy || !prompt}>
            {busy ? t('playground.running') : t('playground.run')}
          </button>
        </div>
        <ErrorLine error={error} />
      </Section>
      <div className="playground">
        {(Object.keys(LEVELS) as Level[])
          .filter((level) => results[level] !== undefined)
          .map((level) => (
            <LevelOutput key={level} level={level} result={results[level]!} reference={level === 'L2' ? undefined : l2} />
          ))}
      </div>
    </>
  )
}

function LevelOutput({ level, result, reference }: { level: Level; result: Generation | string; reference: Generation | undefined }) {
  const { t } = useI18n()
  if (typeof result === 'string') {
    return (
      <Section title={t(`playground.${level}` as Key)}>
        <p className="error">{result}</p>
      </Section>
    )
  }
  const differs = (i: number) => reference !== undefined && reference.tokens[i] !== result.tokens[i]
  const count = reference ? Math.max(result.tokens.length, reference.tokens.length) : result.tokens.length
  const mismatches = reference ? Array.from({ length: count }, (_, i) => i).filter(differs) : []
  const exact = level === 'L3' // L3 ≡ L2; L0 ≈ L2
  const cycles = result.cyclesPerToken ?? []
  return (
    <Section title={t(`playground.${level}` as Key)}>
      <p className="generated">{result.text}</p>
      {reference && (
        <p className={exact ? (mismatches.length ? 'error' : 'ok-text') : 'muted'}>
          {exact
            ? mismatches.length
              ? t('playground.mismatch', { count: mismatches.length, first: mismatches[0]! })
              : t('playground.identical')
            : mismatches.length
              ? t('playground.parts', { first: mismatches[0]! })
              : t('playground.same')}
        </p>
      )}
      <div className="tokens mono">
        {result.tokens.map((token, i) => (
          <span key={i} className={differs(i) ? (exact ? 'token bad' : 'token warn') : 'token'} title={`#${i}`}>
            {token}
          </span>
        ))}
      </div>
      {cycles.length > 0 && <p className="muted">{t('playground.cycles', { cycles: Math.round(cycles.reduce((a, b) => a + b, 0) / cycles.length).toLocaleString() })}</p>}
    </Section>
  )
}
