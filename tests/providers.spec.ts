// The OpenAI-compatible (DeepSeek, GPT, local servers) and Anthropic adapters
// against local mock servers: request shape, tool-call parsing, API keys from
// the secret store, and that the egress gateway sees every request first.

import { createServer, type IncomingMessage } from 'node:http'
import type { AddressInfo } from 'node:net'
import { describe, expect, it } from 'vitest'
import type { LlmProviderConfig } from '@llmchip/kernel-api'
import { connect, startKernel } from './helpers.ts'

interface Captured {
  headers: IncomingMessage['headers']
  body: any
}

async function mockServer(replies: object[]): Promise<{ url: string; requests: Captured[]; close(): Promise<void> }> {
  const requests: Captured[] = []
  const server = createServer(async (req, res) => {
    let body = ''
    for await (const chunk of req) body += chunk
    requests.push({ headers: req.headers, body: JSON.parse(body) })
    const reply = replies[requests.length - 1] ?? replies.at(-1)
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(reply))
  })
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r))
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, requests, close: () => new Promise((r) => server.close(() => r())) }
}

/** A kernel with one provider, its API key in the secret store, and a project; runs one task to the end. */
async function runTask(provider: LlmProviderConfig) {
  const { kernel } = await startKernel(undefined, { bundled: true, config: { llm: { providers: [provider] } } })
  const client = await connect(kernel)
  await client.call('secrets.set', { name: 'llm.key', value: 'sk-test-0123456789' })
  await client.call('projects.create', { id: 'chip', title: 'Chip' })
  const t = await client.call('tasks.create', { project: 'chip', goal: 'FR-EST-01', title: 'Estimate', role: 'executor', provider: provider.id })
  return { client, task: await client.call('tasks.run', { task: t.id }) }
}

describe('LLM provider adapters', () => {
  it('speaks the OpenAI-compatible chat completions API', async () => {
    const server = await mockServer([
      {
        choices: [{ message: { content: 'listing', tool_calls: [{ id: 'c1', type: 'function', function: { name: 'list_artifacts', arguments: '{}' } }] } }],
        usage: { total_tokens: 120 },
      },
      { choices: [{ message: { content: '', tool_calls: [{ id: 'c2', type: 'function', function: { name: 'finish', arguments: '{"result":"ok"}' } }] } }], usage: { total_tokens: 30 } },
    ])
    try {
      const { client, task } = await runTask({ id: 'deepseek', kind: 'openai-compatible', location: 'cloud', baseUrl: server.url, model: 'deepseek-chat', apiKeySecret: 'llm.key' })
      expect(task).toMatchObject({ state: 'finished', result: 'ok', tokens: 150 })

      const [first, second] = server.requests
      expect(first!.headers.authorization).toBe('Bearer sk-test-0123456789')
      expect(first!.body).toMatchObject({ model: 'deepseek-chat', messages: [{ role: 'system' }, { role: 'user' }] })
      expect(first!.body.tools.map((t: any) => t.function.name)).toContain('write_file')
      expect(second!.body.messages.slice(-2)).toMatchObject([
        { role: 'assistant', tool_calls: [{ id: 'c1', function: { name: 'list_artifacts' } }] },
        { role: 'tool', tool_call_id: 'c1' },
      ])
      const audits = (await client.call('events.list', { kind: 'egress.decided' })).events
      expect(audits.map((e) => e.payload.decision)).toEqual(['allowed', 'allowed'])
      expect(JSON.stringify(audits)).not.toContain('sk-test-0123456789')
    } finally {
      await server.close()
    }
  })

  it('speaks the Anthropic messages API', async () => {
    const server = await mockServer([
      { content: [{ type: 'text', text: 'done' }, { type: 'tool_use', id: 't1', name: 'finish', input: { result: 'from claude' } }], usage: { input_tokens: 10, output_tokens: 5 } },
    ])
    try {
      const { task } = await runTask({ id: 'claude', kind: 'anthropic', location: 'cloud', baseUrl: server.url, model: 'claude-sonnet-5', apiKeySecret: 'llm.key' })
      expect(task).toMatchObject({ state: 'finished', result: 'from claude', tokens: 15 })
      const [request] = server.requests
      expect(request!.headers['x-api-key']).toBe('sk-test-0123456789')
      expect(request!.headers['anthropic-version']).toBe('2023-06-01')
      expect(request!.body).toMatchObject({ model: 'claude-sonnet-5', messages: [{ role: 'user' }] })
      expect(request!.body.system).toMatch(/managed agent of llmchip-harness/)
      expect(request!.body.tools.find((t: any) => t.name === 'finish').input_schema).toMatchObject({ type: 'object' })
    } finally {
      await server.close()
    }
  })
})

describe('provider configuration errors', () => {
  it('pauses the task and records an incident when the API key is missing', async () => {
    const { kernel } = await startKernel(undefined, {
      bundled: true,
      config: { llm: { providers: [{ id: 'gpt', kind: 'openai-compatible', location: 'cloud', baseUrl: 'http://127.0.0.1:9', model: 'gpt', apiKeySecret: 'missing.key' }] } },
    })
    const client = await connect(kernel)
    await client.call('projects.create', { id: 'chip', title: 'Chip' })
    const t = await client.call('tasks.create', { project: 'chip', goal: 'FR-EST-01', title: 'x', role: 'executor', provider: 'gpt' })
    expect(await client.call('tasks.run', { task: t.id })).toMatchObject({ state: 'paused', steps: 0 })
    expect((await client.call('incidents.list', { kind: 'llm_failure' })).incidents[0]!.summary.params).toMatchObject({ error: expect.stringMatching(/LLM_KEY_MISSING|missing.key/) })
  })
})
