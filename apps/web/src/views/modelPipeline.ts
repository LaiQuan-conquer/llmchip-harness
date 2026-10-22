// Starting a chip project from a downloaded model, through the kernel API as
// the owner: S0 (import, census, estimate, checks) and S1 (graph recovery, L0,
// comparison with the official implementation, checks). The G0 approval stays
// a human decision on the Approvals page.

import type { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'

export type StepState = 'running' | 'done' | 'failed'
export type Report = (step: string, state: StepState, detail?: string) => void

/** Assumptions for the estimate per process, as in the demo project. */
export const PROCESSES = {
  gf180mcu: { weightStorage: 'gf180mcu.stdcell-rom', targetAreaMm2: 2.4, parallelMacs: 2, clockMHz: 8 },
  'sephonchitech-180bcd': { weightStorage: 'sephonchitech-180bcd.foundry-rom', targetAreaMm2: 2, parallelMacs: 8, clockMHz: 50 },
} as const
export type ProcessId = keyof typeof PROCESSES

export interface StartOptions {
  path: string
  project: string
  title: string
  bits: number
  process: ProcessId
}

async function run(client: KernelClient, project: string, plugin: string, handler: string, inputs: [string, string][], params: Record<string, unknown> = {}): Promise<RunRecord> {
  const started = await client.call('runs.start', { plugin, handler, project, params, inputs: inputs.map(([name, hash]) => ({ name, hash })) })
  for (;;) {
    const r = await client.call('runs.wait', { run: started.id, timeoutMs: 600_000 })
    if (r.state !== 'finished') continue
    if (r.execution !== 'exited_zero') throw new Error(r.error ?? r.execution)
    return r
  }
}

const outputs = (r: RunRecord, name: string) => r.outputs.filter((o) => o.name === name).map((o) => o.hash)

async function judge(client: KernelClient, project: string, check: string, r: RunRecord): Promise<string> {
  return (await client.call('verdicts.evaluate', { check, run: r.id, project })).verdict
}

/** Runs one reported step: running, then done with a detail, or failed with the error. */
function stepper(report: Report) {
  return async <T,>(name: string, work: () => Promise<T>, detail?: (value: T) => string): Promise<T> => {
    report(name, 'running')
    try {
      const value = await work()
      report(name, 'done', detail?.(value))
      return value
    } catch (e) {
      report(name, 'failed', (e as Error).message)
      throw e
    }
  }
}

/** S0 from real weights. Returns the verdict of the feasibility check. */
export async function startFromWeights(client: KernelClient, o: StartOptions, report: Report): Promise<string> {
  const step = stepper(report)
  const { project } = o
  await step('project', async () => {
    const { projects } = await client.call('projects.list')
    if (!projects.some((p) => p.id === project)) await client.call('projects.create', { id: project, title: o.title })
  })
  const imported = await step('import', () => run(client, project, 'model-llama', 'import', [], { path: o.path }))
  await step('census', () => judge(client, project, 'model.census', imported), (v) => v)
  const census = outputs(imported, 'census')[0]!
  const data = (await client.call('plugins.list')).plugins.flatMap((p) => p.data)
  const profile = data.find((d) => d.type === 'process.profile' && d.id === o.process)!.hash
  const library = data.find((d) => d.type === 'implementation.library')!.hash
  await step('bind', async () => {
    const request = await client.upload(JSON.stringify({ ...PROCESSES[o.process], weightBits: o.bits, contextLength: 64, activationLocation: 'offchip', kvLocation: 'offchip' }), {
      type: 'estimate.request',
      mediaType: 'application/json',
    })
    const charter = await client.upload(`# CHARTER\nModel: ${o.title} (${o.path})\nGoal: decide whether this model can become a chip on ${o.process}, then recover its computation graph.\n`, {
      type: 'project.charter',
      mediaType: 'text/markdown',
    })
    for (const [slot, hash] of [['charter', charter.hash], ['estimate-request', request.hash], ['process-profile', profile], ['impl-library', library]] as const) {
      await client.call('projects.bind', { project, slot, hash })
    }
    return request.hash
  })
  const state = await client.call('projects.get', { project })
  const request = state.slots['estimate-request']!
  const estimate = await step('estimate', () => run(client, project, 'estimator', 'estimate', [['request', request], ['profile', profile], ['library', library], ['census', census]]))
  return step('feasibility', () => judge(client, project, 'estimate.feasibility', estimate), (v) => v)
}

export interface TestOptions {
  /** Prompts for L0 and the test suite; the plugin's defaults when empty. */
  prompts?: string[]
  steps?: number
}

/** The latest successful import of a project, if any. */
export async function latestImport(client: KernelClient, project: string): Promise<RunRecord | undefined> {
  const { runs } = await client.call('runs.list', { project, limit: 1000 })
  return runs.filter((r) => r.plugin === 'model-llama' && r.handler === 'import' && r.execution === 'exited_zero').at(-1)
}

/**
 * Imports the weights a project already has (e.g. the proxy model of S1, as
 * llama2.c artifacts) so that its graph can be recovered like any downloaded model.
 */
export async function importProjectWeights(client: KernelClient, project: string): Promise<RunRecord> {
  const { runs } = await client.call('runs.list', { project, limit: 1000 })
  const ok = runs.filter((r) => r.plugin === 'model-llama' && r.execution === 'exited_zero')
  // The model the project made (the proxy) when there is one, else the llama2.c checkpoint it took in.
  const source = ok.filter((r) => r.handler === 'synthesize').at(-1) ?? ok.filter((r) => r.handler === 'intake').at(-1)
  if (!source) throw new Error('this project has no model weights yet')
  const pick = (name: string) => (source.handler === 'synthesize' ? source.outputs : source.inputs).find((o) => o.name === name)!.hash
  return run(client, project, 'model-llama', 'import', [['weights', pick('weights')], ['tokenizer', pick('tokenizer')]], { name: source.handler === 'synthesize' ? 'proxy' : 'llama2.c' })
}

/** Imports a model from a path on this machine into a project (created if missing), without the S0 estimate. */
export async function importPath(client: KernelClient, project: string, path: string): Promise<RunRecord> {
  const { projects } = await client.call('projects.list')
  const title = path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || project
  if (!projects.some((p) => p.id === project)) await client.call('projects.create', { id: project, title })
  return run(client, project, 'model-llama', 'import', [], { path })
}

/** S1: graph recovery, L0 and the full test suite, from the project's latest import. */
export async function recoverGraph(client: KernelClient, project: string, report: Report, options: TestOptions = {}): Promise<void> {
  const step = stepper(report)
  const imported = (await latestImport(client, project)) ?? (await step('importWeights', () => importProjectWeights(client, project)))
  // Enter S1 when G0 allows it; otherwise the runs are analysis outside the stage.
  const release = await client.call('gates.release', { project, gate: 'G0' })
  const state = await client.call('projects.get', { project })
  if (release.released && !state.stages.find((s) => s.id === 'S1')?.entered) await client.call('stages.enter', { project, stage: 'S1' })
  const census = outputs(imported, 'census')[0]!
  const graph = await step('recover', () => run(client, project, 'model-llama', 'recover', [['census', census]]))
  await step('graphCheck', () => judge(client, project, 'model.graph', graph), (v) => v)
  if (graph.metrics.family_supported !== true) return
  const inputs: [string, string][] = [['graph', outputs(graph, 'graph')[0]!], ['bundle', outputs(imported, 'bundle')[0]!], ...outputs(imported, 'file').map((h): [string, string] => ['files', h])]
  const params = { ...(options.prompts?.length ? { prompts: options.prompts } : {}), ...(options.steps ? { steps: options.steps } : {}) }
  await step('l0', () => run(client, project, 'model-llama', 'run_graph', inputs, params))
  const compared = await step('compare', () => run(client, project, 'model-llama', 'compare', [...inputs, ['census', census]], params), (r) => `${r.metrics.tests_passed}/${r.metrics.tests_total}`)
  await step('upstreamCheck', () => judge(client, project, 'model.upstream', compared), (v) => v)
}

/** The numeric behavior chosen in S2 (numeric/quantize, numeric/quantize_graph). */
export interface Scheme {
  weightFormat: 'int8' | 'int4' | 'int3' | 'ternary'
  groupSize: number
  /** Narrower than the proven no-overflow width when set. */
  accumulatorBits?: number
  rounding: 'half-up' | 'half-even' | 'truncate'
  overflow: 'saturate' | 'wrap'
  /** Recovered graphs only: fraction bits per layer, and the width of the residual stream. */
  perLayer?: boolean
  residualBits?: 16 | 24
}

/** How much of the evaluation set a recovered graph is evaluated on (large models are slow in L2). */
export interface EvalSize {
  maxPrompts: number
  steps: number
  passageTokens: number
}

export interface NumericSources {
  /** The llama2.c Model IR the vector engine implements: quantize gives the formal numeric spec. */
  chip?: { ir: string; tokenizer: string; evalset: string; context: number }
  /** The recovered graph of an imported model: quantize_graph gives an analysis. */
  graph?: { inputs: [string, string][]; evalset?: string }
}

const ok = (runs: RunRecord[], plugin: string, handler: string) => runs.filter((r) => r.plugin === plugin && r.handler === handler && r.execution === 'exited_zero')
const input = (r: RunRecord | undefined, name: string) => r?.inputs.find((i) => i.name === name)?.hash

/** What S2 can quantize in a project. */
export async function numericSources(client: KernelClient, project: string): Promise<NumericSources> {
  const { runs } = await client.call('runs.list', { project, limit: 1000 })
  const sources: NumericSources = {}
  const intake = ok(runs, 'model-llama', 'intake').at(-1)
  const quantized = ok(runs, 'numeric', 'quantize').at(-1)
  const evalset = input(quantized, 'evalset') ?? input(ok(runs, 'model-llama', 'reference').at(-1), 'evalset')
  if (intake && evalset) {
    const context = Number((quantized?.params as { contextLength?: number } | undefined)?.contextLength ?? 64)
    sources.chip = { ir: outputs(intake, 'ir')[0]!, tokenizer: input(intake, 'tokenizer')!, evalset, context }
  }
  const imported = ok(runs, 'model-llama', 'import').at(-1)
  const recovered = ok(runs, 'model-llama', 'recover').filter((r) => r.metrics.family_supported === true).at(-1)
  if (imported && recovered) {
    const inputs: [string, string][] = [['graph', outputs(recovered, 'graph')[0]!], ['bundle', outputs(imported, 'bundle')[0]!], ...outputs(imported, 'file').map((h): [string, string] => ['files', h])]
    // A project's own llama2.c model (the proxy) has a tiny alphabet: use the evaluation set of its chip path.
    sources.graph = { inputs, ...(evalset ? { evalset } : {}) }
  }
  return sources
}

/** S2: quantizes with a scheme and evaluates L2 against L0, on the chip path or the recovered graph. */
export async function runScheme(client: KernelClient, project: string, path: 'chip' | 'graph', scheme: Scheme, size: EvalSize, report: Report): Promise<{ quantize: RunRecord; evaluate: RunRecord }> {
  const step = stepper(report)
  const sources = await numericSources(client, project)
  const params: Record<string, unknown> = { weightFormat: scheme.weightFormat, groupSize: scheme.groupSize, rounding: scheme.rounding, overflow: scheme.overflow }
  if (scheme.accumulatorBits) params.accumulatorBits = scheme.accumulatorBits
  if (path === 'chip') {
    const c = sources.chip
    if (!c) throw new Error('this project has no Model IR (S1 intake)')
    const base: [string, string][] = [['ir', c.ir], ['tokenizer', c.tokenizer], ['evalset', c.evalset]]
    const quantize = await step('quantize', () => run(client, project, 'numeric', 'quantize', base, { ...params, contextLength: c.context }))
    const spec: [string, string][] = [['spec', outputs(quantize, 'spec')[0]!], ['weights', outputs(quantize, 'weights')[0]!]]
    const evaluate = await step('evaluate', () => run(client, project, 'numeric', 'evaluate', [...base, ...spec]), (r) => `${(r.metrics.ppl_increase_pct as number).toFixed(1)}%`)
    return { quantize, evaluate }
  }
  const g = sources.graph
  if (!g) throw new Error('this project has no recovered graph (S1)')
  const base: [string, string][] = [...g.inputs, ...(g.evalset ? [['evalset', g.evalset] as [string, string]] : [])]
  const quantize = await step('quantize', () => run(client, project, 'numeric', 'quantize_graph', base, { ...params, perLayer: scheme.perLayer ?? true, residualBits: scheme.residualBits ?? 24 }))
  const spec: [string, string][] = [['spec', outputs(quantize, 'spec')[0]!], ['weights', outputs(quantize, 'weights')[0]!]]
  const evaluate = await step('evaluate', () => run(client, project, 'numeric', 'evaluate_graph', [...base, ...spec], { ...size }), (r) => `${(r.metrics.ppl_increase_pct as number).toFixed(1)}%`)
  await step('check', () => judge(client, project, 'numeric.accuracy', evaluate), (v) => v)
  return { quantize, evaluate }
}

/**
 * Makes a chip-path scheme the formal numeric spec of the project: binds the G1 slots
 * (this invalidates earlier G1/G2 approvals), judges the G1 check on its evaluation,
 * and runs L2 with observation-point traces for S3-S5. G1 itself stays a human decision.
 */
export async function adoptScheme(client: KernelClient, project: string, quantize: RunRecord, evaluate: RunRecord, report: Report): Promise<void> {
  const step = stepper(report)
  const spec = outputs(quantize, 'spec')[0]!
  const weights = outputs(quantize, 'weights')[0]!
  const tokenizer = input(quantize, 'tokenizer')!
  const evalset = input(quantize, 'evalset')!
  await step('bind', async () => {
    for (const [slot, hash] of [['model-ir', input(quantize, 'ir')!], ['tokenizer', tokenizer], ['eval-set', evalset], ['numeric-spec', spec], ['l2-weights', weights]] as const) {
      await client.call('projects.bind', { project, slot, hash })
    }
  })
  await step('budget', () => judge(client, project, 'accuracy.budget', evaluate), (v) => v)
  await step('infer', () => run(client, project, 'numeric', 'infer', [['spec', spec], ['weights', weights], ['tokenizer', tokenizer], ['evalset', evalset]]))
}

/** S3: the microarchitecture choices of the template (arch-taalas design / explore). */
export interface ArchPoint {
  parallelMacs: number
  clockMHz: number
  memoryInterface: 'spi' | 'sqi'
  operandBuffers: 'none' | 'attention' | 'all'
}

/** S3 of the spatial engine (taalas-spatial/1): softmax dividers, clock and die. */
export interface SpatialPoint {
  architecture: 'taalas-spatial/1'
  dividers: number
  clockMHz: number
  die: { xMm: number; yMm: number }
}

export const SPATIAL = 'taalas-spatial/1'

/** The prerun die of the gf180 project (owner decision 2026-10-18) and its storage. */
export const ARCH_BASE = { weightStorage: 'gf180mcu.stdcell-rom', die: { xMm: 1.2, yMm: 2 }, targetAreaMm2: 2.4 }

/**
 * The spatial engine's die: the one S3 suggests from the measured cells of the proxy (owner
 * decision 2026-10-20: the die grows as needed), and its speed target (1000 tok/s at 8 MHz).
 */
export const SPATIAL_BASE = { weightStorage: 'gf180mcu.stdcell-rom', die: { xMm: 5, yMm: 3 }, cyclesPerToken: 8000 }

/** The parameters of arch-taalas for a point of either template. */
function archParams(point: ArchPoint | SpatialPoint): Record<string, unknown> {
  return 'architecture' in point ? { ...SPATIAL_BASE, ...point } : { ...ARCH_BASE, ...point }
}

async function processData(client: KernelClient) {
  const data = (await client.call('plugins.list')).plugins.flatMap((p) => p.data)
  return { profile: data.find((d) => d.id === 'gf180mcu')!.hash, library: data.find((d) => d.type === 'implementation.library')!.hash }
}

/** The numeric spec S3 designs from: the formal one (G1 slot), else the latest chip-path or analysis spec. */
export async function archSource(client: KernelClient, project: string): Promise<{ spec: string; weights: string; analysis: boolean } | undefined> {
  const state = await client.call('projects.get', { project })
  if (state.slots['numeric-spec'] && state.slots['l2-weights']) return { spec: state.slots['numeric-spec'], weights: state.slots['l2-weights'], analysis: false }
  const { runs } = await client.call('runs.list', { project, limit: 1000 })
  const q = runs.filter((r) => r.plugin === 'numeric' && (r.handler === 'quantize' || r.handler === 'quantize_graph') && r.execution === 'exited_zero').at(-1)
  return q ? { spec: outputs(q, 'spec')[0]!, weights: outputs(q, 'weights')[0]!, analysis: q.handler === 'quantize_graph' } : undefined
}

/** S3 design-space exploration around a point (also the analysis of an imported model). */
export async function exploreArch(client: KernelClient, project: string, point: ArchPoint | SpatialPoint, report: Report): Promise<RunRecord> {
  const step = stepper(report)
  const src = await step('source', async () => {
    const s = await archSource(client, project)
    if (!s) throw new Error('this project has no numeric spec yet (S2)')
    return s
  })
  const { profile, library } = await processData(client)
  return step('explore', () => run(client, project, 'arch-taalas', 'explore', [['spec', src.spec], ['weights', src.weights], ['profile', profile], ['library', library]], exploreParams(point)), (r) => `${(r.metrics.tokens_per_second as number).toFixed(1)} tok/s`)
}

function exploreParams(point: ArchPoint | SpatialPoint): Record<string, unknown> {
  // The spatial explore takes no speed target (it is a design parameter, not an exploration axis).
  if ('architecture' in point) {
    const { cyclesPerToken: _, ...base } = SPATIAL_BASE
    return { ...base, ...point }
  }
  return { ...ARCH_BASE, ...point }
}

/**
 * S3 chip spec and PPA at a point (chip path only), its consistency check, and, with `bind`,
 * the G2 slots (this invalidates earlier G2 approvals). G2 itself stays a human decision.
 */
export async function designArch(client: KernelClient, project: string, point: ArchPoint | SpatialPoint, bind: boolean, report: Report): Promise<RunRecord> {
  const step = stepper(report)
  const src = await archSource(client, project)
  if (!src || src.analysis) throw new Error('only a chip-path numeric spec (S2, formal) can become a chip spec')
  const { profile, library } = await processData(client)
  const design = await step('design', () => run(client, project, 'arch-taalas', 'design', [['spec', src.spec], ['weights', src.weights], ['profile', profile], ['library', library]], archParams(point)), (r) => `${(r.metrics.tokens_per_second as number).toFixed(1)} tok/s`)
  const chip = outputs(design, 'chip')[0]!
  const ppa = outputs(design, 'ppa')[0]!
  if (bind) {
    await step('bind', async () => {
      await client.call('projects.bind', { project, slot: 'chip-spec', hash: chip })
      await client.call('projects.bind', { project, slot: 'ppa-estimate', hash: ppa })
    })
  }
  const check = await step('verify', () => run(client, project, 'arch-taalas', 'verify_spec', [['chip', chip], ['spec', src.spec], ['ppa', ppa], ['weights', src.weights]]))
  await step('archCheck', () => judge(client, project, 'arch.spec', check), (v) => v)
  return design
}

/** A project id from a model directory, e.g. ~/models/Qwen3-0.6B -> qwen3-0-6b. */
export function projectIdFor(path: string): string {
  const base = path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() ?? 'model'
  const id = base.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  return /^[a-z]/.test(id) ? id : `m-${id}`
}
