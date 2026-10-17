import { randomBytes } from 'node:crypto'
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { KERNEL_API_VERSION, type Actor, type KernelInfo, type WorkspaceConfig } from '@llmchip/kernel-api'
import { ArtifactStore } from './artifacts/store.ts'
import { loadConfig, type ResolvedConfig } from './config/config.ts'
import { ensureHome, homeLayout, resolveHome, type HomeLayout } from './config/home.ts'
import { Redactor, SecretStore } from './config/secrets.ts'
import { EventStore } from './events/store.ts'
import { PluginHost, type InKernelPlugin } from './plugin-host.ts'
import { PluginRegistry } from './plugins/registry.ts'
import { Scheduler } from './runs/scheduler.ts'
import { Workflow } from './workflow/engine.ts'
import { KernelServer, type MethodHandlers } from './server.ts'
import { KernelError } from './util/errors.ts'
import { createLogger, type Logger } from './util/logger.ts'

export interface KernelOptions {
  home?: string
  /** Highest-priority configuration layer, e.g. from command-line flags. */
  config?: WorkspaceConfig
  plugins?: InKernelPlugin[]
  /** How long a plugin may take to load, or to clean up, before it is recorded as failed. */
  pluginTimeoutMs?: number
  /** Receives log lines in addition to the log file. */
  console?: (line: string) => void
}

/** Contents of `<home>/run/harnessd.json`, how clients find the running kernel. */
export interface RuntimeInfo {
  pid: number
  url: string
  token: string
  version: string
  startedAt: string
}

const KERNEL_ACTOR: Actor = { kind: 'kernel', id: 'harnessd' }

// Plugins and the Python environment shipped with the workbench, relative to this package
// (packages/kernel/{src,lib}/ in the repository).
const BUNDLED_PLUGINS = fileURLToPath(new URL('../../../plugins', import.meta.url))
const BUNDLED_PYTHON = fileURLToPath(new URL('../../../.venv/bin/python', import.meta.url))

export function kernelVersion(): string {
  // src/ and lib/ are both one level below the package root.
  return JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version
}

/** Reads the runtime file of the kernel running in `home`, if any. */
export function readRuntimeInfo(home?: string): RuntimeInfo | undefined {
  const path = join(homeLayout(resolveHome(home)).run, 'harnessd.json')
  return existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : undefined
}

/**
 * The microkernel process, harnessd (architecture §4). Assembles the kernel
 * components in dependency order and shuts them down in the reverse, explicit
 * order of architecture §5 item 5: stop accepting requests, unload plugins,
 * record the stop, close storage.
 */
export class Kernel {
  readonly layout: HomeLayout
  readonly config: ResolvedConfig
  readonly logger: Logger
  readonly secrets: SecretStore
  readonly events: EventStore
  readonly artifacts: ArtifactStore
  readonly plugins: PluginHost
  readonly registry: PluginRegistry
  readonly scheduler: Scheduler
  readonly workflow: Workflow
  readonly startedAt = new Date().toISOString()
  readonly version = kernelVersion()
  /** Resolves once the kernel has fully stopped. */
  readonly stopped: Promise<void>
  url = ''
  private server?: KernelServer
  private stopping?: Promise<void>
  private resolveStopped!: () => void

  private constructor(options: KernelOptions) {
    this.stopped = new Promise((resolve) => (this.resolveStopped = resolve))
    this.layout = homeLayout(resolveHome(options.home))
    ensureHome(this.layout)
    acquireLock(this.layout)
    try {
      this.config = loadConfig(this.layout.config, options.config)
      this.config.plugins.python ??= existsSync(BUNDLED_PYTHON) ? BUNDLED_PYTHON : 'python3'
      this.secrets = new SecretStore(this.layout.secrets)
      const redactor = new Redactor(() => this.secrets.entries())
      this.logger = createLogger({
        file: join(this.layout.logs, 'harnessd.log'),
        redact: (text) => redactor.text(text),
        ...(options.console ? { console: options.console } : {}),
      })
      this.events = new EventStore(join(this.layout.db, 'harness.sqlite'), { redact: (payload) => redactor.value(payload) })
      this.secrets.bindEvents(this.events)
      const integrity = this.events.verify({ tail: this.config.events.verifyTailOnStart })
      if (!integrity.ok) {
        throw new KernelError('EVENT_LOG_CORRUPT', 'the event log hash chain is broken; run `llmchip events verify`', {
          problems: integrity.problems,
        })
      }
      this.artifacts = new ArtifactStore(this.layout.store, this.events)
      const removed = this.artifacts.cleanupTemp()
      if (removed > 0) this.logger.warn('removed interrupted artifact writes', { count: removed })
      this.registry = new PluginRegistry({
        searchPaths: [...(this.config.plugins.bundled ? [BUNDLED_PLUGINS] : []), this.layout.plugins, ...this.config.plugins.searchPaths],
        artifacts: this.artifacts,
        logger: this.logger,
      })
      this.scheduler = new Scheduler({
        events: this.events,
        artifacts: this.artifacts,
        registry: this.registry,
        layout: this.layout,
        config: this.config,
        logger: this.logger,
        redact: (text) => redactor.text(text),
      })
      this.workflow = new Workflow({ events: this.events, artifacts: this.artifacts, registry: this.registry, scheduler: this.scheduler })
      this.plugins = new PluginHost(
        { events: this.events, artifacts: this.artifacts, logger: this.logger },
        options.pluginTimeoutMs === undefined ? {} : { loadTimeoutMs: options.pluginTimeoutMs, unloadTimeoutMs: options.pluginTimeoutMs },
      )
    } catch (error) {
      releaseLock(this.layout)
      throw error
    }
  }

  static async start(options: KernelOptions = {}): Promise<Kernel> {
    const kernel = new Kernel(options)
    try {
      kernel.events.append({
        kind: 'kernel.started',
        payload: { version: kernel.version, apiVersion: KERNEL_API_VERSION, pid: process.pid },
        actor: KERNEL_ACTOR,
      })
      for (const problem of [...(await kernel.registry.scan()), ...kernel.workflow.loadFlows()]) {
        kernel.logger.warn('plugin problem', { problem })
      }
      kernel.scheduler.recover()
      for (const plugin of options.plugins ?? []) await kernel.plugins.load(plugin)
      const token = randomBytes(32).toString('base64url')
      kernel.server = new KernelServer({
        token,
        handlers: kernel.handlers(),
        events: kernel.events,
        artifacts: kernel.artifacts,
        logger: kernel.logger,
      })
      kernel.url = await kernel.server.listen(kernel.config.server.host, kernel.config.server.port)
      const runtime: RuntimeInfo = { pid: process.pid, url: kernel.url, token, version: kernel.version, startedAt: kernel.startedAt }
      writeFileSync(join(kernel.layout.run, 'harnessd.json'), JSON.stringify(runtime, null, 2), { mode: 0o600 })
      kernel.scheduler.pump()
      kernel.logger.info('harnessd started', { url: kernel.url, home: kernel.layout.root })
    } catch (error) {
      await kernel.stop('startup failed')
      throw error
    }
    return kernel
  }

  info(): KernelInfo {
    return {
      name: 'harnessd',
      version: this.version,
      apiVersion: KERNEL_API_VERSION,
      pid: process.pid,
      startedAt: this.startedAt,
      head: this.events.head(),
      paused: this.scheduler.paused,
    }
  }

  /** Stops the kernel. Idempotent; concurrent callers share one shutdown. */
  stop(reason = 'shutdown'): Promise<void> {
    this.stopping ??= this.shutdown(reason)
    return this.stopping
  }

  private async shutdown(reason: string): Promise<void> {
    try {
      rmSync(join(this.layout.run, 'harnessd.json'), { force: true })
      await this.server?.close()
      await this.scheduler.stop()
      await this.plugins.dispose()
      this.events.append({ kind: 'kernel.stopped', payload: { reason }, actor: KERNEL_ACTOR })
      this.logger.info('harnessd stopped', { reason })
    } finally {
      this.events.close()
      releaseLock(this.layout)
      this.resolveStopped()
    }
  }

  private handlers(): MethodHandlers {
    return {
      'kernel.info': () => this.info(),
      'kernel.shutdown': () => {
        setImmediate(() => void this.stop('requested by client'))
        return { ok: true }
      },
      'events.list': (params) => ({ events: this.events.list(params), head: this.events.head() }),
      'events.verify': () => this.events.verify(),
      'artifacts.stat': ({ hash }) => this.artifacts.require(hash),
      'artifacts.list': (params) => ({ artifacts: this.artifacts.list(params) }),
      'artifacts.lineage': ({ hash, direction }) => this.artifacts.lineage(hash, direction),
      'artifacts.markDeleted': ({ hash, reason }, session) => this.artifacts.markDeleted(hash, reason, session.actor),
      'artifacts.verify': () => this.artifacts.verify(),
      'projections.rebuild': (_params, session) => {
        const rebuilt = this.events.rebuildProjections()
        for (const payload of rebuilt) this.events.append({ kind: 'projection.rebuilt', payload: { ...payload }, actor: session.actor })
        return { rebuilt }
      },
      'secrets.set': ({ name, value }, session) => {
        this.secrets.set(name, value, session.actor)
        return { ok: true }
      },
      'secrets.remove': ({ name }, session) => {
        this.secrets.remove(name, session.actor)
        return { ok: true }
      },
      'secrets.list': () => ({ names: this.secrets.list() }),
      'kernel.pause': ({ reason }, session) => ({ paused: this.scheduler.setPaused(true, reason ?? 'requested', session.actor) }),
      'kernel.resume': (_params, session) => ({ paused: this.scheduler.setPaused(false, 'resumed', session.actor) }),
      'plugins.list': () => this.registry.list(),
      'runs.start': (params, session) => this.scheduler.start(params, session.actor),
      'runs.get': ({ run }) => this.scheduler.get(run),
      'runs.wait': ({ run, timeoutMs }) => this.scheduler.wait(run, timeoutMs ?? 60_000),
      'runs.list': (params) => ({ runs: this.scheduler.list(params) }),
      'runs.cancel': ({ run }, session) => this.scheduler.cancel(run, session.actor),
      'checks.list': () => ({ checks: this.workflow.checks() }),
      'verdicts.evaluate': ({ check, run, project }, session) => this.workflow.evaluate(check, run, session.actor, project),
      'verdicts.list': (params) => ({ verdicts: this.workflow.verdicts(params) }),
      'projects.create': ({ id, title, flow }, session) => this.workflow.createProject(id, title, flow ?? 'default', session.actor),
      'projects.list': () => ({ projects: this.workflow.projects() }),
      'projects.get': ({ project, branch }) => this.workflow.project(project, branch),
      'projects.bind': ({ project, branch, slot, hash }, session) => {
        this.workflow.bind(project, branch ?? 'main', slot, hash, session.actor)
        return { ok: true }
      },
      'projects.branch': ({ project, from, name, atSeq }, session) => {
        this.workflow.branch(project, from ?? 'main', name, atSeq, session.actor)
        return { ok: true }
      },
      'stages.enter': ({ project, branch, stage }, session) => {
        this.workflow.enterStage(project, branch ?? 'main', stage, session.actor)
        return { ok: true }
      },
      'gates.snapshot': ({ project, branch, gate }) => this.workflow.snapshot(project, branch ?? 'main', gate),
      'gates.release': ({ project, branch, gate }) => this.workflow.release(project, branch ?? 'main', gate),
      'approvals.decide': (params, session) => this.workflow.decide(params, session.actor),
      'approvals.closeCondition': ({ approval, condition, verdict }, session) => this.workflow.closeCondition(approval, condition, verdict, session.actor),
      'approvals.list': ({ project, branch }) => ({ approvals: this.workflow.approvals(project, branch) }),
      'waivers.create': (params, session) => this.workflow.createWaiver(params, session.actor),
    }
  }
}

/**
 * Only one kernel may own a home: two writers would fork the event chain. The
 * lock file holds the owner's pid; a lock left by a dead process is taken over.
 */
function acquireLock(layout: HomeLayout): void {
  const path = join(layout.run, 'harnessd.lock')
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      writeFileSync(path, String(process.pid), { flag: 'wx', mode: 0o600 })
      return
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
      const pid = Number(readFileSync(path, 'utf8'))
      if (pid && (pid === process.pid || isAlive(pid))) {
        throw new KernelError('KERNEL_ALREADY_RUNNING', `harnessd (pid ${pid}) already owns ${layout.root}`, { pid })
      }
      rmSync(path, { force: true })
    }
  }
  throw new KernelError('KERNEL_LOCK_FAILED', `cannot acquire ${path}`)
}

function releaseLock(layout: HomeLayout): void {
  const path = join(layout.run, 'harnessd.lock')
  if (existsSync(path) && readFileSync(path, 'utf8') === String(process.pid)) rmSync(path, { force: true })
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM'
  }
}
