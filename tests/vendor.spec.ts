// Smoke tests for the vendored Cordis packages (vendor/). They run against the
// built output in vendor/*/lib, the same entry points that workspace packages
// will import, so they also catch packaging mistakes in the vendored manifests.

import { describe, expect, it, vi } from 'vitest'
import { Context, Service } from 'cordis'
import Schema from 'schemastery'

describe('vendored cordis', () => {
  it('starts an injected plugin after its service and disposes its effects', async () => {
    class Counter extends Service {
      value = 0
      constructor(ctx: Context) {
        super(ctx, 'counter')
      }
    }

    const root = new Context()
    const disposed = vi.fn()
    const consumer = Object.assign((ctx: Context) => {
      ;(ctx as any).counter.value++
      ctx.effect(() => disposed)
    }, { inject: ['counter'] })

    const fiber = root.plugin(consumer)
    await root.plugin(Counter)
    await fiber
    expect((root as any).counter.value).toBe(1)

    await fiber.dispose()
    expect(disposed).toHaveBeenCalledOnce()
  })

  it('contains a plugin that throws during apply', async () => {
    const root = new Context()
    const logged = vi.spyOn(root.logger, 'error').mockImplementation(() => {})

    await expect(root.plugin(() => { throw new Error('boom') })).rejects.toThrow('boom')
    expect(logged).toHaveBeenCalled()

    const healthy = vi.fn()
    await root.plugin(healthy)
    expect(healthy).toHaveBeenCalledOnce()
  })

  it('validates plugin config with schemastery', async () => {
    const root = new Context()
    const received = vi.fn()
    const plugin = Object.assign((_ctx: Context, config: { port: number }) => received(config), {
      Config: Schema.object({ port: Schema.natural().default(8080) }),
    })

    await root.plugin(plugin, {})
    expect(received).toHaveBeenCalledWith({ port: 8080 })
  })
})
