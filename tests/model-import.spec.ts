// Real weights to a recovered graph (requirements FR-MDL-01/03/05/06, FR-EST):
// a downloaded model directory on this machine is imported, censused from its
// headers, estimated, turned into a computation graph, executed as L0 and
// compared with the official implementation, all through the kernel with the
// bundled plugins. The fixture is a tiny random Qwen3 in the real Hugging Face
// layout (tests/fixtures/models/make_tiny.py).

import { copyFileSync, mkdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'
import { connect, startKernel, tempDir } from './helpers.ts'

const TINY_QWEN3 = resolve(import.meta.dirname, 'fixtures/models/tiny-qwen3')
const STORIES = resolve(import.meta.dirname, 'fixtures/models/stories260K')

const PROJECT = 'models'

async function run(client: KernelClient, plugin: string, handler: string, inputs: [string, string][], params: Record<string, unknown> = {}): Promise<RunRecord> {
  const started = await client.call('runs.start', { plugin, handler, params, project: PROJECT, inputs: inputs.map(([name, hash]) => ({ name, hash })) })
  const r = await client.call('runs.wait', { run: started.id, timeoutMs: 300_000 })
  expect(r, `${plugin}/${handler}: ${r.error}`).toMatchObject({ state: 'finished', execution: 'exited_zero' })
  return r
}

const out = (r: RunRecord, name: string) => r.outputs.filter((o) => o.name === name).map((o) => o.hash)
const json = async (client: KernelClient, hash: string): Promise<any> => (await client.download(hash)).json()
const judge = async (client: KernelClient, check: string, run: RunRecord) => (await client.call('verdicts.evaluate', { check, run: run.id, project: PROJECT })).verdict

async function setup() {
  const { kernel } = await startKernel(undefined, { bundled: true })
  const client = await connect(kernel)
  await client.call('projects.create', { id: PROJECT, title: 'Imported models' })
  return client
}

async function pipeline(client: KernelClient, path: string) {
  const imported = await run(client, 'model-llama', 'import', [], { path })
  const files = out(imported, 'file').map((h): [string, string] => ['files', h])
  const graph = await run(client, 'model-llama', 'recover', [['census', out(imported, 'census')[0]!]])
  const inputs: [string, string][] = [['graph', out(graph, 'graph')[0]!], ['bundle', out(imported, 'bundle')[0]!], ...files]
  const tests: [string, string][] = [...inputs, ['census', out(imported, 'census')[0]!]]
  return { imported, graph, inputs, tests }
}

describe('model import and graph recovery', () => {
  it('imports a downloaded model, recovers its graph and matches transformers', { timeout: 240_000 }, async () => {
    const client = await setup()
    const { imported, graph, inputs, tests } = await pipeline(client, TINY_QWEN3)

    // S0: the census from the headers, and an estimate of exactly these weights.
    expect(imported.metrics).toMatchObject({ files: 4, tensors: 24, parameters: 22_720, weight_format: 'safetensors', model_type: 'qwen3' })
    expect(await judge(client, 'model.census', imported)).toBe('pass')
    const bundle = await json(client, out(imported, 'bundle')[0]!)
    expect(bundle.files.map((f: { name: string; role: string }) => `${f.name}:${f.role}`).sort()).toEqual(
      ['config.json:config', 'generation_config.json:generation_config', 'model.safetensors:weights', 'tokenizer.json:tokenizer'],
    )
    const census = out(imported, 'census')[0]!
    const lineage = await client.call('artifacts.lineage', { hash: census })
    expect(lineage.nodes.find((n) => n.hash === census)).toMatchObject({ type: 'model.census', run: imported.id })
    const { plugins } = await client.call('plugins.list')
    const data = plugins.flatMap((p) => p.data)
    const estimate = await run(client, 'estimator', 'estimate', [
      ['census', census],
      ['profile', data.find((d) => d.id === 'gf180mcu')!.hash],
      ['library', data.find((d) => d.type === 'implementation.library')!.hash],
    ], { model: { preset: 'stories260K' }, weightStorage: 'gf180mcu.stdcell-rom', weightBits: 2, targetAreaMm2: 2.4 })
    const report = await json(client, out(estimate, 'report')[0]!)
    // The census overrides the request's model: the estimate is of the imported weights.
    expect(report.model).toMatchObject({ params: 22_720, dim: 32, layers: 2, heads: 4, kvHeads: 2, vocab: 128, hiddenDim: 64 })

    // S1: the graph, every tensor bound, Qwen3's QK-norm and tied output recognised.
    expect(graph.metrics).toMatchObject({ family_supported: true, family: 'qwen3', tensors_unmatched: 0, tensors_missing: 0, layers: 2 })
    expect(await judge(client, 'model.graph', graph)).toBe('pass')
    const g = await json(client, out(graph, 'graph')[0]!)
    const decisions = Object.fromEntries(g.decisions.map((d: { id: string; value: unknown }) => [d.id, d.value]))
    expect(decisions).toMatchObject({ qkNorm: true, qkvBias: false, tied: true, rope: 'half', gqa: 2 })

    // L0 by executing the graph, and the comparison with the official implementation.
    const l0 = await run(client, 'model-llama', 'run_graph', inputs, { prompts: ['The capital of France is'], steps: 4 })
    const outputs = await json(client, out(l0, 'outputs')[0]!)
    expect(outputs.prompts[0]).toMatchObject({ prompt: 'The capital of France is' })
    expect(outputs.prompts[0].generated).toHaveLength(4)
    expect(outputs.prompts[0].nextToken).toHaveLength(5)
    const compared = await run(client, 'model-llama', 'compare', tests, { prompts: ['The capital of France is', 'Once upon a time'], steps: 4 })
    expect(compared.metrics).toMatchObject({ backend: 'transformers', greedy_match: true, top1_agreement: 1, tests_failed: 0 })
    // The full suite: weights, configuration, parameters, layers, per-prompt logits and generation, boundary cases.
    const suite = await json(client, out(compared, 'report')[0]!)
    expect(suite.items.map((i: { kind: string }) => i.kind)).toEqual(['weights', 'config', 'params', 'layers', 'logits', 'greedy', 'logits', 'greedy', 'top1', 'single', 'long'])
    expect(suite.items.find((i: { kind: string }) => i.kind === 'weights').values).toMatchObject({ compared: 25, mismatched: 0, missing: 0 })
    expect(suite.items.find((i: { kind: string }) => i.kind === 'layers').details).toHaveLength(3)
    expect(compared.metrics.max_rel_logit_diff as number).toBeLessThan(1e-4)
    expect(await judge(client, 'model.upstream', compared)).toBe('pass')
  })

  it('recovers llama2.c checkpoints and compares them with the run.c port', { timeout: 120_000 }, async () => {
    const client = await setup()
    const { graph, tests } = await pipeline(client, STORIES)
    expect(graph.metrics).toMatchObject({ family: 'llama2c', tensors_bound: 47, tensors_ignored: 2, tensors_unmatched: 0 })
    const compared = await run(client, 'model-llama', 'compare', tests, { steps: 16 })
    expect(compared.metrics).toMatchObject({ backend: 'run.c (NumPy port)', greedy_match: true, tests_failed: 0 })
    expect(await judge(client, 'model.upstream', compared)).toBe('pass')
  })

  it("recovers a project's own llama2.c weights (the proxy model)", { timeout: 120_000 }, async () => {
    const client = await setup()
    const proxy = await run(client, 'model-llama', 'synthesize', [], { dim: 16, hiddenDim: 32, layers: 2, heads: 2, kvHeads: 1, seqLen: 64, seed: 1 })
    const imported = await run(client, 'model-llama', 'import', [['weights', out(proxy, 'weights')[0]!], ['tokenizer', out(proxy, 'tokenizer')[0]!]], { name: 'proxy' })
    expect(imported.metrics).toMatchObject({ weight_format: 'llama2c', parameters: 5_248 })
    expect((await json(client, out(imported, 'bundle')[0]!)).source).toBe(`artifact ${out(proxy, 'weights')[0]}`)
    const graph = await run(client, 'model-llama', 'recover', [['census', out(imported, 'census')[0]!]])
    const tests: [string, string][] = [['graph', out(graph, 'graph')[0]!], ['bundle', out(imported, 'bundle')[0]!], ['census', out(imported, 'census')[0]!], ...out(imported, 'file').map((h): [string, string] => ['files', h])]
    const compared = await run(client, 'model-llama', 'compare', tests, { steps: 8 })
    expect(compared.metrics).toMatchObject({ tests_failed: 0, greedy_match: true })
  })

  it('reports a model it cannot recover instead of guessing a graph', { timeout: 120_000 }, async () => {
    const client = await setup()
    // Weights without config.json: the census still works, the graph does not.
    const dir = join(tempDir(), 'weights-only')
    mkdirSync(dir)
    copyFileSync(join(TINY_QWEN3, 'model.safetensors'), join(dir, 'model.safetensors'))
    const { imported, graph } = await pipeline(client, dir)
    expect(imported.metrics).toMatchObject({ config_present: false, all_tensors_classified: true, header_consistent: true })
    expect(graph.metrics).toMatchObject({ family_supported: false })
    expect((await json(client, out(graph, 'graph')[0]!)).reason).toMatchObject({ code: 'CONFIG_MISSING' })
    expect(await judge(client, 'model.graph', graph)).toBe('fail')
    // A path that is not on this machine fails the run with a clear error.
    const started = await client.call('runs.start', { plugin: 'model-llama', handler: 'import', params: { path: '/nonexistent/model' } })
    const failed = await client.call('runs.wait', { run: started.id, timeoutMs: 60_000 })
    expect(failed.execution).toBe('exited_nonzero')
    expect(failed.error).toContain('does not exist')
  })

  it('keeps the whole model as one archive and imports it back', { timeout: 120_000 }, async () => {
    const client = await setup()
    const imported = await run(client, 'model-llama', 'import', [], { path: TINY_QWEN3, archive: true })
    const archive = out(imported, 'model')[0]!
    const again = await run(client, 'model-llama', 'import', [['model', archive]])
    expect(again.metrics).toMatchObject({ files: 4, tensors: 24, parameters: 22_720, weight_format: 'safetensors', model_type: 'qwen3' })
    expect(out(again, 'census')[0]).toBe(out(imported, 'census')[0])
    const graph = await run(client, 'model-llama', 'recover', [['census', out(again, 'census')[0]!]])
    expect(graph.metrics).toMatchObject({ family: 'qwen3', tensors_missing: 0 })
  })
})
