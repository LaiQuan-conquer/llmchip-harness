// The numeric-scheme matrix of ADR-012 line A (drafts/Qwen05B_Taalas_Refactor_Plan_zh.md §12.1 A1/A2):
// three schemes on Qwen2.5-0.5B-Instruct, each built by `calibrate_graph` (GPTQ, per-matrix
// `overrides` for mixed precision) and measured against one L0 report on the same question set.
//
// Opt-in: LLMCHIP_QWEN=1 yarn test tests/qwen-schemes.spec.ts [LLMCHIP_SCHEME_SET=full].
// The quick set is the default (900 questions); the full set (7 benchmarks) takes hours and is the
// one that decides `spec.accuracy.<benchmark>`. Needs the GPU, the model and the benchmark data in
// the tools directory, and the code sandbox image; otherwise the test is skipped, visibly.

import { execFileSync } from 'node:child_process'
import { existsSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'
import { PYTHON, connect, startKernel } from './helpers.ts'

const TOOLS = process.env.LLMCHIP_TOOLS ?? resolve(import.meta.dirname, '../../tools')
const MODEL = join(TOOLS, 'models/Qwen2.5-0.5B-Instruct')
const SANDBOX = 'docker.io/library/python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1'
const SET = process.env.LLMCHIP_SCHEME_SET === 'full' ? 'full' : 'quick'
const BENCHMARKS = ['mmlu', 'cmmlu', 'ceval', 'gsm8k', 'ifeval', 'humaneval', 'mbpp']

export const SCHEMES = [
  { id: 'w8', params: { weightFormat: 'int8', groupSize: 128, contextLength: 2048, sequences: 64 } },
  {
    id: 'sensitive-w8',
    params: {
      weightFormat: 'int4',
      groupSize: 128,
      contextLength: 2048,
      sequences: 64,
      overrides: { '[0-9]+\\.o_proj': 'int8', '[0-9]+\\.down_proj': 'int8', embed: 'int8' },
    },
  },
  { id: 'w4-g64', params: { weightFormat: 'int4', groupSize: 64, contextLength: 2048, sequences: 64 } },
]

function imagePresent(image: string): boolean {
  try {
    execFileSync('podman', ['image', 'exists', image], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

function gpuPresent(): boolean {
  try {
    execFileSync(PYTHON, ['-c', 'import sys, torch; sys.exit(0 if torch.cuda.is_available() else 1)'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

const ready = !!process.env.LLMCHIP_QWEN && existsSync(join(MODEL, 'model.safetensors')) && existsSync(join(TOOLS, 'hf')) && imagePresent(SANDBOX) && gpuPresent()

const output = (r: RunRecord, name: string) => r.outputs.find((o) => o.name === name)!.hash

async function run(
  client: KernelClient, plugin: string, handler: string, params: Record<string, unknown>, inputs: { name: string; hash: string }[] = [],
): Promise<RunRecord> {
  const started = await client.call('runs.start', { plugin, handler, params, project: 'schemes', inputs })
  for (;;) {
    const r = await client.call('runs.wait', { run: started.id, timeoutMs: 1_800_000 })
    if (r.state === 'finished') {
      expect(r, `${plugin}/${handler}: ${r.error}`).toMatchObject({ execution: 'exited_zero' })
      return r
    }
  }
}

describe.skipIf(!ready)(`Qwen2.5-0.5B-Instruct: the three numeric schemes (${SET} set)`, () => {
  it('builds W8, sensitive-matrix W8 and W4 with finer groups, and measures each against L0', { timeout: 21_600_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    await client.call('projects.create', { id: 'schemes', title: 'Qwen2.5-0.5B-Instruct numeric schemes', flow: 'six' })

    const imported = await run(client, 'model-llama', 'import', { path: MODEL, archive: true })
    const recovered = await run(client, 'model-llama', 'recover', {}, [{ name: 'census', hash: output(imported, 'census') }])
    const data = await run(client, 'numeric', 'benchmark_data', { path: TOOLS })
    const calibration = await run(client, 'numeric', 'calibration_data', { path: TOOLS })
    const model = output(imported, 'model')
    const graph = output(recovered, 'graph')

    // One L0 report decides every scheme (L0 does not depend on the numeric spec).
    const l0 = await run(client, 'numeric', 'benchmark', { layer: 'L0', set: SET, batchSize: 32 }, [
      { name: 'data', hash: output(data, 'data') },
      { name: 'model', hash: model },
    ])

    const results: Record<string, unknown> = {}
    for (const scheme of SCHEMES) {
      const calibrated = await run(client, 'numeric', 'calibrate_graph', scheme.params, [
        { name: 'graph', hash: graph },
        { name: 'model', hash: model },
        { name: 'calibration', hash: output(calibration, 'calibration') },
      ])
      const spec = output(calibrated, 'spec')
      const weights = output(calibrated, 'weights')
      const l2 = await run(client, 'numeric', 'benchmark', { layer: 'L2', set: SET, batchSize: 32 }, [
        { name: 'data', hash: output(data, 'data') },
        { name: 'model', hash: model },
        { name: 'graph', hash: graph },
        { name: 'spec', hash: spec },
        { name: 'weights', hash: weights },
      ])
      const comparison = await run(client, 'numeric', 'benchmark_compare', {}, [
        { name: 'l0', hash: output(l0, 'report') },
        { name: 'l2', hash: output(l2, 'report') },
      ])
      const verdicts: Record<string, string> = {}
      for (const benchmark of BENCHMARKS) {
        const id = `spec.accuracy.${SET === 'full' ? '' : 'quick.'}${benchmark}`
        verdicts[benchmark] = (await client.call('verdicts.evaluate', { check: id, run: comparison.id, project: 'schemes' })).verdict
      }
      const metrics = (await client.call('runs.get', { run: comparison.id })).metrics
      results[scheme.id] = { calibration: calibrated.metrics, verdicts, metrics }
      console.log(scheme.id, JSON.stringify({ calibration: calibrated.metrics, verdicts, metrics }))
    }

    writeFileSync(join(tmpdir(), `llmchip-qwen-schemes-${SET}.json`), JSON.stringify(results, null, 1))
    expect(Object.keys(results)).toEqual(SCHEMES.map((s) => s.id))
    for (const id of Object.keys(results)) expect((results[id] as any).verdicts).toBeTruthy()
  })
})
