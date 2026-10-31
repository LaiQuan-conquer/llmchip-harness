// R4 wiring: the flow-six VER stage and the tile_verify handler are present and bound to the
// same rtl-bundle. The actual verification run is the Python `tile_verify` handler
// (plugins/rtl-taalas/tests/test_verify.py and test_tile_slice.py cover the runner directly).
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function json(path: string): any {
  return JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'))
}

describe('R4 verification wiring', () => {
  const flow = json('../plugins/flow-six/flow.json')
  const plugin = json('../plugins/rtl-taalas/llmchip-plugin.json')

  it('declares the VER stage and its runnable checks', () => {
    const stage = flow.stages.find((s: any) => s.id === 'VER')
    expect(stage).toBeTruthy()
    const checks = stage.tests.map((t: any) => t.check)
    expect(checks).toEqual(['ver.unit', 'ver.slice', 'ver.cdc', 'ver.layer', 'rtl.tile.chip', 'ver.chain', 'ver.directed', 'ver.coverage', 'ver.formal', 'ver.mutation', 'ver.dft', 'rtl.tile.equivalence'])
    const node = stage.develop.nodes.find((n: any) => n.id === 'verify')
    expect(node.plugin).toBe('rtl-taalas')
    expect(node.handler).toBe('tile_verify')
  })

  it('defines the ver.unit, ver.slice, ver.cdc, ver.layer and ver.coverage, ver.formal, ver.mutation, ver.dft and rtl.tile.equivalence checks on the tile_verify handler', () => {
    for (const id of ['ver.unit', 'ver.slice', 'ver.cdc', 'ver.layer', 'rtl.tile.chip', 'ver.chain', 'ver.directed', 'ver.coverage', 'ver.formal', 'ver.mutation', 'ver.dft', 'rtl.tile.equivalence']) {
      const check = flow.checks.find((c: any) => c.id === id)
      expect(check, id).toBeTruthy()
      expect(check.plugin).toBe('rtl-taalas')
      expect(check.handler).toBe('tile_verify')
      expect(check.completeness.length).toBeGreaterThan(0)
      expect(check.criteria.length).toBeGreaterThan(0)
    }
  })

  it('exposes the tile_verify handler with a verification.report output', () => {
    const handler = plugin.handlers.find((h: any) => h.name === 'tile_verify')
    expect(handler).toBeTruthy()
    expect(handler.inputs.map((i: any) => i.name)).toEqual(['rtl', 'model'])
    expect(handler.outputs[0].type).toBe('verification.report')
  })
})
