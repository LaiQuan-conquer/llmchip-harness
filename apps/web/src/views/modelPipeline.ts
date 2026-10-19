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

/** S0 from real weights. Returns the verdict of the feasibility check. */
export async function startFromWeights(client: KernelClient, o: StartOptions, report: Report): Promise<string> {
  const step = async <T,>(name: string, work: () => Promise<T>, detail?: (value: T) => string): Promise<T> => {
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
  const step = async <T,>(name: string, work: () => Promise<T>, detail?: (value: T) => string): Promise<T> => {
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

/** A project id from a model directory, e.g. ~/models/Qwen3-0.6B -> qwen3-0-6b. */
export function projectIdFor(path: string): string {
  const base = path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() ?? 'model'
  const id = base.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  return /^[a-z]/.test(id) ? id : `m-${id}`
}
