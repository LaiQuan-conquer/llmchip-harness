// Readable names and one-line explanations of artifact types, so that no page
// shows a bare type id such as `model.structure` without saying what it is.

import { CATALOGS, type Key } from '../i18n.ts'

export const ARTIFACT_TYPES = [
  'estimate.request', 'estimate.report', 'estimate.summary', 'process.profile', 'implementation.library', 'project.charter', 'flow.pack', 'eval.set',
  'model.file', 'model.bundle', 'model.census', 'model.graph', 'l0g.outputs', 'upstream.report',
  'model.weights.llama2c', 'model.tokenizer.llama2c', 'model.ir', 'model.structure', 'l0.outputs',
  'numeric.spec', 'numeric.weights', 'accuracy.report', 'l2.outputs', 'l2.trace', 'chip.spec', 'ppa.estimate', 'calibration.report',
  'rtl.bundle', 'storage.map', 'isa.check', 'lint.report', 'qualification.report', 'verification.plan', 'verification.report', 'waveform.vcd', 'l3.outputs',
  'vchip.report', 'vchip.outputs', 'vchip.conformance',
  'timing.constraints', 'timing.exceptions', 'layout.bundle', 'layout.gds', 'layout.image', 'measurement.record', 'pad.list', 'timing.sdf',
  'drc.report', 'lvs.report', 'sta.report', 'gatesim.report', 'sdfsim.report', 'signoff.rules', 
  'migration.report', 'run.log', 'agent.handoff', 'changeset.file',
] as const

const known = (key: string): key is Key => key in CATALOGS.zh

/** "模型结构报告" for `model.structure`; the type itself for unknown types. */
export function typeName(t: (k: Key, p?: Record<string, unknown>) => string, type: string): string {
  const key = `art.name.${type}`
  return known(key) ? t(key) : t('art.unknownType', { type })
}

/** What an artifact of this type is, in one sentence. */
export function typeDescription(t: (k: Key, p?: Record<string, unknown>) => string, type: string): string {
  const key = `art.desc.${type}`
  return known(key) ? t(key) : t('art.unknownDesc')
}
