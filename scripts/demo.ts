// Drives a demo project through the default flow against a running harnessd,
// with the bundled plugins and the proxy of the reference model, so that the
// Web UI's flow page has real results for every stage; each stage's checks judge
// it (there are no approval gates). Run with `yarn demo [--project ID] [--until S0..S7] [--arch spatial]`.
// `--arch spatial` designs the spatial engine (taalas-spatial/1: the model as circuits, the
// KV cache on chip, die 5 x 3 mm) instead of the vector engine.
//
// The sequence is the positive path of tests/m4-flow.spec.ts. S5 onwards needs
// the pinned EDA image; S6-S7 run the physical tools for about half an hour.
//
// With `--model PATH`, it instead starts a project from a downloaded model on this
// machine, as the Web UI's "Start from model weights" does: S0 (import, tensor
// census, estimate) and S1 (graph recovery, L0, comparison with the official
// implementation) and S2 as analysis (the recovered graph quantized to int8, its L2
// against L0). `--bits` and `--process` choose the estimate's assumptions.

import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { parseArgs } from 'node:util'
import { KernelClient } from '@llmchip/client'
import type { RunRecord } from '@llmchip/kernel-api'
import { projectIdFor, recoverGraph, runScheme, startFromWeights, type ProcessId } from '../apps/web/src/views/modelPipeline.ts'

const STAGES = ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7']
const PROXY = { dim: 16, hiddenDim: 32, layers: 2, heads: 2, kvHeads: 1, seqLen: 64, seed: 1 }
const PRERUN = { die: { xMm: 1.2, yMm: 2 }, clockMHz: 8, parallelMacs: 2, memoryInterface: 'sqi', operandBuffers: 'none', placementDensityPct: 70 }
const PRERUN_SPATIAL = { architecture: 'taalas-spatial/1', die: { xMm: 5, yMm: 3 }, clockMHz: 8, dividers: 16, cyclesPerToken: 8000, placementDensityPct: 70, synthHierarchy: 'deferred_flatten' }

const { values } = parseArgs({
  options: {
    project: { type: 'string' },
    until: { type: 'string', default: 'S7' },
    home: { type: 'string' },
    model: { type: 'string' },
    bits: { type: 'string', default: '4' },
    process: { type: 'string', default: 'gf180mcu' },
    arch: { type: 'string', default: 'hardwired' },
  },
})
const spatial = values.arch === 'spatial'
const project = values.project ?? (values.model ? projectIdFor(values.model) : 'demo')
const until = STAGES.indexOf(values.until!)
if (until < 0) throw new Error(`--until must be one of ${STAGES.join(', ')}`)

const home = resolve(values.home ?? process.env.LLMCHIP_HOME ?? join(homedir(), '.llmchip'))
const runtimePath = join(home, 'run', 'harnessd.json')
if (!existsSync(runtimePath)) throw new Error(`harnessd is not running for ${home}; start it with \`yarn harnessd serve\``)
const client = await KernelClient.connect(JSON.parse(readFileSync(runtimePath, 'utf8')))

const started = Date.now()
const log = (text: string) => console.log(`[${((Date.now() - started) / 1000).toFixed(0).padStart(5)} s] ${text}`)

if (values.model) {
  const report = (step: string, state: string, detail?: string) => state !== 'running' && log(`  ${step}: ${state}${detail ? ` (${detail})` : ''}`)
  try {
    log(`S0 ${values.model} -> project ${project}`)
    const title = values.model.replace(/[\\/]+$/, '').split(/[\\/]/).pop()!
    const feasibility = await startFromWeights(client, { path: values.model, project, title, bits: Number(values.bits), process: values.process as ProcessId }, report)
    log(`S0 feasibility: ${feasibility}`)
    log('S1 graph recovery')
    await recoverGraph(client, project, report)
    log('S2 numeric analysis: int8, groups of 32')
    await runScheme(client, project, 'graph', { weightFormat: 'int8', groupSize: 32, rounding: 'half-up', overflow: 'saturate' }, { maxPrompts: 4, steps: 8, passageTokens: 128 }, report)
    log('done')
  } finally {
    await client.close()
  }
  process.exit(0)
}

async function run(plugin: string, handler: string, inputs: Record<string, string>, params: Record<string, unknown> = {}): Promise<RunRecord> {
  const queued = await client.call('runs.start', { plugin, handler, params, project, inputs: Object.entries(inputs).map(([name, hash]) => ({ name, hash })) })
  for (;;) {
    const r = await client.call('runs.wait', { run: queued.id, timeoutMs: 600_000 })
    if (r.state !== 'finished') continue
    if (r.execution !== 'exited_zero') throw new Error(`${plugin}/${handler} ended ${r.execution}: ${r.error}`)
    log(`  ${plugin}/${handler} finished`)
    return r
  }
}
const out = (r: RunRecord, name: string) => r.outputs.find((o) => o.name === name)!.hash
const bind = (slot: string, hash: string) => client.call('projects.bind', { project, slot, hash })
const enter = (stage: string) => (log(`${stage}`), client.call('stages.enter', { project, stage }))
async function judge(check: string, r: RunRecord): Promise<void> {
  const verdict = await client.call('verdicts.evaluate', { check, run: r.id, project })
  log(`  check ${check}: ${verdict.verdict}`)
  if (verdict.verdict !== 'pass') throw new Error(`${check} is ${verdict.verdict}: ${JSON.stringify(verdict.reasons)}`)
}
/** Whether the demo goes on after `stage` (`--until`). */
const goOn = (stage: string) => STAGES.indexOf(stage) < until
const data = async (id: string) => (await client.call('plugins.list')).plugins.flatMap((p) => p.data).find((d) => d.id === id)!.hash
const upload = async (content: string, type: string, mediaType: string) => (await client.upload(content, { type, mediaType })).hash

try {
  await client.call('projects.create', { id: project, title: 'Demo: proxy of the reference model on GF180MCU' })

  // S0: feasibility estimate of the bound request.
  log('S0')
  const profile = await data('gf180mcu')
  const library = await data('library-180nm')
  const model = { params: 5248, dim: PROXY.dim, layers: PROXY.layers, heads: PROXY.heads, kvHeads: PROXY.kvHeads, vocab: 35, hiddenDim: PROXY.hiddenDim, title: 'proxy' }
  const request = await upload(
    JSON.stringify({ model, weightBits: 2, activationLocation: 'offchip', contextLength: 64, weightStorage: 'gf180mcu.stdcell-rom', parallelMacs: PRERUN.parallelMacs, clockMHz: PRERUN.clockMHz, targetAreaMm2: 2.4 }),
    'estimate.request',
    'application/json',
  )
  await bind('charter', await upload('# CHARTER\nGoal: demonstrate S0-S7 on GF180MCU with the proxy of the reference model.\n', 'project.charter', 'text/markdown'))
  await bind('estimate-request', request)
  await bind('process-profile', profile)
  await bind('impl-library', library)
  await judge('estimate.feasibility', await run('estimator', 'estimate', { request, profile, library }))
  if (!goOn('S0')) process.exit(0)

  // S1: the proxy model, its intake into the Model IR, and the L0 reference.
  await enter('S1')
  const proxy = await run('model-llama', 'synthesize', {}, PROXY)
  const tokenizer = out(proxy, 'tokenizer')
  const ir = out(await run('model-llama', 'intake', { weights: out(proxy, 'weights'), tokenizer }, { license: 'proxy (untrained)' }), 'ir')
  const evalset = await data('evalset-proxy')
  await run('model-llama', 'reference', { ir, tokenizer, evalset })
  if (!goOn('S1')) process.exit(0)

  // S2: ternary numeric spec, L2 weights and their accuracy against L0.
  await enter('S2')
  const q = await run('numeric', 'quantize', { ir, tokenizer, evalset }, { weightFormat: 'ternary', groupSize: 16, contextLength: 64 })
  const spec = out(q, 'spec')
  const weights = out(q, 'weights')
  for (const [slot, hash] of [['model-ir', ir], ['tokenizer', tokenizer], ['eval-set', evalset], ['numeric-spec', spec], ['l2-weights', weights]] as const) await bind(slot, hash)
  await judge('accuracy.budget', await run('numeric', 'evaluate', { ir, tokenizer, evalset, spec, weights }))
  if (!goOn('S2')) process.exit(0)

  // S3: chip spec on the 1.2 x 2 mm prerun die.
  await enter('S3')
  const l2 = await run('numeric', 'infer', { spec, weights, tokenizer, evalset })
  const { placementDensityPct: _, synthHierarchy: __, ...spatialParams } = PRERUN_SPATIAL
  const design = await run('arch-taalas', 'design', { spec, weights, profile, library }, spatial
    ? { weightStorage: 'gf180mcu.stdcell-rom', ...spatialParams }
    : { weightStorage: 'gf180mcu.stdcell-rom', parallelMacs: PRERUN.parallelMacs, clockMHz: PRERUN.clockMHz, die: PRERUN.die, targetAreaMm2: 2.4, memoryInterface: PRERUN.memoryInterface, operandBuffers: PRERUN.operandBuffers })
  const chip = out(design, 'chip')
  await bind('chip-spec', chip)
  await bind('ppa-estimate', out(design, 'ppa'))
  await judge('arch.spec', await run('arch-taalas', 'verify_spec', { chip, spec, ppa: out(design, 'ppa'), weights }))
  if (!goOn('S3')) process.exit(0)

  // S4: storage calibration admits the stage; RTL, its ISA check and lint.
  const calibration = out(await run('eda-gf180', 'calibrate', {}, { implementation: 'gf180mcu.stdcell-rom', sizes: [[256, 16]] }), 'report')
  await judge('impl.views.S4', await run('estimator', 'check_views', { calibration }))
  await bind('storage-impl', calibration)
  await enter('S4')
  const rtl = out(await run('rtl-taalas', 'generate', { spec, weights, chip }), 'rtl')
  await run('rtl-taalas', 'check_isa', { rtl, spec, weights, tokenizer, evalset })
  await judge('rtl.lint', await run('sim-verilator', 'lint', { rtl }))
  if (!goOn('S4')) process.exit(0)

  // S5: the whole verification plan, RTL against L2.
  await enter('S5')
  const trace = out(l2, 'trace')
  const plan = await data(spatial ? 'verification-plan-spatial' : 'verification-plan-default')
  await bind('rtl', rtl)
  await bind('l2-trace', trace)
  await bind('verification-plan', plan)
  await judge('sim-verilator.qualify.rtl-functional-sim', await run('sim-verilator', 'qualify', {}))
  await judge('rtl.equivalence', await run('sim-verilator', 'verify', { rtl, spec, trace, plan }))
  if (!goOn('S5')) process.exit(0)

  // S6: constraints and the physical implementation with the pad ring.
  await judge('impl.views.S6', await run('estimator', 'check_views', { calibration }))
  await enter('S6')
  const sdc = out(await run('eda-gf180', 'constraints', { chip }), 'constraints')
  const registry = await upload(JSON.stringify({ format: 'llmchip-timing-exceptions/1', exceptions: [] }), 'timing.exceptions', 'application/json')
  const impl = await run('eda-gf180', 'implement', { rtl, chip, constraints: sdc }, { placementDensityPct: (spatial ? PRERUN_SPATIAL : PRERUN).placementDensityPct, ...(spatial ? { synthHierarchy: PRERUN_SPATIAL.synthHierarchy } : {}) })
  const layout = out(impl, 'layout')
  const gds = out(impl, 'gds')
  if (!goOn('S6')) process.exit(0)

  // S7: signoff, each check on exactly what it judges.
  await judge('impl.views.S7', await run('estimator', 'check_views', { calibration }))
  await enter('S7')
  const rules = await data('signoff-gf180mcu')
  for (const [slot, hash] of [['constraints', sdc], ['timing-exceptions', registry], ['signoff-rules', rules], ['layout', layout], ['gds', gds]] as const) await bind(slot, hash)
  const drc = await run('eda-gf180', 'drc', { gds, layout, rules })
  await judge('phy.drc', drc)
  const lvs = await run('eda-gf180', 'lvs', { gds, layout })
  await judge('phy.lvs', lvs)
  const sta = await run('eda-gf180', 'sta', { layout, constraints: sdc, exceptions: registry, chip })
  await judge('phy.sta', sta)
  await judge('phy.gatesim', await run('sim-verilator', 'gatesim', { layout, rtl, spec, trace }, { prompts: 2, steps: 8 }))
} finally {
  await client.close()
}
