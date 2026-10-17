// `llmchip mcp`: a Model Context Protocol server on stdio that lets an
// external agent (Claude Code, Codex, ...) use the workbench as an external
// client (requirements §6.1, FR-INT-06). It connects with the kernel's
// external token, so the kernel's policy applies: read the workbench and
// start work, never decide, waive, change constraints or see NDA content.

import { createInterface } from 'node:readline'
import { KernelCallError, KernelClient } from '@llmchip/client'
import type { KernelMethod } from '@llmchip/kernel-api'

export const MCP_PROTOCOL_VERSION = '2025-06-18'

/** Shown to the connecting agent and its user (FR-AGT-01). */
export const BOUNDARY_NOTICE =
  'You are connected to the llmchip-harness workbench as an external client. Through these tools you can read projects, ' +
  'artifacts, runs, events and incidents, and start runs and agent tasks. You cannot approve gates, waive checks, change ' +
  'constraints or policy, or read NDA-labelled content. The workbench controls only what you do through these tools: it ' +
  'does not control your other tools, such as files or a terminal on this computer.'

interface Tool {
  name: string
  description: string
  inputSchema: Record<string, unknown>
  run(client: KernelClient, args: Record<string, any>): Promise<unknown>
}

const object = (properties: Record<string, unknown> = {}, required: string[] = []) => ({ type: 'object', properties, required })
const rpc =
  (method: KernelMethod) =>
  (client: KernelClient, args: Record<string, unknown>): Promise<unknown> =>
    (client.call as (m: string, p: unknown) => Promise<unknown>)(method, args)

const TOOLS: Tool[] = [
  { name: 'kernel_info', description: 'Workbench kernel version and event log head.', inputSchema: object(), run: rpc('kernel.info') },
  { name: 'list_projects', description: 'List chip projects.', inputSchema: object(), run: rpc('projects.list') },
  {
    name: 'get_project',
    description: 'Project state: slots, stages and gate release status.',
    inputSchema: object({ project: { type: 'string' }, branch: { type: 'string' } }, ['project']),
    run: rpc('projects.get'),
  },
  { name: 'list_artifacts', description: 'List artifacts.', inputSchema: object({ type: { type: 'string' }, limit: { type: 'integer' } }), run: rpc('artifacts.list') },
  {
    name: 'read_artifact',
    description: 'Read the text content of an artifact. NDA-labelled content is refused.',
    inputSchema: object({ hash: { type: 'string' } }, ['hash']),
    run: async (client, args) => (await client.download(String(args.hash))).text(),
  },
  {
    name: 'artifact_lineage',
    description: 'Lineage graph of an artifact.',
    inputSchema: object({ hash: { type: 'string' }, direction: { enum: ['up', 'down'] } }, ['hash']),
    run: rpc('artifacts.lineage'),
  },
  {
    name: 'list_events',
    description: 'Events of the workbench log, optionally filtered by kind or kind prefix.',
    inputSchema: object({ since: { type: 'integer' }, limit: { type: 'integer' }, kind: { type: 'string' }, project: { type: 'string' } }),
    run: rpc('events.list'),
  },
  { name: 'list_runs', description: 'List runs.', inputSchema: object({ project: { type: 'string' }, state: { type: 'string' } }), run: rpc('runs.list') },
  {
    name: 'start_run',
    description: 'Start a plugin handler run.',
    inputSchema: object({ plugin: { type: 'string' }, handler: { type: 'string' }, params: { type: 'object' }, inputs: { type: 'array' }, project: { type: 'string' } }, ['plugin', 'handler']),
    run: rpc('runs.start'),
  },
  { name: 'wait_run', description: 'Wait for a run to finish.', inputSchema: object({ run: { type: 'string' }, timeoutMs: { type: 'integer' } }, ['run']), run: rpc('runs.wait') },
  { name: 'list_incidents', description: 'List incidents.', inputSchema: object({ open: { type: 'boolean' }, kind: { type: 'string' } }), run: rpc('incidents.list') },
  { name: 'list_tasks', description: 'List agent tasks.', inputSchema: object(), run: rpc('tasks.list') },
  {
    name: 'create_task',
    description: 'Create an agent task for the workbench\'s managed agents. `goal` must be a requirement id such as FR-EST-07 or `goal:<charter-goal>`.',
    inputSchema: object(
      { project: { type: 'string' }, goal: { type: 'string' }, title: { type: 'string' }, role: { enum: ['executor', 'verifier', 'reviewer'] }, provider: { type: 'string' } },
      ['project', 'goal', 'title', 'role', 'provider'],
    ),
    run: rpc('tasks.create'),
  },
]

/** Serves MCP on the given streams until input ends. */
export async function serveMcp(runtime: { url: string; externalToken: string }, input: NodeJS.ReadableStream, write: (line: string) => void, version: string): Promise<void> {
  const client = await KernelClient.connect({ url: `${runtime.url}`, token: runtime.externalToken })
  const send = (message: object) => write(JSON.stringify(message) + '\n')
  const lines = createInterface({ input })
  const pending: Promise<void>[] = []
  for await (const line of lines) {
    if (!line.trim()) continue
    let message: { id?: string | number; method?: string; params?: any }
    try {
      message = JSON.parse(line)
    } catch {
      send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'invalid JSON' } })
      continue
    }
    const { id, method, params } = message
    if (id === undefined) continue // Notifications such as notifications/initialized need no answer.
    const reply = (result: unknown) => send({ jsonrpc: '2.0', id, result })
    switch (method) {
      case 'initialize':
        reply({
          protocolVersion: MCP_PROTOCOL_VERSION,
          capabilities: { tools: {} },
          serverInfo: { name: 'llmchip-harness', version },
          instructions: BOUNDARY_NOTICE,
        })
        break
      case 'ping':
        reply({})
        break
      case 'tools/list':
        reply({ tools: TOOLS.map(({ name, description, inputSchema }) => ({ name, description: `${description}\n\n${BOUNDARY_NOTICE}`, inputSchema })) })
        break
      case 'tools/call': {
        const tool = TOOLS.find((t) => t.name === params?.name)
        if (!tool) {
          send({ jsonrpc: '2.0', id, error: { code: -32602, message: `unknown tool ${params?.name}` } })
          break
        }
        pending.push(
          tool.run(client, params?.arguments ?? {}).then(
            (result) => reply({ content: [{ type: 'text', text: typeof result === 'string' ? result : JSON.stringify(result, null, 2) }] }),
            (error: unknown) =>
              reply({
                isError: true,
                content: [{ type: 'text', text: error instanceof KernelCallError ? `${error.code}: ${error.message}` : String(error) }],
              }),
          ),
        )
        break
      }
      default:
        send({ jsonrpc: '2.0', id, error: { code: -32601, message: `unknown method ${method}` } })
    }
  }
  await Promise.all(pending)
  await client.close()
}
