// The bundled flow packs load whole, and every pipeline node judges checks of its own handler
// (2026-10-30: flow-six was half registered by a check id conflict with flow-default, so its stages
// ran while the checks after the conflict did not exist; its design node then judged
// `spec.design`, which `verify_spec` defines, and the SPEC step could not complete).
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { connect, startKernel } from './helpers.ts'

function pack(name: string): any {
  return JSON.parse(readFileSync(new URL(`../plugins/${name}/flow.json`, import.meta.url), 'utf8'))
}

const flows = [pack('flow-six'), pack('flow-default')]
const checks = new Map<string, any>(flows.flatMap((flow: any) => flow.checks).map((check: any) => [check.id, check]))
const spec = flows[0].stages.find((stage: any) => stage.id === 'SPEC')

describe('the bundled flow packs', () => {
  it('judge every node with a check of the node\'s own handler', () => {
    for (const flow of flows) {
      for (const stage of flow.stages) {
        for (const node of stage.develop?.nodes ?? []) {
          for (const id of node.judge ?? []) {
            const check = checks.get(id)
            expect(check, `${flow.id}/${stage.id}/${node.id}: ${id} is not defined`).toBeTruthy()
            expect([check.plugin, check.handler], `${flow.id}/${stage.id}/${node.id}: ${id}`).toEqual([node.plugin, node.handler])
          }
        }
        for (const test of stage.tests ?? []) expect(checks.has(test.check), `${flow.id}/${stage.id}: ${test.check}`).toBe(true)
      }
    }
  })

  it('run the tile design through a verify node, so the chip spec is judged and the TLM still runs', () => {
    const nodes = spec.develop.nodes
    // The design node produces the chip spec and the PPA; `spec.design` is defined on verify_spec.
    const design = nodes.find((node: any) => node.id === 'design')
    expect(design.handler).toBe('design')
    expect(design.judge).toBeUndefined()
    const verify = nodes.find((node: any) => node.id === 'verify')
    expect(verify).toMatchObject({ plugin: 'arch-taalas', handler: 'verify_spec', judge: ['spec.design'] })
    // The check judges the chip spec and nothing else: its subjects are exactly what the handler reads.
    expect(verify.inputs).toEqual({ chip: 'node:design.chip' })
    expect(spec.tests.find((test: any) => test.check === 'spec.design').inputs).toEqual({ chip: 'slot:chip-spec' })
    // The transaction-level model depends on the design, not on the verify node: a failed check
    // must not stop the chip-level simulation from running.
    const tlm = nodes.find((node: any) => node.id === 'tlm')
    expect(tlm.inputs.chip).toBe('node:design.chip')
    expect(Object.values(tlm.inputs).filter((ref: any) => String(ref).startsWith('node:verify'))).toEqual([])
  })

  it('load whole: every declared check keeps its own definition, and both flows are usable', async () => {
    const { kernel, home } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    // No pack was refused: a refusal would have left its stages or checks out of the kernel.
    expect(readFileSync(join(home, 'logs/harnessd.log'), 'utf8')).not.toMatch(/FLOW_INVALID/)
    // Every plugin the packs run and every plugin their checks run must be registered: a manifest
    // the kernel refuses (a kind outside `PluginKind`, a field the input spec does not allow) leaves
    // a stage that cannot start while its own pytest and JSON tests still pass (2026-10-30).
    const registered = new Set((await client.call('plugins.list', {})).plugins.map((plugin: any) => plugin.id))
    const used = new Set<string>(
      flows.flatMap((flow: any) => [
        ...flow.stages.flatMap((stage: any) => (stage.develop?.nodes ?? []).map((node: any) => node.plugin)),
        ...flow.checks.map((check: any) => check.plugin),
      ]),
    )
    for (const plugin of used) expect(registered, plugin).toContain(plugin)
    const listed = new Map<string, any>((await client.call('checks.list', {})).checks.map((check: any) => [check.id, check]))
    for (const flow of flows) {
      for (const check of flow.checks) {
        const found = listed.get(check.id)
        expect(found, `${flow.id}: ${check.id} is missing`).toBeTruthy()
        expect([found.plugin, found.handler, found.version], `${flow.id}: ${check.id}`).toEqual([check.plugin, check.handler, check.version])
      }
    }
    // The tile's equivalence check has an id of its own, so neither pack shadows the other.
    expect(listed.get('rtl.equivalence')).toMatchObject({ plugin: 'sim-verilator', handler: 'verify' })
    expect(listed.get('rtl.tile.equivalence')).toMatchObject({ plugin: 'rtl-taalas', handler: 'tile_verify' })
    // A pack that was refused, or half registered, cannot create a project on its flow.
    for (const flow of flows) await expect(client.call('projects.create', { id: `p-${flow.id}`, title: flow.id, flow: flow.id })).resolves.toBeTruthy()
  })
})
