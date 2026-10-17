// The MCP server (`llmchip mcp`) as an external agent sees it: a separate
// process speaking MCP on stdio, connected to the kernel as an external client.

import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { BOUNDARY_NOTICE } from '@llmchip/cli'
import { connect, startKernel } from './helpers.ts'

const CLI = resolve(import.meta.dirname, '../packages/cli/src/bin.ts')

describe('llmchip mcp', () => {
  it('serves read-only tools, states its boundary and refuses NDA content', { timeout: 30_000 }, async () => {
    const { kernel, home } = await startKernel()
    const owner = await connect(kernel)
    const nda = await owner.upload('fpdk_sc7t_nand2x1 area 10.98', { type: 'pdk.cells', label: 'nda' })
    const note = await owner.upload('hello from the workbench', { type: 'text.note', label: 'internal' })

    const child = spawn(process.execPath, ['--conditions=development', CLI, '--home', home, 'mcp'], { stdio: ['pipe', 'pipe', 'inherit'] })
    const replies = new Map<number, any>()
    const lines = createInterface({ input: child.stdout! })
    lines.on('line', (line) => {
      const message = JSON.parse(line)
      replies.set(message.id, message)
    })
    let id = 0
    const request = async (method: string, params: object = {}) => {
      const current = ++id
      child.stdin!.write(JSON.stringify({ jsonrpc: '2.0', id: current, method, params }) + '\n')
      await expect.poll(() => replies.has(current), { timeout: 10_000 }).toBe(true)
      return replies.get(current)
    }

    try {
      const init = await request('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '0' } })
      expect(init.result).toMatchObject({ serverInfo: { name: 'llmchip-harness' }, capabilities: { tools: {} }, instructions: BOUNDARY_NOTICE })
      expect(init.result.instructions).toMatch(/does not control your other tools/)
      child.stdin!.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n')

      const tools = (await request('tools/list')).result.tools.map((t: { name: string }) => t.name)
      expect(tools).toContain('read_artifact')
      expect(tools).not.toContain('approve_gate')

      const info = await request('tools/call', { name: 'kernel_info', arguments: {} })
      expect(JSON.parse(info.result.content[0].text)).toMatchObject({ name: 'harnessd' })
      expect((await request('tools/call', { name: 'read_artifact', arguments: { hash: note.hash } })).result.content[0].text).toBe('hello from the workbench')

      const refused = (await request('tools/call', { name: 'read_artifact', arguments: { hash: nda.hash } })).result
      expect(refused.isError).toBe(true)
      expect(refused.content[0].text).toMatch(/^EGRESS_BLOCKED/)
      expect(refused.content[0].text).not.toContain('fpdk_sc7t')
      expect((await request('tools/call', { name: 'no_such_tool', arguments: {} })).error.code).toBe(-32602)
    } finally {
      child.stdin!.end()
      await new Promise((r) => child.once('exit', r))
    }
    const { incidents } = await owner.call('incidents.list', { kind: 'egress_blocked' })
    expect(incidents[0]!.actor).toMatchObject({ kind: 'external' })
  })
})
