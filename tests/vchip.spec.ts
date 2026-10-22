// The virtual-chip test harness through the kernel (owner's draft design, L4): the bundled
// plugin `vchip` builds its chips from a project's artifacts, its runs and reports are
// artifacts, and the flow's checks `vchip.l3` and `vchip.conformance` judge them (ADR-010:
// the harness's own policy verdicts are not gate evidence; these verdicts are).
//
// Without the EDA image: the L2 chip against the float reference of the Model IR (needs .venv).
// With the image: the spatial RTL under the pin-level testbench on 4 prompts (the check is then
// indeterminate: the acceptance needs 20), and a cancelled run leaves no simulator behind.
// LLMCHIP_VCHIP_L3=1 adds the acceptance run on all 20 prompts (about 11 minutes): pass.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import type { KernelClient } from '@llmchip/client'
import { flow, out, PROXY, PRERUN_SPATIAL, run } from './flow-helpers.ts'
import { connect, REPO_PLUGINS, startKernel } from './helpers.ts'

const IMAGE = JSON.parse(readFileSync(join(REPO_PLUGINS, 'sim-verilator/llmchip-plugin.json'), 'utf8')).entry.image as string
const FULL = process.env.LLMCHIP_VCHIP_L3 === '1'

function available(image: string): boolean {
  try {
    execFileSync('podman', ['image', 'exists', image], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

function simulators(): string[] {
  // A container removed while it is listed makes podman complain on stderr; that is not a failure.
  const listed = execFileSync('podman', ['ps', '-a', '--filter', 'name=llmchip-vchip', '--format', '{{.Names}}'], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] })
  return listed.split('\n').filter(Boolean)
}

/** Project `chip` with the proxy through S2: tokenizer, evaluation set, Model IR, numeric spec and weights. */
async function proxyProject(client: KernelClient) {
  const { dataHash } = flow(client)
  await client.call('projects.create', { id: 'chip', title: 'Virtual chips of the proxy' })
  const proxy = await run(client, 'model-llama', 'synthesize', {}, PROXY)
  const tokenizer = out(proxy, 'tokenizer')
  const ir = out(await run(client, 'model-llama', 'intake', { weights: out(proxy, 'weights'), tokenizer }, { license: 'proxy (untrained)' }), 'ir')
  const evalset = await dataHash('evalset-proxy')
  const q = await run(client, 'numeric', 'quantize', { ir, tokenizer, evalset }, { weightFormat: 'ternary', groupSize: 16, contextLength: 64 })
  return { tokenizer, evalset, ir, spec: out(q, 'spec'), weights: out(q, 'weights') }
}

describe('virtual chips through the kernel (vchip plugin)', () => {
  it('benches L2 against the float reference, reports without verdicts, and the flow judges', { timeout: 600_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    const { judge } = flow(client)
    const a = await proxyProject(client)

    const bench = await run(client, 'vchip', 'bench', { tokenizer: a.tokenizer, evalset: a.evalset, ir: a.ir, spec: a.spec, weights: a.weights }, { maxTokens: 64 })
    expect(bench.metrics).toMatchObject({ chip_backend: 'numeric', prompts: 20, max_tokens: 64, errors: 0, golden_compared: false, measurement_trusted: false })
    expect(bench.metrics.next_token_agreement as number).toBeGreaterThan(0.5)
    expect(bench.metrics).not.toHaveProperty('tok_s')
    const report = (await (await client.download(out(bench, 'report'))).json()) as Record<string, unknown>
    expect(report).not.toHaveProperty('verdict')
    expect(report).toMatchObject({ format: 'llmchip-vchip-report/1', built: { chips: { golden: 'chip-l2', reference: 'chip-ref' } } })
    const lines = (await (await client.download(out(bench, 'outputs'))).text()).trim().split('\n')
    expect(lines).toHaveLength(20)
    // L2 is not hardware: the L3 acceptance does not apply, so it is indeterminate, never a pass.
    expect(await judge('vchip.l3', bench)).toMatchObject({ verdict: 'indeterminate' })

    const conform = await run(client, 'vchip', 'conform', { tokenizer: a.tokenizer, evalset: a.evalset, spec: a.spec, weights: a.weights })
    expect(conform.metrics).toMatchObject({ chip_backend: 'numeric', prompts: 20, checks_failed: 0, checks_not_run: 0, clock_source: 'none' })
    expect((await judge('vchip.conformance', conform)).verdict).toBe('pass')
    // On fewer prompts than the acceptance's 20 it cannot pass.
    const few = await run(client, 'vchip', 'conform', { tokenizer: a.tokenizer, evalset: a.evalset, spec: a.spec, weights: a.weights }, { maxPrompts: 3 })
    expect(await judge('vchip.conformance', few)).toMatchObject({ verdict: 'indeterminate', reasons: [{ code: 'COVERAGE_INCOMPLETE' }] })

    // A few prompts typed in (the Web UI's virtual-chip page), without an evaluation set.
    const typed = await run(client, 'vchip', 'bench', { tokenizer: a.tokenizer, spec: a.spec, weights: a.weights }, { prompts: ['tom had a'], maxTokens: 8 })
    expect(typed.metrics).toMatchObject({ prompts: 1, max_tokens: 8, errors: 0 })
  })

  describe.skipIf(!available(IMAGE))('the spatial RTL (needs the EDA image)', () => {
    async function rtlProject(client: KernelClient) {
      const { dataHash } = flow(client)
      const a = await proxyProject(client)
      const { placementDensityPct: _, synthHierarchy: __, ...params } = PRERUN_SPATIAL
      const design = await run(client, 'arch-taalas', 'design', { spec: a.spec, weights: a.weights, profile: await dataHash('gf180mcu'), library: await dataHash('library-180nm') }, { weightStorage: 'gf180mcu.stdcell-rom', ...params })
      const chip = out(design, 'chip')
      const rtl = out(await run(client, 'rtl-taalas', 'generate', { spec: a.spec, weights: a.weights, chip }), 'rtl')
      return { ...a, chip, rtl, cycles: design.metrics.cycles_per_token as number }
    }

    it('runs the RTL under the pin-level testbench, bit-exact with L2, judged by vchip.l3', { timeout: 3_600_000 }, async () => {
      const { kernel } = await startKernel(undefined, { bundled: true })
      const client = await connect(kernel)
      const { judge } = flow(client)
      const a = await rtlProject(client)
      const inputs = { tokenizer: a.tokenizer, evalset: a.evalset, ir: a.ir, spec: a.spec, weights: a.weights, rtl: a.rtl, chip: a.chip }
      const bench = await run(client, 'vchip', 'bench', inputs, { maxTokens: 64, ...(FULL ? {} : { maxPrompts: 4 }) })
      expect(bench.metrics).toMatchObject({
        chip_backend: 'rtl', golden_compared: true, bit_exact: true, measurement_trusted: true, cycles_match_design: true,
        design_cycles_per_token: a.cycles, cycles_per_token_min: a.cycles, cycles_per_token_max: a.cycles,
        design_cycles_source: 'chip.spec', clock_source: 'chip.spec', clock_mhz: 8, errors: 0, comparison_errors: 0,
      })
      expect(bench.metrics.tok_s as number).toBeGreaterThan(1000)
      const verdict = await judge('vchip.l3', bench)
      expect(verdict.verdict).toBe(FULL ? 'pass' : 'indeterminate') // the acceptance needs all 20 prompts
      expect(simulators()).toEqual([])
    })

    it('leaves no simulator behind when a run is cancelled', { timeout: 1_200_000 }, async () => {
      const { kernel } = await startKernel(undefined, { bundled: true })
      const client = await connect(kernel)
      const a = await rtlProject(client)
      const inputs = [['tokenizer', a.tokenizer], ['evalset', a.evalset], ['spec', a.spec], ['weights', a.weights], ['rtl', a.rtl]].map(([name, hash]) => ({ name: name!, hash: hash! }))
      const started = await client.call('runs.start', { plugin: 'vchip', handler: 'bench', project: 'chip', inputs, params: { maxTokens: 64 } })
      // Wait until the testbench runs (after its build), then cancel.
      await expect.poll(() => simulators().filter((n) => !n.includes('-build-')).length, { timeout: 600_000, interval: 1000 }).toBeGreaterThan(0)
      await client.call('runs.cancel', { run: started.id })
      expect(await client.call('runs.wait', { run: started.id, timeoutMs: 60_000 })).toMatchObject({ execution: 'cancelled' })
      await expect.poll(() => simulators(), { timeout: 30_000 }).toEqual([])
    })
  })
})
