// Qwen2.5-0.5B-Instruct through the SPEC step of the six-step flow (requirements FR-MDL,
// FR-QNT-03/04/08, owner decisions 2026-10-24): the model and the open benchmarks are imported
// from this machine and bound to the project, then the step's development pipeline runs (import,
// graph, comparison with transformers, numeric spec and L2, the fast L2 against the reference,
// the quick benchmark set on L0 and L2 and their comparison), then its required tests, which
// judge the development runs again. Every SPEC check reaches a verdict; a failure is a result
// (the provisional spec quantizes by rounding only), never a reason to change a check.
//
// Opt-in (about 20 minutes with a GPU): LLMCHIP_QWEN=1 yarn test tests/qwen-spec.spec.ts. Needs the
// model and the benchmark data in the tools directory (../tools, or LLMCHIP_TOOLS; see its
// README), and the code sandbox image; otherwise the test is skipped, visibly.

import { execFileSync } from 'node:child_process'
import { existsSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import type { PipelineRecord, RunRecord } from '@llmchip/kernel-api'
import { PYTHON, connect, startKernel } from './helpers.ts'

const TOOLS = process.env.LLMCHIP_TOOLS ?? resolve(import.meta.dirname, '../../tools')
const MODEL = join(TOOLS, 'models/Qwen2.5-0.5B-Instruct')
const SANDBOX = 'docker.io/library/python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1'

function imagePresent(image: string): boolean {
  try {
    execFileSync('podman', ['image', 'exists', image], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

function gpuPresent(): boolean {
  // The L2 benchmarks would fall back to the CPU without a GPU, where they take hours: skip.
  try {
    execFileSync(PYTHON, ['-c', 'import sys, torch; sys.exit(0 if torch.cuda.is_available() else 1)'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

const ready = !!process.env.LLMCHIP_QWEN && existsSync(join(MODEL, 'model.safetensors')) && existsSync(join(TOOLS, 'hf')) && imagePresent(SANDBOX) && gpuPresent()

async function run(client: KernelClient, plugin: string, handler: string, params: Record<string, unknown>): Promise<RunRecord> {
  const started = await client.call('runs.start', { plugin, handler, params, project: 'qwen' })
  const r = await client.call('runs.wait', { run: started.id, timeoutMs: 1_800_000 })
  expect(r, `${plugin}/${handler}: ${r.error}`).toMatchObject({ state: 'finished', execution: 'exited_zero' })
  return r
}

async function finished(client: KernelClient, id: string, timeoutMs: number): Promise<PipelineRecord> {
  for (const end = Date.now() + timeoutMs; Date.now() < end; await sleep(1000)) {
    const record = await client.call('pipelines.get', { pipeline: id })
    if (record.state !== 'running') return record
  }
  throw new Error(`pipeline ${id} still running after ${timeoutMs} ms`)
}

const output = (r: RunRecord, name: string) => r.outputs.find((o) => o.name === name)!.hash

describe.skipIf(!ready)('Qwen2.5-0.5B-Instruct: the SPEC step', () => {
  it('runs the development pipeline and the required tests; every check reaches a verdict', { timeout: 3_600_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    await client.call('projects.create', { id: 'qwen', title: 'Qwen2.5-0.5B-Instruct', flow: 'six' })
    expect(await client.call('pipelines.plan', { project: 'qwen', stage: 'SPEC', kind: 'develop' })).toMatchObject({ ready: false })

    // What only the owner can give: the model and the benchmarks on this machine.
    const imported = await run(client, 'model-llama', 'import', { path: MODEL, archive: true })
    const data = await run(client, 'numeric', 'benchmark_data', { path: TOOLS })
    expect(data.metrics).toMatchObject({ quick_total: 900, 'questions.mmlu': 14042, 'questions.humaneval': 164 })
    const calibration = await run(client, 'numeric', 'calibration_data', { path: TOOLS })
    await client.call('projects.bind', { project: 'qwen', slot: 'model', hash: output(imported, 'model') })
    await client.call('projects.bind', { project: 'qwen', slot: 'benchmark-data', hash: output(data, 'data') })
    await client.call('projects.bind', { project: 'qwen', slot: 'calibration-data', hash: output(calibration, 'calibration') })

    const develop = await finished(client, (await client.call('pipelines.start', { project: 'qwen', stage: 'SPEC', kind: 'develop' })).id, 3_000_000)
    const verdicts = Object.fromEntries(develop.nodes.flatMap((n) => n.verdicts.map((v) => [v.check, v.verdict])))
    console.log('SPEC development:', develop.nodes.map((n) => `${n.id} ${n.state}`).join(', '), verdicts)
    // Every node ran; the ones that judge reached a verdict.
    for (const n of develop.nodes) expect(['done', 'failed'], `${n.id}: ${JSON.stringify(n.reason)}`).toContain(n.state)
    for (const n of develop.nodes.filter((n) => n.state === 'failed')) expect(n.reason?.code, n.id).toBe('PIPELINE_CHECK_FAILED')
    expect(verdicts).toMatchObject({ 'model.census': 'pass', 'model.graph': 'pass', 'model.upstream': 'pass', 'spec.l2.fast': 'pass' })
    const benchmarks = ['mmlu', 'cmmlu', 'ceval', 'gsm8k', 'ifeval', 'humaneval', 'mbpp']
    // The provisional W4 spec fails six of them. MMLU is indeterminate because the quick set has 5
    // questions per subject: its 0.35-point drop has a 95% interval whose upper bound lies above the
    // 2-point allowance, so the result is honestly undecidable (requirements FR-QNT-08; conflict C8
    // in the implementation guide §7.3). What must not happen is an indeterminate from a metric the
    // handler did not report, or a verdict the accuracy run did not produce.
    const accuracyNode = develop.nodes.find((n) => n.id === 'SPEC/accuracy')!
    for (const b of benchmarks) {
      const verdict = verdicts[`spec.accuracy.quick.${b}`]
      expect(['pass', 'fail', 'indeterminate'], b).toContain(verdict)
      const latest = (await client.call('verdicts.list', { check: `spec.accuracy.quick.${b}`, limit: 1 })).verdicts[0]!
      expect(latest.run, b).toBe(accuracyNode.runs.at(-1))
      const codes = latest.reasons.map((reason) => reason.code)
      expect(codes, b).not.toContain('METRIC_MISSING')
      if (verdict === 'indeterminate') {
        // Only the interval of a small sample may make a result undecidable, and only because its
        // upper bound lies above the allowance: the drop itself stays within it.
        expect(codes, b).toEqual(['COVERAGE_INCOMPLETE'])
        const params = (latest.reasons[0]!.params ?? {}) as Record<string, unknown>
        expect(String(params.metric), b).toBe(`drop_high.${b}`)
        expect(Number(params.actual), b).toBeGreaterThan(Number(params.value))
      }
    }
    // The quick set as a whole must not become undecidable: today six of the seven fail (the split is
    // recorded in the implementation guide §6.3 and moves when the numeric format changes), so at
    // least one benchmark has to reach a definite verdict here.
    const definite = benchmarks.filter((b) => {
      const verdict = verdicts[`spec.accuracy.quick.${b}`]
      return verdict === 'pass' || verdict === 'fail'
    })
    expect(definite.length, JSON.stringify(verdicts)).toBeGreaterThan(0)

    // The required tests judge the development runs again (nothing reruns).
    const tests = await finished(client, (await client.call('pipelines.start', { project: 'qwen', stage: 'SPEC', kind: 'test' })).id, 600_000)
    for (const n of tests.nodes) expect(n.reused, n.id).toBeTruthy()
    expect(tests.summary.nodes).toBe(13)
    expect(tests.summary.pass + tests.summary.fail + tests.summary.indeterminate).toBe(13)
    const accuracy = tests.nodes.find((n) => n.id === 'SPEC/spec.accuracy.quick.mmlu')!
    const comparison = await client.call('runs.get', { run: accuracy.reused! })
    // The numbers, for the implementation guide (vitest does not show the console of a passing test).
    const out = join(tmpdir(), 'llmchip-qwen-spec.json')
    writeFileSync(out, JSON.stringify({ develop: develop.nodes.map((n) => ({ id: n.id, state: n.state, verdicts: n.verdicts })), tests: tests.summary, quick: comparison.metrics }, null, 1))
  })
})
