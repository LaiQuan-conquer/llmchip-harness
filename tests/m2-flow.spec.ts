// M2 acceptance through the kernel with the bundled plugins and stories260K:
// S0 -> S1 (intake, L0) -> S2 (numeric spec, L2, accuracy) -> S3 (chip spec),
// each judged by its checks; then a changed numeric spec is not covered by the
// earlier verdicts, and the old chip spec fails against it (AC-04).

import { describe, expect, it } from 'vitest'
import { flow, out, run, throughS3 } from './flow-helpers.ts'
import { connect, startKernel } from './helpers.ts'

describe('M2: model to chip spec', () => {
  it('judges S0-S3 by their checks, and earlier evidence does not judge a new numeric spec', { timeout: 600_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    const { bind, judge } = flow(client)
    const { quantize, design, spec } = await throughS3(client)
    const passed = await client.call('verdicts.list', { check: 'arch.spec' })
    expect(passed.verdicts.some((v) => v.verdict === 'pass' && v.subjects.includes(spec))).toBe(true)

    // A new numeric spec: the earlier verdicts judge the old spec only.
    const q8 = await quantize('int8')
    const spec8 = out(q8, 'spec')
    await bind('numeric-spec', spec8)
    await bind('l2-weights', out(q8, 'weights'))
    // The old chip spec does not follow the new numeric spec.
    const stale = await run(client, 'arch-taalas', 'verify_spec', { chip: out(design, 'chip'), spec: out(q8, 'spec'), ppa: out(design, 'ppa'), weights: out(q8, 'weights') })
    const verdict = await judge('arch.spec', stale)
    expect(verdict.verdict).toBe('fail')
    expect(verdict.reasons).toEqual(expect.arrayContaining([expect.objectContaining({ code: 'CRITERION_FAILED', params: expect.objectContaining({ metric: 'numeric_spec_matches' }) })]))
    // On the new spec, arch.spec has exactly this failure; the pass on the old spec stays.
    expect((await client.call('verdicts.list', { check: 'arch.spec', subject: spec8 })).verdicts.map((v) => v.id)).toEqual([verdict.id])
    expect((await client.call('verdicts.list', { check: 'arch.spec', subject: spec })).verdicts.some((v) => v.verdict === 'pass')).toBe(true)
  })
})
