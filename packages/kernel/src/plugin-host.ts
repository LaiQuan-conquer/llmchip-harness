import { Context, type Fiber } from 'cordis'
import type { ArtifactStore } from './artifacts/store.ts'
import type { EventStore } from './events/store.ts'
import type { Logger } from './util/logger.ts'

/** Kernel services available to in-kernel plugins as `ctx.kernel`. */
export interface KernelServices {
  events: EventStore
  artifacts: ArtifactStore
  logger: Logger
}

declare module 'cordis' {
  interface Context {
    kernel: KernelServices
  }
}

/**
 * An in-kernel system plugin (architecture §4.9): TypeScript code shipped with
 * the product and reviewed, running inside harnessd. Everything else runs out
 * of process.
 */
export interface InKernelPlugin {
  readonly id: string
  /** Cordis services, besides `kernel`, that must be provided before `apply` runs. */
  readonly inject?: readonly string[]
  /** Cordis services this plugin provides. Plugins injecting them are unloaded first. */
  readonly provides?: readonly string[]
  apply(ctx: Context, kernel: KernelServices): void | Promise<void>
}

const KERNEL_ACTOR = { kind: 'kernel', id: 'plugin-host' } as const

interface Loaded {
  plugin: InKernelPlugin
  fiber: Fiber
}

/**
 * Hosts in-kernel plugins on Cordis (ADR-001). Cordis supplies dependency
 * injection and reversible effects. The host adds what the kernel needs on
 * top, each guarding against a Cordis behavior found in the M1a prototype
 * validation:
 *
 * - every load, failure and unload is an event;
 * - loading and unloading are bounded by timeouts, because Cordis waits for a
 *   plugin's setup to settle before disposing it, so a hung plugin would
 *   otherwise block shutdown forever;
 * - unloading is sequential in reverse load order, instead of Cordis's
 *   concurrent sibling disposal;
 * - unloading a provider first unloads the plugins that inject its services,
 *   because Cordis runs the provider's own cleanup before deactivating its
 *   dependents.
 */
export class PluginHost {
  readonly root: Context
  private readonly services: KernelServices
  private readonly loadTimeoutMs: number
  private readonly unloadTimeoutMs: number
  private readonly loadedPlugins = new Map<string, Loaded>()

  constructor(services: KernelServices, options: { loadTimeoutMs?: number; unloadTimeoutMs?: number } = {}) {
    this.services = services
    this.loadTimeoutMs = options.loadTimeoutMs ?? 10_000
    this.unloadTimeoutMs = options.unloadTimeoutMs ?? 10_000
    this.root = new Context()
    this.root.provide('kernel', services)
  }

  loaded(): string[] {
    return [...this.loadedPlugins.keys()]
  }

  /** Loads a plugin. Returns false, after recording `plugin.failed`, if it throws or times out. */
  async load(plugin: InKernelPlugin): Promise<boolean> {
    if (this.loadedPlugins.has(plugin.id)) throw new Error(`plugin ${plugin.id} is already loaded`)
    const fiber = this.root.plugin({
      name: plugin.id,
      inject: ['kernel', ...(plugin.inject ?? [])],
      apply: (ctx: Context) => plugin.apply(ctx, ctx.kernel),
    })
    try {
      await withTimeout(fiber, this.loadTimeoutMs, 'loading')
    } catch (error) {
      this.recordFailure(plugin.id, error)
      // Do not wait for a hung plugin; its effects are torn down in the background.
      void withTimeout(fiber.dispose(), this.unloadTimeoutMs, 'cleanup').catch(() => {})
      return false
    }
    this.loadedPlugins.set(plugin.id, { plugin, fiber })
    this.services.events.append({ kind: 'plugin.loaded', payload: { plugin: plugin.id }, actor: KERNEL_ACTOR })
    return true
  }

  /** Unloads a plugin, after unloading every plugin that injects a service it provides. */
  async unload(id: string): Promise<void> {
    const entry = this.loadedPlugins.get(id)
    if (!entry) return
    const provided = new Set(entry.plugin.provides ?? [])
    const dependents = [...this.loadedPlugins.values()]
      .filter((other) => other !== entry && (other.plugin.inject ?? []).some((name) => provided.has(name)))
      .reverse()
    for (const dependent of dependents) await this.unload(dependent.plugin.id)

    this.loadedPlugins.delete(id)
    try {
      await withTimeout(entry.fiber.dispose(), this.unloadTimeoutMs, 'cleanup')
    } catch (error) {
      this.recordFailure(id, error)
    }
    this.services.events.append({ kind: 'plugin.unloaded', payload: { plugin: id }, actor: KERNEL_ACTOR })
  }

  /** Unloads every plugin in reverse load order, then the host itself. */
  async dispose(): Promise<void> {
    for (const id of [...this.loadedPlugins.keys()].reverse()) await this.unload(id)
    // The root fiber still owns plugins that failed to load; bound their teardown too.
    await withTimeout(this.root.fiber.dispose(), this.unloadTimeoutMs, 'cleanup').catch((error) =>
      this.services.logger.error('plugin host cleanup failed', { error: String(error) }),
    )
  }

  private recordFailure(plugin: string, error: unknown): void {
    const message = error instanceof Error ? error.message : String(error)
    this.services.logger.error('plugin failed', { plugin, error: message })
    this.services.events.append({ kind: 'plugin.failed', payload: { plugin, error: message }, actor: KERNEL_ACTOR })
  }
}

async function withTimeout<T>(promise: PromiseLike<T>, ms: number, what: string): Promise<T> {
  let timer: NodeJS.Timeout | undefined
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${what} timed out after ${ms} ms`)), ms)
      }),
    ])
  } finally {
    clearTimeout(timer)
  }
}
