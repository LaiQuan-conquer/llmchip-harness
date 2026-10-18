// M2 acceptance through the kernel with the bundled plugins and stories260K:
// S0 -> G0 -> S1 (intake, L0) -> S2 (numeric spec, L2, accuracy) -> G1 ->
// S3 (chip spec) -> G2, then a changed numeric spec invalidates G1 and G2 and
// their evidence (AC-04 and the M2 criteria of the development plan).

import { describe, expect, it } from 'vitest'
import { flow, out, run, throughG2 } from './flow-helpers.ts'
import { connect, startKernel } from './helpers.ts'

describe('M2: model to chip spec', () => {
  it('passes G0, G1 and G2 on bound snapshots, and a new numeric spec invalidates them', { timeout: 600_000 }, async () => {
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    const { bind, judge } = flow(client)
    const { quantize, design, g1, g2 } = await throughG2(client)
    for (const gate of ['G0', 'G1', 'G2']) expect((await client.call('gates.release', { project: 'chip', gate })).released).toBe(true)

    // Approvals are bound to snapshots: changing the numeric spec invalidates G1 and G2 and their evidence.
    const q8 = await quantize('int8')
    await bind('numeric-spec', out(q8, 'spec'))
    await bind('l2-weights', out(q8, 'weights'))
    for (const gate of ['G1', 'G2']) {
      expect(await client.call('gates.release', { project: 'chip', gate })).toMatchObject({ released: false, status: 'invalidated' })
      expect((await client.call('gates.snapshot', { project: 'chip', gate })).evidence).toEqual([])
    }
    expect((await client.call('gates.release', { project: 'chip', gate: 'G0' })).released).toBe(true)
    const invalidated = (await client.call('events.list', { kind: 'approval.invalidated' })).events.map((e) => e.payload.approval)
    expect(invalidated).toEqual(expect.arrayContaining([g1.id, g2.id]))
    // The old chip spec does not follow the new numeric spec.
    const stale = await run(client, 'arch-taalas', 'verify_spec', { chip: out(design, 'chip'), spec: out(q8, 'spec'), ppa: out(design, 'ppa') })
    expect(await judge('arch.spec', stale)).toMatchObject({ verdict: 'fail', reasons: [{ code: 'CRITERION_FAILED', params: { metric: 'numeric_spec_matches' } }] })
  })
})
