// TypeScript SDK for out-of-process plugins (IF-2, architecture §9.2).
//
// A plugin process reads newline-delimited JSON-RPC 2.0 requests from stdin
// and writes responses and notifications to stdout. Anything a handler prints
// with console.* goes to stderr, which the kernel captures as the run log, so
// it can never corrupt the protocol stream.

import { createInterface } from 'node:readline'
import {
  PLUGIN_PROTOCOL_VERSION,
  type ProtocolInitializeResult,
  type ProtocolInput,
  type ProtocolInvokeParams,
  type ProtocolInvokeResult,
} from '@llmchip/kernel-api'

export interface InvokeContext {
  run: string
  params: Record<string, unknown>
  inputs: ProtocolInput[]
  /** Directory the handler writes its outputs to; output paths are relative to it. */
  outDir: string
  /** Scratch directory, discarded after the run. */
  workDir: string
  /** Aborted when the kernel cancels the run. */
  signal: AbortSignal
  progress(fraction?: number, message?: string): void
  log(level: 'debug' | 'info' | 'warn' | 'error', message: string): void
  /** The input with the given name; throws if it is missing. */
  input(name: string): ProtocolInput
}

export type Handler = (ctx: InvokeContext) => ProtocolInvokeResult | Promise<ProtocolInvokeResult>

export interface PluginDefinition {
  id: string
  version: string
  handlers: Record<string, Handler>
}

const write = process.stdout.write.bind(process.stdout)

function send(message: object): void {
  write(JSON.stringify(message) + '\n')
}

/** Serves the plugin over stdio until the kernel sends `shutdown` or closes stdin. */
export function runPlugin(plugin: PluginDefinition): void {
  for (const method of ['log', 'info', 'warn', 'error', 'debug'] as const) {
    console[method] = (...args: unknown[]) => process.stderr.write(args.map(String).join(' ') + '\n')
  }
  const cancels = new Map<string, AbortController>()
  const lines = createInterface({ input: process.stdin })
  lines.on('line', (line) => {
    if (!line.trim()) return
    let message: { id?: number | string; method?: string; params?: any }
    try {
      message = JSON.parse(line)
    } catch {
      return send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'invalid JSON' } })
    }
    const { id, method, params } = message
    const reply = (result: unknown) => send({ jsonrpc: '2.0', id, result })
    const fail = (code: number, text: string) => send({ jsonrpc: '2.0', id, error: { code, message: text } })
    switch (method) {
      case 'initialize': {
        const result: ProtocolInitializeResult = { id: plugin.id, version: plugin.version, handlers: Object.keys(plugin.handlers) }
        if (params?.protocolVersion !== PLUGIN_PROTOCOL_VERSION) {
          return fail(-32000, `unsupported protocol version ${params?.protocolVersion}; this SDK speaks ${PLUGIN_PROTOCOL_VERSION}`)
        }
        return reply(result)
      }
      case 'invoke':
        return void invoke(plugin, params as ProtocolInvokeParams, cancels).then(reply, (error: unknown) =>
          fail(-32001, error instanceof Error ? error.message : String(error)),
        )
      case 'cancel':
        cancels.get(params?.run)?.abort()
        return
      case 'shutdown':
        reply({ ok: true })
        lines.close()
        return
      default:
        if (id !== undefined) fail(-32601, `unknown method ${method}`)
    }
  })
  lines.on('close', () => process.exit(process.exitCode ?? 0))
}

async function invoke(
  plugin: PluginDefinition,
  params: ProtocolInvokeParams,
  cancels: Map<string, AbortController>,
): Promise<ProtocolInvokeResult> {
  const handler = plugin.handlers[params.handler]
  if (!handler) throw new Error(`plugin ${plugin.id} has no handler ${params.handler}`)
  const controller = new AbortController()
  cancels.set(params.run, controller)
  try {
    return await handler({
      run: params.run,
      params: params.params,
      inputs: params.inputs,
      outDir: params.outDir,
      workDir: params.workDir,
      signal: controller.signal,
      progress: (fraction, message) =>
        send({ jsonrpc: '2.0', method: 'progress', params: { run: params.run, ...(fraction === undefined ? {} : { fraction }), ...(message ? { message } : {}) } }),
      log: (level, message) => send({ jsonrpc: '2.0', method: 'log', params: { run: params.run, level, message } }),
      input: (name) => {
        const input = params.inputs.find((i) => i.name === name)
        if (!input) throw new Error(`missing input ${name}`)
        return input
      },
    })
  } finally {
    cancels.delete(params.run)
  }
}
