import { Fragment, useMemo, useState } from 'react'
import type { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'
import { useKernel, useQuery } from '../../api.ts'
import { useI18n, type Key } from '../../i18n.ts'
import { ErrorLine, Section } from '../common.tsx'
import { importPath, projectIdFor, recoverGraph, type StepState } from '../modelPipeline.ts'

// S1 in detail: what the recovered graph is and why, drawn as an operator flow;
// which tensor feeds which node; the work per token; the L0 generations; and the
// comparison with the official implementation. Everything comes from the runs'
// outputs (model.graph, model.census, l0g.outputs, upstream.report).

type Json = any

const NODE_KEYS: Record<string, Key> = {
  embed: 's1.node.embed', attn_norm: 's1.node.attn_norm', q_proj: 's1.node.q_proj', k_proj: 's1.node.k_proj', v_proj: 's1.node.v_proj',
  q_norm: 's1.node.q_norm', k_norm: 's1.node.k_norm', rope: 's1.node.rope', attention: 's1.node.attention', o_proj: 's1.node.o_proj',
  residual_attn: 's1.node.residual', mlp_norm: 's1.node.mlp_norm', gate_proj: 's1.node.gate_proj', up_proj: 's1.node.up_proj',
  act: 's1.node.act', gate_mul: 's1.node.gate_mul', down_proj: 's1.node.down_proj', residual_mlp: 's1.node.residual',
  final_norm: 's1.node.final_norm', lm_head: 's1.node.lm_head', next_token: 's1.node.next_token',
}
const OP_CLASS: Record<string, string> = {
  linear: 'mm', embedding: 'lookup', argmax: 'lookup', rmsnorm: 'norm', head_rmsnorm: 'norm', rope: 'rope', attention: 'attn', add: 'elt', mul: 'elt', silu: 'elt',
}
const FAMILY_KEYS: Record<string, Key> = { llama: 's1.family.llama', qwen2: 's1.family.qwen2', qwen3: 's1.family.qwen3', llama2c: 's1.family.llama2c' }
const REASON_KEYS: Record<string, Key> = {
  CONFIG_MISSING: 's1.reason.CONFIG_MISSING', FAMILY_UNSUPPORTED: 's1.reason.FAMILY_UNSUPPORTED', QUANTIZED_FORMAT: 's1.reason.QUANTIZED_FORMAT',
  ACTIVATION_UNSUPPORTED: 's1.reason.ACTIVATION_UNSUPPORTED', ROPE_SCALING_UNSUPPORTED: 's1.reason.ROPE_SCALING_UNSUPPORTED', HYPER_MISSING: 's1.reason.HYPER_MISSING',
}
const HYPER: [string, Key][] = [
  ['layers', 's1.h.layers'], ['hidden', 's1.h.hidden'], ['heads', 's1.h.heads'], ['kvHeads', 's1.h.kvHeads'], ['headDim', 's1.h.headDim'],
  ['intermediate', 's1.h.intermediate'], ['vocab', 's1.h.vocab'], ['ropeTheta', 's1.h.ropeTheta'], ['normEps', 's1.h.normEps'],
]
const RUN_STEPS: [string, Key][] = [['importPath', 's1.run.importPath'], ['importWeights', 's1.run.importWeights'], ['recover', 's1.run.recover'], ['graphCheck', 's1.run.graphCheck'], ['l0', 's1.run.l0'], ['compare', 's1.run.compare'], ['upstreamCheck', 's1.run.upstreamCheck']]

async function json(client: KernelClient, hash: string | undefined): Promise<Json> {
  return hash ? (await client.download(hash)).json() : undefined
}

function latest(runs: RunRecord[] | undefined, handler: string): RunRecord | undefined {
  return runs?.filter((r) => r.plugin === 'model-llama' && r.handler === handler && r.execution === 'exited_zero').at(-1)
}

interface ModelChoice {
  project: string
  label: string
}

/** Every model this workbench has imported, across projects, for the model picker. */
export function useModelChoices(): ModelChoice[] {
  const { t } = useI18n()
  const { data } = useQuery((c) => c.call('runs.list', { limit: 1000 }), [])
  const seen = new Map<string, ModelChoice>()
  for (const r of data?.runs ?? []) {
    if (r.plugin !== 'model-llama' || r.handler !== 'import' || r.execution !== 'exited_zero' || !r.project) continue
    const params = r.params as { path?: string }
    // Imported from a project's own weights (no path): name it after the project.
    const label = params.path ? (params.path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() ?? r.project) : t('s1.ownWeights', { project: r.project })
    seen.set(r.project, { project: r.project, label })
  }
  return [...seen.values()]
}

export function S1Detail({ project, onBack, onProject }: { project: string; onBack(): void; onProject(project: string): void }) {
  const { t, message } = useI18n()
  const { client } = useKernel()
  const { data: runsData } = useQuery((c) => c.call('runs.list', { project, limit: 1000 }), [project])
  const runs = runsData?.runs
  const imported = latest(runs, 'import')
  const recovered = latest(runs, 'recover')
  const l0 = latest(runs, 'run_graph')
  const compared = latest(runs, 'compare')
  const ownWeights = runs?.some((r) => r.plugin === 'model-llama' && (r.handler === 'synthesize' || r.handler === 'intake') && r.execution === 'exited_zero')
  const out = (r: RunRecord | undefined, name: string) => r?.outputs.find((o) => o.name === name)?.hash
  const { data: graph } = useQuery((c) => json(c, out(recovered, 'graph')), [out(recovered, 'graph')], false)
  const { data: census } = useQuery((c) => json(c, out(imported, 'census')), [out(imported, 'census')], false)
  const { data: bundle } = useQuery((c) => json(c, out(imported, 'bundle')), [out(imported, 'bundle')], false)
  const { data: outputs } = useQuery((c) => json(c, out(l0, 'outputs')), [out(l0, 'outputs')], false)
  const { data: suite } = useQuery((c) => json(c, out(compared, 'report')), [out(compared, 'report')], false)
  const { data: state } = useQuery((c) => c.call('projects.get', { project }), [project])
  const { data: verdicts } = useQuery((c) => c.call('verdicts.list', { limit: 1000 }), [])
  const choices = useModelChoices()
  const verdictOf = (check: string, r: RunRecord | undefined) => verdicts?.verdicts.find((v) => v.check === check && v.run === r?.id)
  const [progress, setProgress] = useState<Record<string, { state: StepState; detail?: string }>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [path, setPath] = useState('')
  const [prompts, setPrompts] = useState('')
  const [steps, setSteps] = useState(8)
  const g0 = state?.gates.find((g) => g.gate === 'G0')
  const entered = state?.stages.find((s) => s.id === 'S1')?.entered
  const canRun = Boolean(imported || ownWeights)

  const track = (step: string, s: StepState, detail?: string) => setProgress((p) => ({ ...p, [step]: { state: s, ...(detail ? { detail } : {}) } }))
  const go = async (work: () => Promise<void>) => {
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
  const options = () => ({ prompts: prompts.split('\n').map((x) => x.trim()).filter(Boolean), steps })
  const runHere = () => go(() => recoverGraph(client, project, track, options()))
  const importOther = () =>
    go(async () => {
      const target = projectIdFor(path)
      track('importPath', 'running')
      await importPath(client, target, path)
      track('importPath', 'done')
      onProject(target)
      await recoverGraph(client, target, track, options())
    })

  return (
    <>
      <button className="link step-back" onClick={onBack}>
        ← {t('s0.back')}
      </button>
      <Section title={t('s1.title')}>
        <p className="flow-what">{t('s1.intro')}</p>
        <h4>{t('s1.pick')}</h4>
        <div className="s1-picker">
          {(ownWeights || imported) && !choices.some((c) => c.project === project) && (
            <button className="active">{t('s1.pickOwn')}</button>
          )}
          {choices.map((c) => (
            <button key={c.project} className={c.project === project ? 'active' : ''} onClick={() => onProject(c.project)} title={c.project}>
              {c.label}
            </button>
          ))}
        </div>
        <div className="form inline">
          <input className="s1-path" value={path} onChange={(e) => setPath(e.target.value)} placeholder={t('s1.pathPlaceholder')} />
          <button disabled={busy || !path} onClick={importOther}>
            {t('s1.importPath')}
          </button>
        </div>
        <p className="muted">{t('s1.pickNote')}</p>
        {bundle && <p>{bundle.source.startsWith('artifact ') ? t('s1.modelFromProject', { project }) : t('s1.model', { name: bundle.name, source: bundle.source })}</p>}
        {g0 && !g0.released && runs && <p className="flow-note">{t(entered ? 's1.entered' : 's1.notEntered', { status: t(`status.${g0.status}` as Key) })}</p>}
        {!canRun && runs && <p className="muted">{t('s1.noImport')}</p>}
      </Section>

      <Section title={t('s1.testEntry')}>
        <p className="flow-what">{t('s1.testIntro')}</p>
        <div className="form grid">
          <label className="wide">
            {t('s1.prompts')}
            <textarea rows={3} value={prompts} onChange={(e) => setPrompts(e.target.value)} placeholder={t('s1.promptsPlaceholder')} />
          </label>
          <label>
            {t('s1.steps')}
            <input type="number" min={1} max={64} value={steps} onChange={(e) => setSteps(Number(e.target.value))} />
          </label>
          <button className="active" disabled={busy || !canRun} onClick={runHere}>
            {busy ? t('playground.running') : t(recovered ? 's1.rerun' : 's1.start')}
          </button>
        </div>
        {Object.keys(progress).length > 0 && (
          <ul className="s1-progress">
            {RUN_STEPS.filter(([id]) => progress[id]).map(([id, label]) => (
              <li key={id} className={progress[id]!.state}>
                {progress[id]!.state === 'running' ? '…' : progress[id]!.state === 'done' ? '✓' : '✗'} {t(label)}
                {progress[id]!.detail && <small className="muted"> {['pass', 'fail', 'indeterminate'].includes(progress[id]!.detail!) ? t(`flow.verdict.${progress[id]!.detail}` as Key) : progress[id]!.detail}</small>}
              </li>
            ))}
          </ul>
        )}
        <ErrorLine error={error} />
        <ul className="flow-checks">
          {(
            [
              ['model.graph', recovered, 'flow.check.graph'],
              ['model.upstream', compared, 'flow.check.upstream'],
            ] as const
          ).map(([check, r, title]) => {
            const v = verdictOf(check, r)
            return (
              <li key={check}>
                <span className={`pill ${v?.verdict === 'pass' ? 'ok' : v ? 'bad' : ''}`}>{t(`flow.verdict.${v?.verdict ?? 'none'}` as Key)}</span>
                {t(title)}
                {v && v.verdict !== 'pass' && <small className="muted"> · {v.reasons.map(message).join('; ')}</small>}
              </li>
            )
          })}
        </ul>
      </Section>

      {suite?.items && <TestSuite suite={suite} />}
      {graph && !graph.supported && (
        <Section title={t('s1.unsupportedTitle')}>
          <p className="s0-answer infeasible">
            <span>{t(REASON_KEYS[graph.reason?.code] ?? 's1.reason.other', flatten(graph.reason?.params))}</span>
          </p>
        </Section>
      )}
      {graph?.supported && (
        <>
          <Identity graph={graph} census={census} />
          <GraphDiagram graph={graph} census={census} />
          <TextIr graph={graph} />
          <TensorMap graph={graph} census={census} />
          <Work graph={graph} />
        </>
      )}
      {outputs && <L0Outputs outputs={outputs} />}
    </>
  )
}

function useFormat() {
  const { lang } = useI18n()
  return useMemo(() => {
    const locale = lang === 'zh' ? 'zh-CN' : 'en-US'
    return {
      int: (v: number) => new Intl.NumberFormat(locale).format(v),
      compact: (v: number) => new Intl.NumberFormat(locale, { notation: 'compact', maximumFractionDigits: 1 }).format(v),
      sci: (v: number) => (v === 0 ? '0' : v.toExponential(1)),
      pct: (v: number) => `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(100 * v)}%`,
    }
  }, [lang])
}

function flatten(params: Record<string, unknown> | undefined): Record<string, unknown> {
  return Object.fromEntries(Object.entries(params ?? {}).map(([k, v]) => [k, Array.isArray(v) ? v.join(', ') : typeof v === 'object' && v ? JSON.stringify(v) : v]))
}

/** Where a hyper-parameter came from, in words. */
function sourceText(source: string | undefined, t: (k: Key, p?: Record<string, unknown>) => string): string {
  if (!source) return t('s1.src.default')
  if (source.startsWith('config.json:')) return t('s1.src.config', { field: source.slice('config.json:'.length).split(';')[0] })
  if (source.startsWith('tensor:')) return t('s1.src.tensor', { tensor: source.slice('tensor:'.length) })
  if (source.includes('llama2.c header')) return t('s1.src.llama2c')
  if (source.includes('run.c')) return t('s1.src.runc')
  if (source === 'hidden / heads') return t('s1.src.derived')
  if (source.startsWith('tensor names')) return t('s1.src.layerNames')
  return source
}

function Identity({ graph, census }: { graph: Json; census: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const h = graph.hyper
  const d = Object.fromEntries(graph.decisions.map((x: Json) => [x.id, x]))
  const sentences: string[] = [
    t(d.qkNorm.value ? 's1.d.qkNormYes' : 's1.d.qkNormNo', { tensor: d.qkNorm.params.tensor }),
    t(d.qkvBias.value ? 's1.d.biasYes' : 's1.d.biasNo', { tensor: d.qkvBias.params.tensor }),
    t(d.rope.value === 'half' ? 's1.d.ropeHalf' : 's1.d.ropeInterleaved', { theta: f.int(d.rope.params.theta) }),
    t('s1.d.gqa', { heads: h.heads, kvHeads: h.kvHeads, group: d.gqa.value, headDim: h.headDim }),
    t(!d.tied.value ? 's1.d.untied' : d.tied.params.duplicate ? 's1.d.tiedDuplicate' : 's1.d.tied', { tensor: d.tied.params.duplicate }),
    t('s1.d.swiglu', { act: d.activation.params.activation }),
    t('s1.d.norm', { eps: d.norm.params.eps }),
    ...(d.window.value ? [t('s1.d.window', { window: d.window.value })] : []),
    t('s1.d.decoding'),
  ]
  return (
    <Section title={t('s1.identity')}>
      <div className="s0-answer feasible">
        <strong>{t(FAMILY_KEYS[graph.family] ?? 's1.family.llama')}</strong>
        <span>{t('s1.architecture', { architecture: graph.architecture ?? graph.family, layers: h.layers, params: f.compact(census?.totals?.parameters ?? 0) })}</span>
      </div>
      <h4>{t('s1.decisions')}</h4>
      <ul className="s1-decisions">
        {sentences.map((s) => (
          <li key={s}>{s}</li>
        ))}
      </ul>
      <h4>{t('s1.hyper')}</h4>
      <table>
        <thead>
          <tr>
            <th>{t('s1.col.param')}</th>
            <th>{t('s1.col.value')}</th>
            <th>{t('s1.col.source')}</th>
          </tr>
        </thead>
        <tbody>
          {HYPER.map(([key, label]) => (
            <tr key={key}>
              <td>{t(label)}</td>
              <td>
                <strong>{typeof h[key] === 'number' ? (h[key] < 1 ? h[key] : f.int(h[key])) : String(h[key])}</strong>
              </td>
              <td className="muted">{sourceText(census?.hyper?.[key]?.source, t)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  )
}

/** Shape of a node's output in words, e.g. "T × 2,048". */
function shapeText(node: Json, graph: Json, f: ReturnType<typeof useFormat>): string {
  const h = graph.hyper
  const size = (sym: string) => ({ d: h.hidden, 'H·hd': h.heads * h.headDim, 'KV·hd': h.kvHeads * h.headDim, ffn: h.intermediate, V: h.vocab })[sym]
  if (node.op === 'argmax') return '1'
  const last = node.shape?.[1]
  const n = size(last) ?? Number(last)
  return Number.isFinite(n) ? `T × ${f.int(n)}` : ''
}

function GraphNode({ node, graph }: { node: Json; graph: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const h = graph.hyper
  const extra =
    node.op === 'attention'
      ? t('s1.attn', { heads: h.heads, kvHeads: h.kvHeads, window: node.attrs.window ? t('s1.attnWindow', { window: node.attrs.window }) : '' })
      : node.op === 'rope'
        ? t(node.attrs.style === 'half' ? 's1.ropeHalfShort' : 's1.ropeInterleavedShort', { theta: f.int(node.attrs.theta) })
        : node.op === 'add'
          ? t('s1.residual')
          : ''
  const weight = node.tiedTo ? t('s1.tiedTo', { tensor: node.tiedTo }) : node.weight
  return (
    <div className={`s1-node ${OP_CLASS[node.op] ?? ''}`}>
      <div className="s1-node-head">
        <strong>{t(NODE_KEYS[node.id] ?? 's1.node.other')}</strong>
        <span className="s1-shape">{shapeText(node, graph, f)}</span>
      </div>
      {extra && <small>{extra}</small>}
      {weight && <code title={weight}>{weight}</code>}
      {node.bias && <code title={node.bias}>+ {node.bias}</code>}
      {node.macsPerToken > 0 && <small className="muted">{t('s1.macs', { macs: f.compact(node.macsPerToken) })}</small>}
    </div>
  )
}

/** Consecutive nodes that read the same input side by side (e.g. Q/K/V, Gate/Up). */
function rows(nodes: Json[]): Json[][] {
  const out: Json[][] = []
  for (const n of nodes) {
    const prev = out.at(-1)?.[0]
    if (prev && prev.op === n.op && (n.op === 'linear' || n.op === 'head_rmsnorm') && prev.inputs[0] === n.inputs[0]) out.at(-1)!.push(n)
    else if (prev && prev.op === 'head_rmsnorm' && n.op === 'head_rmsnorm') out.at(-1)!.push(n)
    else out.push([n])
  }
  return out
}

function NodeRows({ nodes, graph }: { nodes: Json[]; graph: Json }) {
  return (
    <>
      {rows(nodes).map((row) => (
        <div key={row.map((n) => n.id).join()} className="s1-row">
          {row.map((n) => (
            <GraphNode key={n.id} node={n} graph={graph} />
          ))}
        </div>
      ))}
    </>
  )
}

function GraphDiagram({ graph, census }: { graph: Json; census: Json }) {
  const { t } = useI18n()
  const n = graph.nodes
  return (
    <Section title={t('s1.graph')}>
      <p className="flow-what">{t('s1.graphIntro', { layers: n.repeat, nodes: n.prologue.length + n.block.length * n.repeat + n.epilogue.length })}</p>
      <div className="s1-legend">
        {(['mm', 'norm', 'rope', 'attn', 'elt', 'lookup'] as const).map((c) => (
          <span key={c} className={`s1-node ${c}`}>
            {t(`s1.cls.${c}` as Key)}
          </span>
        ))}
      </div>
      <div className="s1-graph">
        <div className="s1-io">{t('s1.input')}</div>
        <NodeRows nodes={n.prologue} graph={graph} />
        <div className="s1-block">
          <div className="s1-block-title">{t('s1.block', { layers: n.repeat })}</div>
          <NodeRows nodes={n.block} graph={graph} />
        </div>
        <NodeRows nodes={n.epilogue} graph={graph} />
        <div className="s1-io">{t('s1.output')}</div>
      </div>
      {census && <p className="muted">{t('s1.graphNote', { tensors: census.totals.tensors })}</p>}
    </Section>
  )
}

function TensorMap({ graph, census }: { graph: Json; census: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const cov = graph.coverage
  const tensors = Object.fromEntries((census?.tensors ?? []).map((x: Json) => [x.name, x]))
  const groups = new Map<string, { node: string; names: string[]; params: number; dtype: string; shape: string }>()
  for (const [name, at] of Object.entries(cov.boundTo as Record<string, string>)) {
    const node = at.replace(/\[\d+\]$/, '')
    const pattern = name.replace(/\.(\d+)\./, '.{i}.')
    const key = `${node}|${pattern}`
    const g = groups.get(key) ?? { node, names: [] as string[], params: 0, dtype: tensors[name]?.dtype ?? '', shape: (tensors[name]?.shape ?? []).join(' × ') }
    g.names.push(name)
    g.params += tensors[name]?.params ?? 0
    groups.set(key, g)
  }
  const ok = cov.unmatched.length === 0 && cov.missing.length === 0
  return (
    <Section title={t('s1.tensors')}>
      <p className={ok ? 'ok-text' : 'error'}>
        {t(ok ? 's1.coverageOk' : 's1.coverageBad', { total: cov.tensors, bound: cov.bound, ignored: cov.ignored.length, unmatched: cov.unmatched.length, missing: cov.missing.length })}
      </p>
      <table className="s0-table">
        <thead>
          <tr>
            <th>{t('s1.col.node')}</th>
            <th>{t('s1.col.tensor')}</th>
            <th>{t('s1.col.count')}</th>
            <th>{t('s1.col.shape')}</th>
            <th>{t('s1.col.dtype')}</th>
            <th>{t('s1.col.params')}</th>
          </tr>
        </thead>
        <tbody>
          {[...groups.entries()].map(([key, g]) => (
            <tr key={key}>
              <td>{t(NODE_KEYS[g.node] ?? 's1.node.other')}</td>
              <td>
                <code>{key.split('|')[1]}</code>
              </td>
              <td>{g.names.length}</td>
              <td>{g.shape}</td>
              <td>{g.dtype}</td>
              <td>{f.compact(g.params)}</td>
            </tr>
          ))}
          {cov.ignored.map((x: Json) => (
            <tr key={x.name} className="muted">
              <td>{t('s1.ignored')}</td>
              <td>
                <code>{x.name}</code>
              </td>
              <td colSpan={4}>{t(x.reason === 'DUPLICATE_OF_EMBEDDING' ? 's1.ignore.duplicate' : 's1.ignore.ropeTable')}</td>
            </tr>
          ))}
          {[...cov.unmatched, ...cov.missing].map((name: string) => (
            <tr key={name} className="error">
              <td>{t(cov.unmatched.includes(name) ? 's1.unmatched' : 's1.missing')}</td>
              <td colSpan={5}>
                <code>{name}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  )
}

function Work({ graph }: { graph: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const n = graph.nodes
  const rowsByNode: [string, number][] = []
  for (const node of n.block) if (node.macsPerToken > 0) rowsByNode.push([node.id, node.macsPerToken * n.repeat])
  for (const node of [...n.prologue, ...n.epilogue]) if (node.macsPerToken > 0) rowsByNode.push([node.id, node.macsPerToken])
  const context = 64
  rowsByNode.push(['attention', 2 * graph.hyper.heads * graph.hyper.headDim * context * n.repeat])
  const total = rowsByNode.reduce((s, [, v]) => s + v, 0)
  const max = Math.max(...rowsByNode.map(([, v]) => v))
  return (
    <Section title={t('s1.work')}>
      <p className="flow-what">{t('s1.workIntro', { total: f.compact(total), context })}</p>
      {rowsByNode.map(([id, v]) => (
        <div key={id} className="s0-row">
          <span>{t(NODE_KEYS[id] ?? 's1.node.other')}</span>
          <div className="s0-bar small">
            <div className="s0-seg" style={{ width: `${(100 * v) / max}%`, background: id === 'attention' ? '#e5883b' : '#2f6fed' }} />
          </div>
          <span>
            {f.compact(v)} · {f.pct(v / total)}
          </span>
        </div>
      ))}
    </Section>
  )
}

export function L0Outputs({ outputs }: { outputs: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  return (
    <Section title={t('s1.l0')}>
      <p className="flow-what">{t('s1.l0Intro', { steps: outputs.steps })}</p>
      {outputs.prompts.map((p: Json) => (
        <div key={p.prompt} className="s1-prompt">
          <p className="generated">
            <span className="muted">{p.prompt}</span>
            <strong>{p.completion}</strong>
          </p>
          <div className="tokens">
            {p.promptTokens.map((x: Json, i: number) => (
              <span key={`p${i}`} className="token" title={`id ${x.id}`}>
                {x.text}
              </span>
            ))}
            {p.generated.map((x: Json, i: number) => (
              <span key={`g${i}`} className="token gen" title={`id ${x.id}`}>
                {x.text}
              </span>
            ))}
          </div>
          <h4>{t('s1.nextToken')}</h4>
          {p.nextToken.map((x: Json) => (
            <div key={x.id} className="s0-row">
              <span className="mono">{x.text}</span>
              <div className="s0-bar small">
                <div className="s0-seg" style={{ width: `${100 * x.probability}%`, background: '#12a594' }} />
              </div>
              <span>{f.pct(x.probability)}</span>
            </div>
          ))}
        </div>
      ))}
    </Section>
  )
}

const TEST_TITLE: Record<string, Key> = {
  weights: 's1.test.weights', config: 's1.test.config', params: 's1.test.params', layers: 's1.test.layers', logits: 's1.test.logits',
  greedy: 's1.test.greedy', top1: 's1.test.top1', single: 's1.test.single', long: 's1.test.long',
}
const TEST_METHOD: Record<string, Key> = {
  weights: 's1.method.weights', config: 's1.method.config', params: 's1.method.params', layers: 's1.method.layers', logits: 's1.method.logits',
  greedy: 's1.method.greedy', top1: 's1.method.top1', single: 's1.method.single', long: 's1.method.long',
}

/** The measured values of one test item, in words. */
function measured(item: Json, t: (k: Key, p?: Record<string, unknown>) => string, f: ReturnType<typeof useFormat>): string {
  const v = item.values
  switch (item.kind) {
    case 'weights':
      return t('s1.val.weights', { compared: v.compared, mismatched: v.mismatched, missing: v.missing })
    case 'config':
      return t('s1.val.config', { compared: v.compared, differ: item.details.filter((d: Json) => !d.same).length })
    case 'params':
      return t('s1.val.params', { graph: f.int(v.graph), reference: f.int(v.reference) })
    case 'layers':
      return t('s1.val.layers', { points: v.points, rel: f.sci(v.worstRel) })
    case 'logits':
    case 'single':
    case 'long':
      return t('s1.val.logits', { tokens: v.tokens, abs: f.sci(v.maxAbs), rel: f.sci(v.maxRel) })
    case 'greedy':
      return v.firstDifference === null ? t('s1.val.greedySame', { steps: v.steps }) : t('s1.val.greedyDiff', { steps: v.steps, at: v.firstDifference + 1 })
    case 'top1':
      return t('s1.val.top1', { positions: v.positions, agreement: f.pct(v.agreement) })
    default:
      return ''
  }
}

function TestDetails({ item }: { item: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  if (item.kind === 'layers') {
    return (
      <table className="s0-table">
        <thead>
          <tr>
            <th>{t('s1.col.point')}</th>
            <th>{t('s1.col.maxAbsValue')}</th>
            <th>{t('s1.col.maxRel')}</th>
            <th>{t('s1.col.result')}</th>
          </tr>
        </thead>
        <tbody>
          {item.details.map((d: Json) => {
            const [kind, index] = d.point.split(':')
            return (
              <tr key={d.point}>
                <td>{kind === 'embedding' ? t('s1.point.embedding') : kind === 'layer' ? t('s1.point.layer', { i: Number(index) + 1 }) : t('s1.point.final', { i: Number(index) + 1 })}</td>
                <td>{f.sci(d.maxAbs)}</td>
                <td>{f.sci(d.maxRel)}</td>
                <td className={d.passed ? 'ok-text' : 'error'}>{t(d.passed ? 'flow.verdict.pass' : 'flow.verdict.fail')}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    )
  }
  if (item.kind === 'config') {
    return (
      <table className="s0-table">
        <thead>
          <tr>
            <th>{t('s1.col.param')}</th>
            <th>{t('s1.col.graphValue')}</th>
            <th>{t('s1.col.referenceValue')}</th>
            <th>{t('s1.col.result')}</th>
          </tr>
        </thead>
        <tbody>
          {item.details.map((d: Json) => (
            <tr key={d.key}>
              <td>{t((HYPER.find(([k]) => k === d.key)?.[1] ?? 's1.h.tied') as Key)}</td>
              <td>{String(d.graph)}</td>
              <td>{String(d.reference)}</td>
              <td className={d.same ? 'ok-text' : 'error'}>{t(d.same ? 'flow.verdict.pass' : 'flow.verdict.fail')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    )
  }
  if (item.kind === 'greedy') {
    return (
      <dl className="facts">
        <dt>{t('s1.col.ours')}</dt>
        <dd className={item.passed ? 'ok-text' : 'error'}>{item.values.ours}</dd>
        <dt>{t('s1.col.reference')}</dt>
        <dd>{item.values.reference}</dd>
      </dl>
    )
  }
  if (item.kind === 'weights' && item.details.length > 0) {
    return (
      <ul>
        {item.details.map((d: Json) => (
          <li key={d.tensor}>
            <code>{d.tensor}</code> {d.missing ? t('s1.missing') : t('s1.val.weightDiff', { abs: d.maxAbs === null ? '—' : f.sci(d.maxAbs) })}
          </li>
        ))}
      </ul>
    )
  }
  return <p className="muted">{t(TEST_METHOD[item.kind] ?? 's1.method.logits')}</p>
}

export function TestSuite({ suite }: { suite: Json }) {
  const { t } = useI18n()
  const f = useFormat()
  const [open, setOpen] = useState<string | null>(null)
  const s = suite.summary
  return (
    <Section title={t('s1.suite')}>
      <div className={`s0-answer ${s.failed === 0 ? 'feasible' : 'infeasible'}`}>
        <strong>{t(s.failed === 0 ? 's1.suitePass' : 's1.suiteFail', { passed: s.passed, total: s.total, failed: s.failed })}</strong>
        <span>{t('s1.suiteAgainst', { backend: t(suite.backend === 'transformers' ? 's1.backend.transformers' : 's1.backend.runc', { version: suite.backendVersion }), prompts: suite.prompts?.length ?? 0, steps: suite.steps })}</span>
      </div>
      <p className="muted">{t('s1.upstreamHow')}</p>
      <table className="s0-table s1-tests">
        <thead>
          <tr>
            <th>#</th>
            <th>{t('s1.col.test')}</th>
            <th>{t('s1.col.method')}</th>
            <th>{t('s1.col.threshold')}</th>
            <th>{t('s1.col.measured')}</th>
            <th>{t('s1.col.result')}</th>
          </tr>
        </thead>
        <tbody>
          {suite.items.map((item: Json, n: number) => (
            <Fragment key={item.id}>
              <tr className="clickable" onClick={() => setOpen(open === item.id ? null : item.id)}>
                <td>{n + 1}</td>
                <td>
                  <strong>{t(TEST_TITLE[item.kind] ?? 's1.test.logits')}</strong>
                  {item.prompt && <div className="muted">“{item.prompt}”</div>}
                </td>
                <td className="muted">{t(TEST_METHOD[item.kind] ?? 's1.method.logits')}</td>
                <td>{item.threshold === undefined ? t('s1.exact') : item.kind === 'top1' ? `≥ ${f.pct(item.threshold)}` : `≤ ${f.sci(item.threshold)}`}</td>
                <td>{measured(item, t, f)}</td>
                <td>
                  <span className={`pill ${item.passed ? 'ok' : 'bad'}`}>{t(item.passed ? 'flow.verdict.pass' : 'flow.verdict.fail')}</span>
                </td>
              </tr>
              {open === item.id && (
                <tr>
                  <td />
                  <td colSpan={5}>
                    <TestDetails item={item} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
      <p className="muted">{t('s1.suiteClick')}</p>
    </Section>
  )
}

/** The recovered graph as a short program: what is computed, in order, with which weights. */
export function TextIr({ graph }: { graph: Json }) {
  const { t } = useI18n()
  const h = graph.hyper
  const line = (n: Json, indent: string): string => {
    const args = [n.inputs.join(', '), n.weight ? `W=${n.weight}` : '', n.bias ? `b=${n.bias}` : '', n.attrs?.eps !== undefined ? `eps=${n.attrs.eps}` : '']
    if (n.op === 'rope') args.push(`θ=${n.attrs.theta}`, n.attrs.style)
    if (n.op === 'attention') args.push(`heads=${h.heads}`, `kv_heads=${h.kvHeads}`, 'causal', ...(n.attrs.window ? [`window=${n.attrs.window}`] : []))
    if (n.op === 'linear' && n.tiedTo) args.push('tied')
    return `${indent}${n.outputs.join(', ')} = ${n.op}(${args.filter(Boolean).join(', ')})`
  }
  const lines = [
    `# ${graph.architecture ?? graph.family}: ${h.layers} × decoder layer, d=${h.hidden}, heads=${h.heads}/${h.kvHeads}, head_dim=${h.headDim}, ffn=${h.intermediate}, vocab=${h.vocab}`,
    ...graph.nodes.prologue.map((n: Json) => line(n, '')),
    `for i in range(${graph.nodes.repeat}):`,
    ...graph.nodes.block.map((n: Json) => line(n, '    ')),
    ...graph.nodes.epilogue.map((n: Json) => line(n, '')),
  ]
  return (
    <Section title={t('s1.textIr')}>
      <p className="flow-what">{t('s1.textIrIntro')}</p>
      <pre className="s1-ir">{lines.join('\n')}</pre>
    </Section>
  )
}
