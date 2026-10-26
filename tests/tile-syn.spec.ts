// R5 wiring: the SYN stage, the three PDK synthesis checks and the syn-yosys handler.
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function json(path: string): any {
  return JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'))
}

describe('R5 synthesis wiring', () => {
  const flow = json('../plugins/flow-six/flow.json')
  const plugin = json('../plugins/syn-yosys/llmchip-plugin.json')

  it('declares the SYN stage with the three PDK checks', () => {
    const stage = flow.stages.find((s: any) => s.id === 'SYN')
    expect(stage).toBeTruthy()
    expect(stage.tests.map((t: any) => t.check)).toEqual([
      'syn.synth.sky130',
      'syn.synth.gf180mcuD',
      'syn.synth.ihp-sg13g2',
      'syn.dft.scan.sky130',
      'syn.dft.scan.gf180mcuD',
      'syn.dft.scan.ihp-sg13g2',
      'syn.timing.sky130',
      'syn.timing.gf180mcuD',
      'syn.timing.ihp-sg13g2',
      'syn.dft.atpg.sky130',
      'syn.dft.atpg.gf180mcuD',
      'syn.dft.atpg.ihp-sg13g2',
    ])
    expect(stage.develop.nodes[0].plugin).toBe('syn-yosys')
    expect(stage.develop.nodes[0].handler).toBe('synth')
  })

  it('defines each syn.synth check on the syn-yosys handler', () => {
    for (const [id, handler] of [
      ['syn.synth.sky130', 'synth'],
      ['syn.synth.gf180mcuD', 'synth'],
      ['syn.synth.ihp-sg13g2', 'synth'],
      ['syn.dft.scan.sky130', 'dft'],
      ['syn.dft.scan.gf180mcuD', 'dft'],
      ['syn.dft.scan.ihp-sg13g2', 'dft'],
      ['syn.timing.sky130', 'timing'],
      ['syn.timing.gf180mcuD', 'timing'],
      ['syn.timing.ihp-sg13g2', 'timing'],
      ['syn.dft.atpg.sky130', 'atpg'],
      ['syn.dft.atpg.gf180mcuD', 'atpg'],
      ['syn.dft.atpg.ihp-sg13g2', 'atpg'],
    ] as const) {
      const check = flow.checks.find((c: any) => c.id === id)
      expect(check, id).toBeTruthy()
      expect(check.plugin).toBe('syn-yosys')
      expect(check.handler).toBe(handler)
      expect(check.completeness.length).toBeGreaterThan(0)
      expect(check.criteria.length).toBeGreaterThan(0)
    }
  })

  it('exposes the synth handler with a PDK enum and netlist output', () => {
    const handler = plugin.handlers.find((h: any) => h.name === 'synth')
    expect(handler).toBeTruthy()
    expect(handler.inputs.map((i: any) => i.name)).toEqual(['rtl'])
    expect(handler.params.properties.pdk.enum).toEqual(['sky130', 'gf180mcuD', 'ihp-sg13g2'])
    expect(handler.outputs.map((o: any) => o.type)).toContain('syn.netlist')
    const dft = plugin.handlers.find((h: any) => h.name === 'dft')
    expect(dft).toBeTruthy()
    expect(dft.outputs.map((o: any) => o.type)).toContain('syn.dft.report')
    const timing = plugin.handlers.find((h: any) => h.name === 'timing')
    expect(timing).toBeTruthy()
    expect(timing.outputs.map((o: any) => o.type)).toContain('syn.timing.report')
    const atpg = plugin.handlers.find((h: any) => h.name === 'atpg')
    expect(atpg).toBeTruthy()
    expect(atpg.outputs.map((o: any) => o.type)).toContain('syn.dft.report')
  })
})
