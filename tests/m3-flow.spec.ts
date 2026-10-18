// M3 acceptance through the kernel with the bundled plugins and the proxy of the
// reference model (owner decision 2026-10-18; the chip-level RTL keeps its data
// in the external SPI SRAM, see rtl-taalas):
// after G2, S4 generates the RTL (lint, compiler check on the ISA simulator),
// S5 executes the whole verification plan in the pinned EDA image with a
// qualified simulator, and G3 is approved on the bound snapshot (AC-03). An
// injected RTL defect fails the check, opens an incident and is localized to
// its operation and cycle (AC-09). The Chip Playground's L3 equals L2.
// Skipped, visibly, where the tool image is not installed.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { flow, out, run, throughG2Proxy } from './flow-helpers.ts'
import { connect, REPO_PLUGINS, startKernel } from './helpers.ts'

const IMAGE = JSON.parse(readFileSync(join(REPO_PLUGINS, 'sim-verilator/llmchip-plugin.json'), 'utf8')).entry.image as string

function available(image: string): boolean {
  try {
    execFileSync('podman', ['image', 'exists', image], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

/** Replaces one file of a USTAR archive (the RTL bundle), keeping every other entry byte for byte. */
function editTar(tar: Buffer, name: string, edit: (text: string) => string): Buffer {
  const parts: Buffer[] = []
  let offset = 0
  let found = false
  while (offset + 512 <= tar.length && tar.subarray(offset, offset + 512).some((b) => b !== 0)) {
    const header = Buffer.from(tar.subarray(offset, offset + 512))
    const entry = header.subarray(0, 100).toString('utf8').replace(/\0.*$/s, '')
    const size = parseInt(header.subarray(124, 136).toString('utf8').replace(/\0.*$/s, '').trim(), 8)
    let data = tar.subarray(offset + 512, offset + 512 + size)
    if (entry === name) {
      found = true
      data = Buffer.from(edit(data.toString('utf8')), 'utf8')
      header.write(data.length.toString(8).padStart(11, '0') + '\0', 124, 12, 'ascii')
      header.fill(0x20, 148, 156)
      const sum = header.reduce((a, b) => a + b, 0)
      header.write(sum.toString(8).padStart(6, '0') + '\0 ', 148, 8, 'ascii')
    }
    parts.push(header, data, Buffer.alloc((512 - (data.length % 512)) % 512))
    offset += 512 + Math.ceil(size / 512) * 512
  }
  if (!found) throw new Error(`${name} is not in the archive`)
  parts.push(Buffer.alloc(1024))
  return Buffer.concat(parts)
}

describe.skipIf(!available(IMAGE))('M3: RTL generation and verification', () => {
  it('verifies the RTL against L2, approves G3, and localizes an injected defect', { timeout: 1_800_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    const { bind, judge, approve, dataHash } = flow(client)
    const { tokenizer, evalset, spec, l2weights, l2, design } = await throughG2Proxy(client)

    // S4 admission: the storage implementation has the views S4 needs (FR-PHY-08).
    const calibration = await run(client, 'eda-gf180', 'calibrate', {}, { implementation: 'gf180mcu.stdcell-rom', sizes: [[256, 16]] })
    const views = await run(client, 'estimator', 'check_views', { calibration: out(calibration, 'report') })
    expect((await judge('impl.views.S4', views)).verdict).toBe('pass')
    await bind('storage-impl', out(calibration, 'report'))
    await client.call('stages.enter', { project: 'chip', stage: 'S4' })

    // S4: RTL from the generator, its mapping round trip, the compiler check and lint.
    const generated = await run(client, 'rtl-taalas', 'generate', { spec, weights: l2weights, chip: out(design, 'chip') })
    expect(generated.metrics).toMatchObject({ mapping_round_trip: true })
    const rtl = out(generated, 'rtl')
    const storage = JSON.parse(await (await client.download(out(generated, 'storage'))).text())
    expect(storage).toMatchObject({ roundTrip: { identical: true } })
    const isa = await run(client, 'rtl-taalas', 'check_isa', { rtl, spec, weights: l2weights, tokenizer, evalset })
    expect(isa.metrics).toMatchObject({ prompts: 20, mismatches: 0 })
    const lint = await run(client, 'sim-verilator', 'lint', { rtl })
    expect((await judge('rtl.lint', lint)).verdict).toBe('pass')

    // S5: the whole verification plan (AC-03).
    await client.call('stages.enter', { project: 'chip', stage: 'S5' })
    const plan = await dataHash('verification-plan-default')
    const trace = out(l2, 'trace')
    await bind('rtl', rtl)
    await bind('l2-trace', trace)
    await bind('verification-plan', plan)
    const verify = await run(client, 'sim-verilator', 'verify', { rtl, spec, trace, plan })
    expect(verify.metrics).toMatchObject({
      items_total: 14,
      items_not_executed: 0,
      items_failed: 0,
      regression_prompts: 20,
      tokens_per_prompt: 64,
      regression_mismatches: 0,
      random_mismatches: 0,
      debug_mismatches: 0,
      'exhaustive.silu_index.failures': 0,
      'exhaustive.rsqrt.failures': 0,
    })
    expect(verify.metrics.coverage_line_pct as number).toBeGreaterThanOrEqual(90)
    const report = JSON.parse(await (await client.download(out(verify, 'report'))).text())
    expect(report.evidence).toMatchObject({ formal: [] }) // FR-VER-07: test evidence only, stated as such
    expect(new Set(report.items.map((i: { category: string }) => i.category))).toEqual(new Set(['regression', 'directed', 'boundary', 'exhaustive', 'random', 'coverage']))

    // Evidence from an unqualified simulator cannot pass (FR-VER-01).
    expect(await judge('rtl.equivalence', verify)).toMatchObject({ verdict: 'indeterminate', reasons: [{ code: 'CAPABILITY_NOT_QUALIFIED' }] })
    const qualify = await run(client, 'sim-verilator', 'qualify', {})
    expect(qualify.metrics).toMatchObject({ 'verilator.mutant_detected': true, 'icarus.mutant_detected': true, qualified: true })
    expect((await judge('sim-verilator.qualify.rtl-functional-sim', qualify)).verdict).toBe('pass')
    expect((await judge('rtl.equivalence', verify)).verdict).toBe('pass')
    await approve('G3')
    expect((await client.call('gates.release', { project: 'chip', gate: 'G3' })).released).toBe(true)

    // AC-09: an injected RTL defect fails, becomes an incident and is localized.
    const tar = Buffer.from(await (await client.download(rtl)).arrayBuffer())
    const faulty = editTar(tar, 'bundle/rtl/llmchip_core.v', (text) => {
      const line = '      S_TR_W: st_data = ra;'
      expect(text).toContain(line)
      // Element 3 of the SiLU gate output at position 2 is off by one.
      return text.replace(line, `      S_SI_W: st_data = post16 + (i == 3 && pos_in == 2);\n${line}`)
    })
    const bad = await client.upload(faulty, { type: 'rtl.bundle', mediaType: 'application/x-tar', label: 'public' })
    const failing = await run(client, 'sim-verilator', 'verify', { rtl: bad.hash, spec, trace, plan })
    expect(failing.metrics).toMatchObject({ divergence_pos: 2, divergence_operation: 'layer 0 silu gate', divergence_element: 3 })
    expect(failing.metrics.regression_mismatches as number).toBeGreaterThan(0)
    expect(failing.metrics.divergence_cycle as number).toBeGreaterThan(0)
    expect(failing.outputs.map((o) => o.name)).toContain('waveform')
    const vcd = await (await client.download(out(failing, 'waveform'))).text()
    expect(vcd).toMatch(/\$enddefinitions/)
    const verdict = await judge('rtl.equivalence', failing)
    expect(verdict.verdict).toBe('fail')
    const { incidents } = await client.call('incidents.list', { kind: 'check_failed' })
    const incident = incidents.find((i) => i.refs.runs?.includes(failing.id))
    expect(incident?.summary).toMatchObject({
      code: 'CHECK_FAILED_DIVERGENCE',
      params: { check: 'rtl.equivalence', verdict: 'fail', divergence_operation: 'layer 0 silu gate', divergence_cycle: failing.metrics.divergence_cycle },
    })
    // The faulty bundle is not part of the approved snapshot, so G3 stays released.
    expect((await client.call('gates.release', { project: 'chip', gate: 'G3' })).released).toBe(true)

    // Chip Playground (FR-PLY-01/02): L3 from the RTL equals L2 token for token.
    const prompts = await client.upload(JSON.stringify({ format: 'llmchip-evalset/1', prompts: ['once upon a time', 'lily wanted'], steps: 24 }), {
      type: 'eval.set',
      mediaType: 'application/json',
      label: 'public',
    })
    const l3 = await run(client, 'sim-verilator', 'generate', { rtl, tokenizer, evalset: prompts.hash })
    const l2small = await run(client, 'numeric', 'infer', { spec, weights: l2weights, tokenizer, evalset: prompts.hash })
    const l3out = JSON.parse(await (await client.download(out(l3, 'outputs'))).text())
    const l2out = JSON.parse(await (await client.download(out(l2small, 'outputs'))).text())
    expect(l3out.generations.map((g: { tokens: number[] }) => g.tokens)).toEqual(l2out.generations.map((g: { tokens: number[] }) => g.tokens))
    expect(l3out.generations[0].text).toMatch(/^once upon a time/)
  })
})
