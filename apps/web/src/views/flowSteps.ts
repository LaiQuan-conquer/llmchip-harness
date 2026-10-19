// What the flow page shows for each stage of the default flow: its purpose,
// the runs that do its work, the checks that judge it, the key results read
// from run metrics, and the deliverables. Presentation metadata only; the
// state itself comes from the kernel (projects.get, runs.list, verdicts.list).

import type { RunRecord } from '@llmchip/kernel-api'
import type { Key } from '../i18n.ts'

export type Metrics = RunRecord['metrics']

/** Number and text formatting in the current language. */
export interface Format {
  t(key: Key, params?: Record<string, unknown>): string
  int(value: unknown): string
  num(value: unknown, digits?: number): string
  yes(value: unknown): string
}

export interface Fact {
  label: Key
  /** `plugin/handler` whose latest successful run supplies the metrics. */
  run: string
  show(m: Metrics, f: Format): string | undefined
  /** true: good, false: needs attention, undefined: neutral. */
  good?(m: Metrics): boolean | undefined
}

export interface Deliverable {
  label: Key
  run: string
  output: string
}

export interface StepDef {
  stage: string
  title: Key
  /** The research step of the weights-to-silicon method this stage carries out. */
  research: Key | null
  what: Key
  /** Gate decided after this stage. */
  gate?: string
  checks: string[]
  runs: string[]
  facts: Fact[]
  deliverables: Deliverable[]
  implemented: boolean
}

const has = (m: Metrics, key: string) => m[key] !== undefined
const zero = (m: Metrics, ...keys: string[]) => (keys.every((k) => has(m, k)) ? keys.every((k) => m[k] === 0) : undefined)

export const STEPS: StepDef[] = [
  {
    stage: 'S0',
    title: 'flow.s0.title',
    research: 'flow.rd.census',
    what: 'flow.s0.what',
    gate: 'G0',
    checks: ['model.census', 'estimate.feasibility'],
    runs: ['model-llama/import', 'estimator/estimate'],
    facts: [
      {
        label: 'flow.fact.imported',
        run: 'model-llama/import',
        show: (m, f) => (has(m, 'parameters') ? f.t('flow.fmt.imported', { type: m.model_type, params: f.int(m.parameters), tensors: f.int(m.tensors) }) : undefined),
      },
      { label: 'flow.fact.feasibility', run: 'estimator/estimate', show: (m, f) => (has(m, 'feasibility') ? f.t(`feasibility.${m.feasibility}` as Key) : undefined), good: (m) => m.feasible_or_conditional as boolean },
      { label: 'flow.fact.dieArea', run: 'estimator/estimate', show: (m, f) => (has(m, 'die_area_mm2.typ') ? `${f.num(m['die_area_mm2.typ'], 2)} mm²` : undefined) },
      { label: 'flow.fact.speed', run: 'estimator/estimate', show: (m, f) => (has(m, 'tokens_per_second') ? `${f.num(m.tokens_per_second, 0)} tok/s` : undefined) },
      { label: 'flow.fact.missing', run: 'estimator/estimate', show: (m, f) => (has(m, 'missing_count') ? f.int(m.missing_count) : undefined), good: (m) => zero(m, 'missing_count') },
    ],
    deliverables: [
      { label: 'flow.out.census', run: 'model-llama/import', output: 'census' },
      { label: 'flow.out.estimate', run: 'estimator/estimate', output: 'summary' },
    ],
    implemented: true,
  },
  {
    stage: 'S1',
    title: 'flow.s1.title',
    research: 'flow.rd.graph',
    what: 'flow.s1.what',
    checks: ['model.graph', 'model.upstream'],
    runs: ['model-llama/synthesize', 'model-llama/intake', 'model-llama/reference', 'model-llama/recover', 'model-llama/run_graph', 'model-llama/compare'],
    facts: [
      {
        label: 'flow.fact.graph',
        run: 'model-llama/recover',
        show: (m, f) =>
          m.family_supported === true
            ? f.t('flow.fmt.graph', { family: m.family, layers: f.int(m.layers), bound: f.int(m.tensors_bound), ignored: f.int(m.tensors_ignored) })
            : m.family_supported === false
              ? f.t('flow.fmt.graphUnsupported')
              : undefined,
        good: (m) => (m.family_supported === undefined ? undefined : m.family_supported === true && m.tensors_unmatched === 0 && m.tensors_missing === 0),
      },
      {
        label: 'flow.fact.upstream',
        run: 'model-llama/compare',
        show: (m, f) => (has(m, 'greedy_match') ? f.t(m.greedy_match ? 'flow.fmt.upstreamOk' : 'flow.fmt.upstreamBad', { rel: Number(m.max_rel_logit_diff).toExponential(1) }) : undefined),
        good: (m) => (has(m, 'greedy_match') ? m.greedy_match === true : undefined),
      },
      { label: 'flow.fact.parameters', run: 'model-llama/intake', show: (m, f) => (has(m, 'parameters') ? f.int(m.parameters) : undefined) },
      {
        label: 'flow.fact.structure',
        run: 'model-llama/intake',
        show: (m, f) => (has(m, 'layers') ? f.t('flow.fmt.structure', { layers: m.layers, dim: m.dim, vocab: m.vocab }) : undefined),
      },
      { label: 'flow.fact.l0Perplexity', run: 'model-llama/reference', show: (m, f) => (has(m, 'perplexity') ? f.num(m.perplexity, 1) : undefined) },
      { label: 'flow.fact.prompts', run: 'model-llama/reference', show: (m, f) => (has(m, 'prompts') ? f.int(m.prompts) : undefined) },
    ],
    deliverables: [
      { label: 'flow.out.graph', run: 'model-llama/recover', output: 'graph' },
      { label: 'flow.out.l0', run: 'model-llama/run_graph', output: 'outputs' },
      { label: 'flow.out.upstream', run: 'model-llama/compare', output: 'report' },
      { label: 'flow.out.structure', run: 'model-llama/intake', output: 'structure' },
      { label: 'flow.out.l0Port', run: 'model-llama/reference', output: 'outputs' },
    ],
    implemented: true,
  },
  {
    stage: 'S2',
    title: 'flow.s2.title',
    research: 'flow.rd.quant',
    what: 'flow.s2.what',
    gate: 'G1',
    checks: ['accuracy.budget'],
    runs: ['numeric/quantize', 'numeric/evaluate', 'numeric/infer'],
    facts: [
      { label: 'flow.fact.weightBits', run: 'numeric/quantize', show: (m, f) => (has(m, 'storage_bits.weights') ? `${f.int(m['storage_bits.weights'])} bit` : undefined) },
      { label: 'flow.fact.accumulator', run: 'numeric/quantize', show: (m, f) => (has(m, 'accumulator_bits') ? `${f.int(m.accumulator_bits)} bit` : undefined) },
      {
        label: 'flow.fact.perplexity',
        run: 'numeric/evaluate',
        show: (m, f) => (has(m, 'l2_perplexity') ? `${f.num(m.l0_perplexity, 1)} → ${f.num(m.l2_perplexity, 1)} (${Number(m.ppl_increase_pct) > 0 ? '+' : ''}${f.num(m.ppl_increase_pct, 1)}%)` : undefined),
      },
      { label: 'flow.fact.saturations', run: 'numeric/evaluate', show: (m, f) => (has(m, 'saturations') ? f.int(m.saturations) : undefined), good: (m) => zero(m, 'saturations') },
    ],
    deliverables: [
      { label: 'flow.out.numericSpec', run: 'numeric/quantize', output: 'spec' },
      { label: 'flow.out.accuracy', run: 'numeric/evaluate', output: 'report' },
      { label: 'flow.out.l2', run: 'numeric/infer', output: 'outputs' },
    ],
    implemented: true,
  },
  {
    stage: 'S3',
    title: 'flow.s3.title',
    research: 'flow.rd.dataflow',
    what: 'flow.s3.what',
    gate: 'G2',
    checks: ['arch.spec'],
    runs: ['arch-taalas/design', 'arch-taalas/verify_spec'],
    facts: [
      {
        label: 'flow.fact.areaVsLimit',
        run: 'arch-taalas/verify_spec',
        show: (m, f) => (has(m, 'die_area_mm2.typ') ? `${f.num(m['die_area_mm2.typ'], 2)} / ${f.num(m.area_limit_mm2, 2)} mm²` : undefined),
        good: (m) => m.fits_area as boolean | undefined,
      },
      { label: 'flow.fact.speed', run: 'arch-taalas/design', show: (m, f) => (has(m, 'tokens_per_second') ? `${f.num(m.tokens_per_second, 1)} tok/s` : undefined) },
      { label: 'flow.fact.storedBits', run: 'arch-taalas/design', show: (m, f) => (has(m, 'stored_bits') ? `${f.int(m.stored_bits)} bit` : undefined) },
      { label: 'flow.fact.specMatches', run: 'arch-taalas/verify_spec', show: (m, f) => (has(m, 'numeric_spec_matches') ? f.yes(m.numeric_spec_matches) : undefined), good: (m) => m.numeric_spec_matches as boolean | undefined },
    ],
    deliverables: [
      { label: 'flow.out.chipSpec', run: 'arch-taalas/design', output: 'chip' },
      { label: 'flow.out.ppa', run: 'arch-taalas/design', output: 'ppa' },
    ],
    implemented: true,
  },
  {
    stage: 'S4',
    title: 'flow.s4.title',
    research: 'flow.rd.rtl',
    what: 'flow.s4.what',
    checks: ['impl.views.S4', 'rtl.lint'],
    runs: ['eda-gf180/calibrate', 'estimator/check_views', 'rtl-taalas/generate', 'rtl-taalas/check_isa', 'sim-verilator/lint'],
    facts: [
      { label: 'flow.fact.programOps', run: 'rtl-taalas/generate', show: (m, f) => (has(m, 'program_ops') ? f.int(m.program_ops) : undefined) },
      { label: 'flow.fact.romWords', run: 'rtl-taalas/generate', show: (m, f) => (has(m, 'weight_rom_words') ? f.int(m.weight_rom_words) : undefined) },
      {
        label: 'flow.fact.isa',
        run: 'rtl-taalas/check_isa',
        show: (m, f) => (has(m, 'values') ? f.t('flow.fmt.compared', { values: f.int(m.values), mismatches: f.int(m.mismatches) }) : undefined),
        good: (m) => zero(m, 'mismatches'),
      },
      {
        label: 'flow.fact.lint',
        run: 'sim-verilator/lint',
        show: (m, f) => (has(m, 'lint_errors') ? f.t('flow.fmt.lint', { errors: f.int(m.lint_errors), warnings: f.int(m.lint_warnings) }) : undefined),
        good: (m) => zero(m, 'lint_errors', 'lint_warnings'),
      },
    ],
    deliverables: [
      { label: 'flow.out.rtl', run: 'rtl-taalas/generate', output: 'rtl' },
      { label: 'flow.out.storageMap', run: 'rtl-taalas/generate', output: 'storage' },
      { label: 'flow.out.isa', run: 'rtl-taalas/check_isa', output: 'report' },
    ],
    implemented: true,
  },
  {
    stage: 'S5',
    title: 'flow.s5.title',
    research: 'flow.rd.bitexact',
    what: 'flow.s5.what',
    gate: 'G3',
    checks: ['sim-verilator.qualify.rtl-functional-sim', 'rtl.equivalence'],
    runs: ['sim-verilator/qualify', 'sim-verilator/verify'],
    facts: [
      {
        label: 'flow.fact.regression',
        run: 'sim-verilator/verify',
        show: (m, f) => (has(m, 'regression_prompts') ? f.t('flow.fmt.regression', { prompts: f.int(m.regression_prompts), tokens: f.int(m.tokens_per_prompt) }) : undefined),
      },
      {
        label: 'flow.fact.mismatches',
        run: 'sim-verilator/verify',
        show: (m, f) => (has(m, 'regression_mismatches') ? f.int(Number(m.regression_mismatches) + Number(m.random_mismatches ?? 0)) : undefined),
        good: (m) => zero(m, 'regression_mismatches', 'random_mismatches'),
      },
      { label: 'flow.fact.itemsFailed', run: 'sim-verilator/verify', show: (m, f) => (has(m, 'items_failed') ? f.int(m.items_failed) : undefined), good: (m) => zero(m, 'items_failed') },
      {
        label: 'flow.fact.coverage',
        run: 'sim-verilator/verify',
        show: (m, f) => (has(m, 'coverage_line_pct') ? `${f.num(m.coverage_line_pct, 1)}%` : undefined),
        good: (m) => (has(m, 'coverage_line_pct') ? Number(m.coverage_line_pct) >= 90 : undefined),
      },
    ],
    deliverables: [
      { label: 'flow.out.verification', run: 'sim-verilator/verify', output: 'report' },
      { label: 'flow.out.waveform', run: 'sim-verilator/verify', output: 'waveform' },
    ],
    implemented: true,
  },
  {
    stage: 'S6',
    title: 'flow.s6.title',
    research: null,
    what: 'flow.s6.what',
    checks: ['impl.views.S6'],
    runs: ['eda-gf180/constraints', 'eda-gf180/implement'],
    facts: [
      { label: 'flow.fact.dieArea', run: 'eda-gf180/implement', show: (m, f) => (has(m, 'die_area_mm2') ? `${f.num(m.die_area_mm2, 2)} mm²` : undefined) },
      { label: 'flow.fact.coreArea', run: 'eda-gf180/implement', show: (m, f) => (has(m, 'core_area_um2') ? `${f.num(Number(m.core_area_um2) / 1e6, 2)} mm²` : undefined) },
      {
        label: 'flow.fact.flowTiming',
        run: 'eda-gf180/implement',
        show: (m, f) => (has(m, 'flow_setup_violations') ? f.t('flow.fmt.violations', { setup: f.int(m.flow_setup_violations), hold: f.int(m.flow_hold_violations) }) : undefined),
        good: (m) => zero(m, 'flow_setup_violations', 'flow_hold_violations'),
      },
      { label: 'flow.fact.routingDrc', run: 'eda-gf180/implement', show: (m, f) => (has(m, 'routing_drc_errors') ? f.int(m.routing_drc_errors) : undefined), good: (m) => zero(m, 'routing_drc_errors') },
    ],
    deliverables: [
      { label: 'flow.out.render', run: 'eda-gf180/implement', output: 'render' },
      { label: 'flow.out.gds', run: 'eda-gf180/implement', output: 'gds' },
      { label: 'flow.out.constraints', run: 'eda-gf180/constraints', output: 'constraints' },
    ],
    implemented: true,
  },
  {
    stage: 'S7',
    title: 'flow.s7.title',
    research: null,
    what: 'flow.s7.what',
    checks: ['impl.views.S7', 'phy.drc', 'phy.lvs', 'phy.sta', 'phy.gatesim'],
    runs: ['eda-gf180/drc', 'eda-gf180/lvs', 'eda-gf180/sta', 'sim-verilator/gatesim'],
    facts: [
      {
        label: 'flow.fact.drc',
        run: 'eda-gf180/drc',
        show: (m, f) =>
          has(m, 'klayout_drc_errors') ? f.int(['klayout_drc_errors', 'magic_drc_errors', 'antenna_errors', 'density_errors'].reduce((n, k) => n + Number(m[k] ?? 0), 0)) : undefined,
        good: (m) => zero(m, 'klayout_drc_errors', 'magic_drc_errors', 'antenna_errors', 'density_errors'),
      },
      { label: 'flow.fact.lvs', run: 'eda-gf180/lvs', show: (m, f) => (has(m, 'lvs_error') ? f.t(m.lvs_error === 0 ? 'flow.fmt.lvsMatch' : 'flow.fmt.lvsMismatch') : undefined), good: (m) => zero(m, 'lvs_error') },
      {
        label: 'flow.fact.sta',
        run: 'eda-gf180/sta',
        show: (m, f) => (has(m, 'corners_checked') ? f.t('flow.fmt.sta', { corners: f.int(m.corners_checked), setup: f.int(m.setup_violations), hold: f.int(m.hold_violations) }) : undefined),
        good: (m) => zero(m, 'setup_violations', 'hold_violations'),
      },
      { label: 'flow.fact.gatesim', run: 'sim-verilator/gatesim', show: (m, f) => (has(m, 'gl_mismatches') ? f.int(m.gl_mismatches) : undefined), good: (m) => zero(m, 'gl_mismatches') },
    ],
    deliverables: [
      { label: 'flow.out.drc', run: 'eda-gf180/drc', output: 'report' },
      { label: 'flow.out.lvs', run: 'eda-gf180/lvs', output: 'report' },
      { label: 'flow.out.sta', run: 'eda-gf180/sta', output: 'report' },
    ],
    implemented: true,
  },
  {
    stage: 'S8',
    title: 'flow.s8.title',
    research: null,
    what: 'flow.s8.what',
    gate: 'G4',
    checks: ['package.integrity'],
    runs: ['packager/build', 'packager/check'],
    facts: [
      { label: 'flow.fact.missingFiles', run: 'packager/check', show: (m, f) => (has(m, 'missing_files') ? f.int(m.missing_files) : undefined), good: (m) => zero(m, 'missing_files') },
      { label: 'flow.fact.hashMismatches', run: 'packager/check', show: (m, f) => (has(m, 'hash_mismatches') ? f.int(m.hash_mismatches) : undefined), good: (m) => zero(m, 'hash_mismatches') },
      { label: 'flow.fact.gdsMatches', run: 'packager/check', show: (m, f) => (has(m, 'gds_matches_snapshot') ? f.yes(m.gds_matches_snapshot) : undefined), good: (m) => m.gds_matches_snapshot as boolean | undefined },
    ],
    deliverables: [
      { label: 'flow.out.package', run: 'packager/build', output: 'package' },
      { label: 'flow.out.manifest', run: 'packager/build', output: 'manifest' },
    ],
    implemented: true,
  },
  {
    stage: 'S9',
    title: 'flow.s9.title',
    research: null,
    what: 'flow.s9.what',
    checks: [],
    runs: [],
    facts: [],
    deliverables: [],
    implemented: false,
  },
]

/** Plain-language names of checks and run handlers. */
export const CHECK_TITLES: Record<string, Key> = {
  'model.census': 'flow.check.census',
  'model.graph': 'flow.check.graph',
  'model.upstream': 'flow.check.upstream',
  'estimate.feasibility': 'flow.check.feasibility',
  'accuracy.budget': 'flow.check.accuracy',
  'arch.spec': 'flow.check.arch',
  'impl.views.S4': 'flow.check.views',
  'impl.views.S6': 'flow.check.views',
  'impl.views.S7': 'flow.check.views',
  'rtl.lint': 'flow.check.lint',
  'sim-verilator.qualify.rtl-functional-sim': 'flow.check.qualify',
  'rtl.equivalence': 'flow.check.equivalence',
  'phy.drc': 'flow.check.drc',
  'phy.lvs': 'flow.check.lvs',
  'phy.sta': 'flow.check.sta',
  'phy.gatesim': 'flow.check.gatesim',
  'package.integrity': 'flow.check.package',
}

export const RUN_TITLES: Record<string, Key> = {
  'model-llama/import': 'flow.run.import',
  'model-llama/recover': 'flow.run.recover',
  'model-llama/run_graph': 'flow.run.runGraph',
  'model-llama/compare': 'flow.run.compare',
  'estimator/estimate': 'flow.run.estimate',
  'model-llama/synthesize': 'flow.run.synthesize',
  'model-llama/intake': 'flow.run.intake',
  'model-llama/reference': 'flow.run.reference',
  'numeric/quantize': 'flow.run.quantize',
  'numeric/evaluate': 'flow.run.evaluate',
  'numeric/infer': 'flow.run.infer',
  'arch-taalas/design': 'flow.run.design',
  'arch-taalas/verify_spec': 'flow.run.verifySpec',
  'eda-gf180/calibrate': 'flow.run.calibrate',
  'estimator/check_views': 'flow.run.checkViews',
  'rtl-taalas/generate': 'flow.run.generate',
  'rtl-taalas/check_isa': 'flow.run.checkIsa',
  'sim-verilator/lint': 'flow.run.lint',
  'sim-verilator/qualify': 'flow.run.qualify',
  'sim-verilator/verify': 'flow.run.verify',
  'eda-gf180/constraints': 'flow.run.constraints',
  'eda-gf180/implement': 'flow.run.implement',
  'eda-gf180/drc': 'flow.run.drc',
  'eda-gf180/lvs': 'flow.run.lvs',
  'eda-gf180/sta': 'flow.run.sta',
  'sim-verilator/gatesim': 'flow.run.gatesim',
  'packager/build': 'flow.run.build',
  'packager/check': 'flow.run.check',
}
