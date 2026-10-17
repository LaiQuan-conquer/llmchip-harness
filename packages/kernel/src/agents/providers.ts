import type { Actor, EgressDestination, Label, LlmProviderConfig } from '@llmchip/kernel-api'
import { defaultMaxLabel, type EgressGateway, type Segment } from '../security/egress.ts'
import { KernelError } from '../util/errors.ts'

/** One message of an agent conversation. Every message carries the label of its content. */
export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  label: Label
  source: string
  toolCalls?: { id: string; name: string; arguments: Record<string, unknown> }[]
  toolCallId?: string
}

export interface ToolSpec {
  name: string
  description: string
  parameters: Record<string, unknown>
}

export interface LlmReply {
  text: string
  toolCalls: { id: string; name: string; arguments: Record<string, unknown> }[]
  tokens: number
}

export interface LlmProvider {
  readonly config: LlmProviderConfig
  readonly destination: EgressDestination
  /**
   * Sends a conversation. The egress gateway checks every message before any
   * byte leaves the kernel (ADR-008); providers never open connections themselves.
   */
  complete(messages: ChatMessage[], tools: ToolSpec[], actor: Actor, project: string): Promise<LlmReply>
}

/** Creates the provider for a configuration. API keys come from the secret store. */
export function createProvider(config: LlmProviderConfig, gateway: EgressGateway, secret: (name: string) => string | undefined): LlmProvider {
  const destination: EgressDestination = { id: `llm:${config.id}`, kind: 'llm', location: config.location, maxLabel: defaultMaxLabel(config.location) }
  const send = async (messages: ChatMessage[], actor: Actor, project: string, request: () => Promise<Response>) => {
    gateway.check(destination, messages.map(toSegment), actor, project)
    const response = await request()
    if (!response.ok) {
      throw new KernelError('LLM_REQUEST_FAILED', `provider ${config.id} answered HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`)
    }
    return response.json() as Promise<any>
  }
  const key = () => {
    const value = config.apiKeySecret ? secret(config.apiKeySecret) : undefined
    if (config.apiKeySecret && !value) throw new KernelError('LLM_KEY_MISSING', `secret ${config.apiKeySecret} for provider ${config.id} is not set`)
    return value
  }
  switch (config.kind) {
    case 'scripted':
      return {
        config,
        destination,
        async complete(messages, _tools, actor, project) {
          gateway.check(destination, messages.map(toSegment), actor, project)
          const turn = messages.filter((m) => m.role === 'assistant').length
          const step = (config.script ?? [])[turn] as { text?: string; toolCalls?: { name: string; arguments?: Record<string, unknown> }[] } | undefined
          if (!step) return { text: 'script exhausted', toolCalls: [{ id: `call-${turn}-0`, name: 'finish', arguments: { result: 'script exhausted' } }], tokens: 0 }
          // `{{result.<i>.<path>}}` refers to the i-th tool result of the previous turn, parsed as JSON.
          const previous: unknown[] = []
          for (let i = messages.length - 1; i >= 0 && messages[i]!.role === 'tool'; i--) previous.unshift(parseArguments(messages[i]!.content))
          return {
            text: step.text ?? '',
            toolCalls: (step.toolCalls ?? []).map((call, i) => ({ id: `call-${turn}-${i}`, name: call.name, arguments: substitute(call.arguments ?? {}, previous) as Record<string, unknown> })),
            tokens: 0,
          }
        },
      }
    case 'openai-compatible':
      return {
        config,
        destination,
        async complete(messages, tools, actor, project) {
          const apiKey = key()
          const body = {
            model: config.model,
            max_tokens: config.maxOutputTokens ?? 4096,
            messages: messages.map((m) =>
              m.role === 'tool'
                ? { role: 'tool', tool_call_id: m.toolCallId, content: m.content }
                : m.role === 'assistant'
                  ? {
                      role: 'assistant',
                      content: m.content || null,
                      ...(m.toolCalls?.length
                        ? { tool_calls: m.toolCalls.map((c) => ({ id: c.id, type: 'function', function: { name: c.name, arguments: JSON.stringify(c.arguments) } })) }
                        : {}),
                    }
                  : { role: m.role, content: m.content },
            ),
            tools: tools.map((t) => ({ type: 'function', function: { name: t.name, description: t.description, parameters: t.parameters } })),
          }
          const data = await send(messages, actor, project, () =>
            fetch(`${(config.baseUrl ?? 'https://api.openai.com/v1').replace(/\/$/, '')}/chat/completions`, {
              method: 'POST',
              headers: { 'content-type': 'application/json', ...(apiKey ? { authorization: `Bearer ${apiKey}` } : {}) },
              body: JSON.stringify(body),
            }),
          )
          const message = data.choices?.[0]?.message ?? {}
          return {
            text: message.content ?? '',
            toolCalls: (message.tool_calls ?? []).map((c: any) => ({ id: c.id, name: c.function.name, arguments: parseArguments(c.function.arguments) })),
            tokens: data.usage?.total_tokens ?? 0,
          }
        },
      }
    case 'anthropic':
      return {
        config,
        destination,
        async complete(messages, tools, actor, project) {
          const apiKey = key()
          const system = messages.filter((m) => m.role === 'system').map((m) => m.content).join('\n\n')
          const turns: { role: 'user' | 'assistant'; content: unknown[] }[] = []
          for (const m of messages) {
            if (m.role === 'system') continue
            const role = m.role === 'assistant' ? 'assistant' : 'user'
            const block =
              m.role === 'tool'
                ? { type: 'tool_result', tool_use_id: m.toolCallId, content: m.content }
                : m.role === 'assistant'
                  ? null
                  : { type: 'text', text: m.content }
            const blocks =
              m.role === 'assistant'
                ? [...(m.content ? [{ type: 'text', text: m.content }] : []), ...(m.toolCalls ?? []).map((c) => ({ type: 'tool_use', id: c.id, name: c.name, input: c.arguments }))]
                : [block]
            const last = turns.at(-1)
            if (last && last.role === role) last.content.push(...blocks)
            else turns.push({ role, content: blocks })
          }
          const data = await send(messages, actor, project, () =>
            fetch(`${(config.baseUrl ?? 'https://api.anthropic.com').replace(/\/$/, '')}/v1/messages`, {
              method: 'POST',
              headers: { 'content-type': 'application/json', 'anthropic-version': '2023-06-01', ...(apiKey ? { 'x-api-key': apiKey } : {}) },
              body: JSON.stringify({
                model: config.model,
                max_tokens: config.maxOutputTokens ?? 4096,
                system,
                messages: turns,
                tools: tools.map((t) => ({ name: t.name, description: t.description, input_schema: t.parameters })),
              }),
            }),
          )
          const content = (data.content ?? []) as any[]
          return {
            text: content.filter((b) => b.type === 'text').map((b) => b.text).join(''),
            toolCalls: content.filter((b) => b.type === 'tool_use').map((b) => ({ id: b.id, name: b.name, arguments: b.input ?? {} })),
            tokens: (data.usage?.input_tokens ?? 0) + (data.usage?.output_tokens ?? 0),
          }
        },
      }
  }
}

function toSegment(message: ChatMessage): Segment {
  const calls = message.toolCalls?.length ? `\n${JSON.stringify(message.toolCalls)}` : ''
  return { label: message.label, source: message.source, text: message.content + calls }
}

function parseArguments(text: string): Record<string, unknown> {
  try {
    const value = JSON.parse(text || '{}')
    return value && typeof value === 'object' ? value : {}
  } catch {
    return { _unparsed: text }
  }
}

function substitute(value: unknown, previous: unknown[]): unknown {
  if (typeof value === 'string') {
    const match = /^\{\{result\.(\d+)((?:\.[\w-]+)*)\}\}$/.exec(value)
    if (!match) return value
    let current: any = previous[Number(match[1])]
    for (const key of match[2]!.split('.').filter(Boolean)) current = current?.[/^\d+$/.test(key) ? Number(key) : key]
    return current
  }
  if (Array.isArray(value)) return value.map((item) => substitute(item, previous))
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, substitute(v, previous)]))
  return value
}
