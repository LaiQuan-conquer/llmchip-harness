// ADR-001 prototype validation: in-kernel plugins hosted on Cordis.
// Exit criteria (architecture §5): unload order, fault isolation, size and
// startup time (the latter is measured by tests/kernel-footprint.spec.ts).

import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { describe, expect, it } from 'vitest'
import type { Context } from 'cordis'
import { EventStore, Kernel, type InKernelPlugin } from '@llmchip/kernel'
import { tempDir } from './helpers.ts'

declare module 'cordis' {
  interface Context {
    probe: { alive: boolean; calls: number }
  }
}

const PLUGIN = { kind: 'plugin', id: 'test' } as const

function kinds(home: string): string[] {
  const events = new EventStore(join(home, 'db', 'harness.sqlite'))
  try {
    return events.list({ limit: 1000 }).map((e) => (e.kind.startsWith('plugin.') ? `${e.kind}:${(e.payload as { plugin: string }).plugin}` : e.kind))
  } finally {
    events.close()
  }
}

describe('PluginHost on Cordis', () => {
  it('contains plugins that throw or hang while loading', async () => {
    const home = tempDir()
    const healthy: InKernelPlugin = { id: 'healthy', apply: () => {} }
    const kernel = await Kernel.start({
      home,
      pluginTimeoutMs: 200,
      plugins: [
        { id: 'throws', apply: () => { throw new Error('boom') } },
        { id: 'rejects', apply: async () => { throw new Error('async boom') } },
        { id: 'hangs', apply: () => new Promise(() => {}) },
        healthy,
      ],
    })
    expect(kernel.plugins.loaded()).toEqual(['healthy'])
    // The kernel keeps serving after the failures.
    expect((await kernel.artifacts.put('still works', { type: 't', creator: PLUGIN })).size).toBe(11)
    await kernel.stop()
    expect(kinds(home)).toEqual([
      'kernel.started',
      'plugin.failed:throws',
      'plugin.failed:rejects',
      'plugin.failed:hangs',
      'plugin.loaded:healthy',
      'artifact.created',
      'plugin.unloaded:healthy',
      'kernel.stopped',
    ])
  })

  it('runs plugin cleanup before storage closes, in reverse load order', async () => {
    const home = tempDir()
    const order: string[] = []
    const provider: InKernelPlugin = {
      id: 'provider',
      provides: ['probe'],
      apply(ctx) {
        const probe = { alive: true, calls: 0 }
        ctx.provide('probe', probe)
        ctx.effect(() => () => {
          order.push('provider cleanup')
          probe.alive = false
        })
      },
    }
    const consumer: InKernelPlugin = {
      id: 'consumer',
      inject: ['probe'],
      apply(ctx: Context, kernel) {
        ctx.effect(() => async () => {
          // Slow asynchronous cleanup that still needs the provider's service and storage.
          await sleep(50)
          order.push(`consumer cleanup, provider alive: ${ctx.probe.alive}`)
          await kernel.artifacts.put('written during cleanup', { type: 't', creator: PLUGIN })
        })
      },
    }
    const kernel = await Kernel.start({ home, plugins: [provider, consumer] })
    await kernel.stop()
    expect(order).toEqual(['consumer cleanup, provider alive: true', 'provider cleanup'])
    expect(kinds(home).slice(-5)).toEqual([
      'plugin.loaded:consumer',
      'artifact.created',
      'plugin.unloaded:consumer',
      'plugin.unloaded:provider',
      'kernel.stopped',
    ])
  })

  it('unloads dependents first when a provider is unloaded directly', async () => {
    const home = tempDir()
    const order: string[] = []
    const kernel = await Kernel.start({
      home,
      plugins: [
        {
          id: 'provider',
          provides: ['probe'],
          apply(ctx) {
            const probe = { alive: true, calls: 0 }
            ctx.provide('probe', probe)
            ctx.effect(() => () => {
              probe.alive = false
              order.push('provider')
            })
          },
        },
        {
          id: 'consumer',
          inject: ['probe'],
          apply(ctx) {
            ctx.effect(() => () => void order.push(`consumer (provider alive: ${ctx.probe.alive})`))
          },
        },
      ],
    })
    await kernel.plugins.unload('provider')
    expect(order).toEqual(['consumer (provider alive: true)', 'provider'])
    await kernel.stop()
  })

  it('records the Cordis ordering hazard that the host guards against', async () => {
    // Plain Cordis, without the host: disposing a provider runs its own
    // cleanup before its dependents are deactivated.
    const { Context } = await import('cordis')
    const root = new Context()
    const order: string[] = []
    const provider = root.plugin((ctx: Context) => {
      const probe = { alive: true, calls: 0 }
      ctx.provide('probe', probe)
      ctx.effect(() => () => {
        probe.alive = false
        order.push('provider')
      })
    })
    await provider
    await root.plugin(
      Object.assign((ctx: Context) => void ctx.effect(() => () => void order.push(`consumer (provider alive: ${ctx.probe.alive})`)), {
        inject: ['probe'],
      }),
    )
    await provider.dispose()
    expect(order).toEqual(['provider', 'consumer (provider alive: false)'])
    await root.fiber.dispose()
  })

  it('does not let a plugin whose cleanup never finishes block shutdown', async () => {
    const home = tempDir()
    const kernel = await Kernel.start({
      home,
      pluginTimeoutMs: 200,
      plugins: [{ id: 'stuck', apply: (ctx) => void ctx.effect(() => () => new Promise<void>(() => {})) }],
    })
    await kernel.stop()
    expect(kinds(home).slice(-3)).toEqual(['plugin.failed:stuck', 'plugin.unloaded:stuck', 'kernel.stopped'])
  })

  it("propagates a listener's exception through synchronous emit", async () => {
    // Documents a Cordis property relied on by ADR-001: synchronous `emit`
    // propagates a listener's exception to the emitter. The kernel therefore
    // never uses Cordis events on its critical paths (architecture §5 item 4).
    const kernel = await Kernel.start({ home: tempDir() })
    // Untyped: the event is not declared in Cordis's Events interface.
    const ctx = kernel.plugins.root as any
    const reached: string[] = []
    ctx.on('test/event', () => {
      throw new Error('listener failure')
    })
    ctx.on('test/event', () => void reached.push('second listener'))
    expect(() => ctx.emit('test/event')).toThrow('listener failure')
    expect(reached).toEqual([])
    await kernel.stop()
  })
})
