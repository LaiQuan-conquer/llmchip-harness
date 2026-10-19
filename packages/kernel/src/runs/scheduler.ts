import { constants, copyFileSync, chmodSync, createReadStream, existsSync, mkdirSync, readFileSync, appendFileSync, rmSync, statSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Ajv2020, type ValidateFunction } from 'ajv/dist/2020.js'
import {
  KERNEL_API_VERSION,
  PLUGIN_PROTOCOL_VERSION,
  maxLabel,
  type Actor,
  type ExecutionStatus,
  type Label,
  type ProtocolInitializeResult,
  type ProtocolInvokeParams,
  type ProtocolInvokeResult,
  type RunRecord,
  type RunRef,
  type RunsStartParams,
} from '@llmchip/kernel-api'
import type { ArtifactStore } from '../artifacts/store.ts'
import type { ResolvedConfig } from '../config/config.ts'
import type { HomeLayout } from '../config/home.ts'
import type { EventStore, Projection } from '../events/store.ts'
import type { PluginRegistry, RegisteredPlugin } from '../plugins/registry.ts'
import { PluginCallError, PluginProcess } from '../plugins/process.ts'
import { KernelError } from '../util/errors.ts'
import type { Logger } from '../util/logger.ts'
import { assertValid } from '../util/schema.ts'
import { ulid } from '../util/ulid.ts'

const KERNEL_ACTOR: Actor = { kind: 'kernel', id: 'scheduler' }

/** Source of the Python plugin SDK, mounted into containers (packages/kernel/{src,lib}/runs/ in the repository). */
const PYTHON_SDK = fileURLToPath(new URL('../../../../python/llmchip-sdk/src', import.meta.url))

/** Executions after which a run is retried if it has attempts left. */
const RETRYABLE: ReadonlySet<ExecutionStatus> = new Set(['interrupted', 'crashed'])

/** Projection of `run.*` events: the run table, which doubles as the persistent queue. */
export const runsProjection: Projection = {
  name: 'runs',
  version: 1,
  setup(db) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        project TEXT,
        state TEXT NOT NULL,
        pid INTEGER,
        record TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS runs_state ON runs (state);
    `)
  },
  reset(db) {
    db.exec('DROP TABLE IF EXISTS runs')
  },
  apply(event, db) {
    if (event.kind === 'run.queued') {
      const run = (event.payload as { run: RunRecord }).run
      db.prepare('INSERT INTO runs (id, seq, project, state, pid, record) VALUES (?, ?, ?, ?, NULL, ?)').run(
        run.id,
        event.seq,
        run.project,
        run.state,
        JSON.stringify(run),
      )
      return
    }
    if (event.kind !== 'run.started' && event.kind !== 'run.finished') return
    const payload = event.payload as Record<string, any>
    const row = db.prepare('SELECT record FROM runs WHERE id = ?').get(payload.run) as { record: string } | undefined
    if (!row) return
    const run = JSON.parse(row.record) as RunRecord
    if (event.kind === 'run.started') {
      Object.assign(run, { state: 'running', execution: 'running', startedAt: event.ts })
      db.prepare('UPDATE runs SET state = ?, pid = ?, record = ? WHERE id = ?').run('running', payload.pid, JSON.stringify(run), run.id)
    } else {
      const { execution, exitCode, signal, outputs, metrics, parse, error, log } = payload
      Object.assign(run, { state: 'finished', execution, exitCode, signal, outputs, metrics, parse, error, log, endedAt: event.ts })
      db.prepare('UPDATE runs SET state = ?, record = ? WHERE id = ?').run('finished', JSON.stringify(run), run.id)
    }
  },
}

interface Active {
  run: RunRecord
  process?: PluginProcess
  /** Why the kernel is stopping the run, if it is. */
  abort?: ExecutionStatus
  progress?: { fraction?: number; message?: string }
}

/**
 * K4: executes runs and supervises their processes (architecture §4.4). The
 * queue is the `runs` projection, so it survives restarts. Each run gets a
 * fresh workspace (`in/` read-only copies of the inputs, `out/`, `work/`), its
 * own process group, a timeout and cancellation. Outputs are registered as
 * artifacts only after the run succeeded (atomic run commit, §4.3).
 */
export class Scheduler {
  private readonly events: EventStore
  private readonly artifacts: ArtifactStore
  private readonly registry: PluginRegistry
  private readonly layout: HomeLayout
  private readonly config: ResolvedConfig
  private readonly logger: Logger
  private readonly redact: (text: string) => string
  private readonly active = new Map<string, Active>()
  private readonly waiters = new Map<string, Set<(run: RunRecord) => void>>()
  private readonly paramValidators = new Map<string, ValidateFunction>()
  private readonly ajv = new Ajv2020({ strict: false, allErrors: true, useDefaults: true })
  private stopped = false

  constructor(options: {
    events: EventStore
    artifacts: ArtifactStore
    registry: PluginRegistry
    layout: HomeLayout
    config: ResolvedConfig
    logger: Logger
    redact: (text: string) => string
  }) {
    this.events = options.events
    this.artifacts = options.artifacts
    this.registry = options.registry
    this.layout = options.layout
    this.config = options.config
    this.logger = options.logger
    this.redact = options.redact
    this.events.registerProjection(runsProjection)
  }

  get paused(): boolean {
    const row = this.events.db.prepare("SELECT payload FROM events WHERE kind = 'kernel.paused' ORDER BY seq DESC LIMIT 1").get() as
      | { payload: string }
      | undefined
    return row ? JSON.parse(row.payload).paused : false
  }

  /**
   * Startup recovery (architecture §11): runs recorded as running belong to a
   * kernel that died. Their process groups are killed, they are finished as
   * `interrupted`, and retried if they have attempts left.
   */
  recover(): void {
    for (const row of this.events.db.prepare("SELECT record, pid FROM runs WHERE state = 'running'").all() as { record: string; pid: number | null }[]) {
      const run = JSON.parse(row.record) as RunRecord
      if (row.pid) killOrphan(row.pid, run.id)
      this.removeContainer(run.id, 'rm')
      this.finish(run, { execution: 'interrupted', error: 'the kernel stopped while the run was in progress' })
    }
    // No run is active yet, so every workspace left on disk is stale.
    rmSync(this.layout.runs, { recursive: true, force: true })
    mkdirSync(this.layout.runs, { recursive: true })
  }

  start(params: RunsStartParams, actor: Actor, retryOf?: RunRecord): RunRecord {
    const { plugin, spec } = this.registry.handler(params.plugin, params.handler)
    if (plugin.manifest.runtime === 'data') throw new KernelError('PLUGIN_NOT_EXECUTABLE', `plugin ${plugin.manifest.id} has no code`)
    const runParams = structuredClone(params.params ?? {})
    if (spec.params) {
      const key = `${plugin.manifest.id}/${plugin.manifest.version}/${spec.name}`
      let validate = this.paramValidators.get(key)
      if (!validate) this.paramValidators.set(key, (validate = this.ajv.compile(spec.params)))
      if (!validate(runParams)) {
        const errors = (validate.errors ?? []).map((e) => `${e.instancePath || '/'} ${e.message}`)
        throw new KernelError('RUN_PARAMS_INVALID', `invalid params for ${params.plugin}/${params.handler}: ${errors.join('; ')}`, { errors })
      }
    }
    const inputs = params.inputs ?? []
    const labels: Label[] = []
    for (const input of inputs) {
      const meta = this.artifacts.require(input.hash)
      const inputSpec = spec.inputs?.find((i) => i.name === input.name)
      if (spec.inputs && !inputSpec) throw new KernelError('RUN_INPUT_UNKNOWN', `handler ${spec.name} has no input ${input.name}`, { input: input.name })
      if (inputSpec && !inputSpec.types.includes(meta.type)) {
        throw new KernelError('RUN_INPUT_TYPE', `input ${input.name} must be one of ${inputSpec.types.join(', ')}, got ${meta.type}`, {
          input: input.name,
          type: meta.type,
        })
      }
      labels.push(meta.label)
    }
    for (const inputSpec of spec.inputs ?? []) {
      const count = inputs.filter((i) => i.name === inputSpec.name).length
      if (inputSpec.required !== false && count === 0) throw new KernelError('RUN_INPUT_MISSING', `missing input ${inputSpec.name}`, { input: inputSpec.name })
      if (!inputSpec.multiple && count > 1) throw new KernelError('RUN_INPUT_MULTIPLE', `input ${inputSpec.name} takes one artifact`, { input: inputSpec.name })
    }

    const run: RunRecord = {
      id: ulid(),
      project: params.project ?? null,
      plugin: plugin.manifest.id,
      handler: spec.name,
      params: runParams,
      inputs,
      outputs: [],
      state: 'queued',
      execution: 'not_started',
      exitCode: null,
      signal: null,
      metrics: {},
      parse: null,
      error: null,
      log: null,
      label: maxLabel(labels),
      attempt: retryOf ? retryOf.attempt + 1 : 1,
      maxAttempts: params.maxAttempts ?? retryOf?.maxAttempts ?? 1,
      retryOf: retryOf?.id ?? null,
      timeoutMs: params.timeoutMs ?? spec.timeoutMs ?? this.config.scheduler.defaultTimeoutMs,
      queuedAt: new Date().toISOString(),
      startedAt: null,
      endedAt: null,
    }
    this.events.append({ kind: 'run.queued', payload: { run }, actor, project: run.project, label: run.label })
    setImmediate(() => this.pump())
    return this.get(run.id)
  }

  get(id: string): RunRecord {
    const row = this.events.db.prepare('SELECT record FROM runs WHERE id = ?').get(id) as { record: string } | undefined
    if (!row) throw new KernelError('RUN_NOT_FOUND', `run ${id} does not exist`, { run: id })
    return JSON.parse(row.record)
  }

  list(options: { project?: string; state?: string; limit?: number } = {}): RunRecord[] {
    const where: string[] = []
    const args: (string | number)[] = []
    if (options.project) {
      where.push('project = ?')
      args.push(options.project)
    }
    if (options.state) {
      where.push('state = ?')
      args.push(options.state)
    }
    args.push(options.limit ?? 100)
    const sql = `SELECT record FROM runs ${where.length ? `WHERE ${where.join(' AND ')}` : ''} ORDER BY seq LIMIT ?`
    return (this.events.db.prepare(sql).all(...args) as { record: string }[]).map((r) => JSON.parse(r.record))
  }

  progress(id: string): { fraction?: number; message?: string } | undefined {
    return this.active.get(id)?.progress
  }

  /** Resolves when the run has finished, or with its current state after `timeoutMs`. */
  wait(id: string, timeoutMs: number): Promise<RunRecord> {
    const run = this.get(id)
    if (run.state === 'finished') return Promise.resolve(run)
    return new Promise((resolve) => {
      const set = this.waiters.get(id) ?? new Set()
      this.waiters.set(id, set)
      const done = (finished: RunRecord) => {
        clearTimeout(timer)
        set.delete(done)
        resolve(finished)
      }
      const timer = setTimeout(() => {
        set.delete(done)
        resolve(this.get(id))
      }, timeoutMs)
      set.add(done)
    })
  }

  cancel(id: string, actor: Actor): RunRecord {
    const run = this.get(id)
    if (run.state === 'finished') return run
    const active = this.active.get(id)
    if (active) {
      this.abort(active, 'cancelled')
      return this.get(id)
    }
    this.finish(run, { execution: 'cancelled', error: `cancelled by ${actor.kind}:${actor.id}` })
    return this.get(id)
  }

  /** Emergency stop (FR-AGT-23): no new run is started until `resume`. Running runs continue. */
  setPaused(paused: boolean, reason: string, actor: Actor): boolean {
    if (this.paused !== paused) this.events.append({ kind: 'kernel.paused', payload: { paused, reason }, actor })
    if (!paused) setImmediate(() => this.pump())
    return paused
  }

  /** Stops dispatching and interrupts every running run (kernel shutdown). */
  async stop(): Promise<void> {
    this.stopped = true
    const running = [...this.active.values()]
    for (const active of running) this.abort(active, 'interrupted')
    await Promise.all(running.map((active) => active.process?.exited))
    // Let the executions record their results.
    while (this.active.size > 0) await new Promise((resolve) => setTimeout(resolve, 10))
  }

  /** Starts queued runs while capacity allows. */
  pump(): void {
    if (this.stopped || this.paused) return
    const capacity = this.config.scheduler.maxParallel - this.active.size
    if (capacity <= 0) return
    const queued = this.events.db
      .prepare("SELECT record FROM runs WHERE state = 'queued' ORDER BY seq LIMIT ?")
      .all(capacity + this.active.size) as { record: string }[]
    for (const { record } of queued) {
      const run = JSON.parse(record) as RunRecord
      if (this.active.has(run.id) || this.active.size >= this.config.scheduler.maxParallel) continue
      const active: Active = { run }
      this.active.set(run.id, active)
      void this.execute(active)
        .catch((error) => this.logger.error('run execution failed', { run: run.id, error: String(error) }))
        .finally(() => {
          this.active.delete(run.id)
          this.pump()
        })
    }
  }

  private abort(active: Active, reason: ExecutionStatus): void {
    if (active.abort) return
    active.abort = reason
    const process = active.process
    if (!process) return
    process.notify('cancel', { run: active.run.id })
    if (this.registry.get(active.run.plugin)?.manifest.runtime === 'container') this.removeContainer(active.run.id, 'kill')
    process.kill('SIGTERM')
    const timer = setTimeout(() => process.kill('SIGKILL'), this.config.scheduler.cancelGraceMs)
    void process.exited.then(() => clearTimeout(timer))
  }

  private async execute(active: Active): Promise<void> {
    const { run } = active
    let plugin: RegisteredPlugin
    try {
      plugin = this.registry.require(run.plugin)
    } catch (error) {
      this.finish(run, { execution: 'crashed', error: (error as Error).message })
      return
    }
    const dir = join(this.layout.runs, run.id)
    const paths = { in: join(dir, 'in'), out: join(dir, 'out'), work: join(dir, 'work'), log: join(dir, 'log.txt') }
    rmSync(dir, { recursive: true, force: true })
    for (const path of [paths.in, paths.out, paths.work]) mkdirSync(path, { recursive: true })
    const log = (level: string, message: string) =>
      appendFileSync(paths.log, this.redact(`${new Date().toISOString()} ${level.padEnd(5)} ${message}`) + '\n')

    const inputs = run.inputs.map((input, index) => {
      const meta = this.artifacts.require(input.hash)
      const path = join(paths.in, `${index}-${input.name}`)
      // Copy (reflink where the file system supports it) so a plugin cannot alter stored content.
      copyFileSync(this.artifacts.objectPath(input.hash), path, constants.COPYFILE_FICLONE)
      chmodSync(path, 0o444)
      return { name: input.name, hash: input.hash, type: meta.type, label: meta.label, path }
    })

    const container = plugin.manifest.runtime === 'container'
    // Inside a container the run workspace and the plugin are mounted at fixed paths.
    const at = container
      ? { in: '/llmchip/in', out: '/llmchip/out', work: '/llmchip/work', plugin: '/llmchip/plugin', python: 'python3', node: 'node' }
      : { in: paths.in, out: paths.out, work: paths.work, plugin: plugin.dir, python: this.config.plugins.python ?? 'python3', node: this.config.plugins.node ?? process.execPath }
    let command = (plugin.manifest.entry?.command ?? []).map((part) =>
      part.replaceAll('${python}', at.python).replaceAll('${node}', at.node).replaceAll('${pluginDir}', at.plugin),
    )
    if (container) command = this.containerCommand(run, plugin, paths, command)
    const invokeInputs = inputs.map((input) => ({ ...input, path: input.path.replace(paths.in, at.in) }))

    const process_ = PluginProcess.spawn({
      command,
      cwd: paths.work,
      env: { ...childEnv(), LLMCHIP_RUN: run.id, LLMCHIP_RUN_DIR: dir },
      onLog: (level, message) => log(level, message),
      onNotification: (method, params) => {
        const p = params as { run?: string; fraction?: number; message?: string; level?: string }
        if (method === 'progress' && p.run === run.id) active.progress = { ...(p.fraction === undefined ? {} : { fraction: p.fraction }), ...(p.message ? { message: p.message } : {}) }
        else if (method === 'log') log(p.level ?? 'info', p.message ?? '')
      },
    })
    active.process = process_
    if (active.abort) {
      // Cancelled while the workspace was being prepared.
      const reason = active.abort
      active.abort = undefined
      this.abort(active, reason)
    }
    this.events.append({ kind: 'run.started', payload: { run: run.id, pid: process_.pid ?? null }, actor: KERNEL_ACTOR, project: run.project, label: run.label })

    const timer = setTimeout(() => this.abort(active, 'timed_out'), run.timeoutMs)
    let result: ProtocolInvokeResult | undefined
    let error: string | null = null
    /** The plugin answered `invoke` with an error, as opposed to dying or breaking the protocol. */
    let pluginError = false
    try {
      const init = await process_.request<ProtocolInitializeResult>('initialize', {
        protocolVersion: PLUGIN_PROTOCOL_VERSION,
        kernelApiVersion: KERNEL_API_VERSION,
        pluginDir: plugin.dir,
      })
      if (init.id !== plugin.manifest.id) throw new Error(`plugin process identifies as ${init.id}, expected ${plugin.manifest.id}`)
      const invoke: ProtocolInvokeParams = { run: run.id, handler: run.handler, params: run.params, inputs: invokeInputs, outDir: at.out, workDir: at.work }
      result = await process_.request<ProtocolInvokeResult>('invoke', invoke)
      assertValid('ProtocolInvokeResult', result, 'PLUGIN_RESULT_INVALID')
      await Promise.race([process_.request('shutdown', {}).catch(() => {}), process_.exited])
    } catch (caught) {
      error = caught instanceof Error ? caught.message : String(caught)
      pluginError = caught instanceof PluginCallError
      result = undefined
    } finally {
      clearTimeout(timer)
    }
    // Give a well-behaved plugin a moment to exit after shutdown, then make sure the group is gone.
    const exited = await Promise.race([process_.exited, new Promise<undefined>((r) => setTimeout(() => r(undefined), this.config.scheduler.cancelGraceMs))])
    process_.kill('SIGKILL')
    const status = exited ?? (await process_.exited)

    let execution: ExecutionStatus
    if (active.abort) execution = active.abort
    else if (result) execution = exited === undefined || status.code === 0 ? 'exited_zero' : 'exited_nonzero'
    else if (pluginError) execution = 'exited_nonzero'
    else execution = 'crashed'

    let outputs: RunRef[] = []
    let finalError = active.abort ? `${active.abort} by the kernel` : error
    if (execution === 'exited_zero' && result) {
      try {
        outputs = await this.registerOutputs(run, plugin, result, paths.out)
      } catch (caught) {
        execution = 'exited_nonzero'
        finalError = (caught as Error).message
      }
    }
    log('info', `run finished: ${execution}${finalError ? ` (${finalError})` : ''}`)
    const logMeta = await this.artifacts.put(readFileSync(paths.log), {
      type: 'run.log',
      mediaType: 'text/plain',
      label: run.label,
      inputs: [...new Set(run.inputs.map((i) => i.hash))],
      run: run.id,
      creator: { kind: 'plugin', id: run.plugin },
      project: run.project,
    })
    this.finish(run, {
      execution,
      exitCode: status.code,
      signal: status.signal,
      outputs,
      metrics: execution === 'exited_zero' ? (result?.metrics ?? {}) : {},
      parse: execution === 'exited_zero' ? (result?.parse ?? null) : null,
      error: finalError,
      log: logMeta.hash,
    })
    rmSync(dir, { recursive: true, force: true })
  }

  /**
   * Wraps a plugin command in the container engine (architecture §9.3): no
   * network, CPU and memory limits, inputs and the plugin read-only, only the
   * run workspace writable. The Python SDK is mounted for Python plugins.
   */
  private containerCommand(run: RunRecord, plugin: RegisteredPlugin, paths: { in: string; out: string; work: string }, command: string[]): string[] {
    const { containerRuntime, containerCpus, containerMemory } = this.config.scheduler
    const uid = process.getuid?.() ?? 0
    const gid = process.getgid?.() ?? 0
    const [entrypoint, ...args] = command
    return [
      containerRuntime,
      'run',
      '--rm',
      '-i',
      `--name=llmchip-run-${run.id.toLowerCase()}`,
      `--label=llmchip.run=${run.id}`,
      '--network=none',
      `--cpus=${containerCpus}`,
      `--memory=${containerMemory}`,
      `--user=${uid}:${gid}`,
      ...(uid === 0 ? [] : ['--userns=keep-id']),
      '--security-opt=label=disable',
      `--volume=${paths.in}:/llmchip/in:ro`,
      `--volume=${paths.out}:/llmchip/out`,
      `--volume=${paths.work}:/llmchip/work`,
      `--volume=${plugin.dir}:/llmchip/plugin:ro`,
      `--volume=${PYTHON_SDK}:/llmchip/sdk:ro`,
      '--env=PYTHONPATH=/llmchip/sdk',
      '--env=HOME=/llmchip/work',
      '--workdir=/llmchip/work',
      `--entrypoint=${entrypoint}`,
      plugin.manifest.entry!.image!,
      ...args,
    ]
  }

  private removeContainer(runId: string, how: 'kill' | 'rm'): void {
    try {
      const name = `llmchip-run-${runId.toLowerCase()}`
      execFileSync(this.config.scheduler.containerRuntime, how === 'kill' ? ['kill', name] : ['rm', '-f', name], { stdio: 'ignore', timeout: 30_000 })
    } catch {
      // No such container, or no container engine installed.
    }
  }

  private async registerOutputs(run: RunRecord, plugin: RegisteredPlugin, result: ProtocolInvokeResult, outDir: string): Promise<RunRef[]> {
    const spec = plugin.manifest.handlers?.find((h) => h.name === run.handler)
    const refs: RunRef[] = []
    for (const output of result.outputs) {
      const path = resolve(outDir, output.path)
      if (relative(outDir, path).startsWith('..') || !existsSync(path) || !statSync(path).isFile()) {
        throw new KernelError('RUN_OUTPUT_INVALID', `output ${output.name} (${output.path}) is not a file in the run's out directory`)
      }
      const declared = spec?.outputs?.find((o) => o.name === output.name)
      if (spec?.outputs && (!declared || declared.type !== output.type)) {
        throw new KernelError('RUN_OUTPUT_INVALID', `output ${output.name} of type ${output.type} is not declared by ${plugin.manifest.id}/${run.handler}`)
      }
      // Streamed: outputs can be multi-GB weight files.
      const meta = await this.artifacts.put(createReadStream(path), {
        type: output.type,
        mediaType: output.mediaType ?? declared?.mediaType ?? 'application/octet-stream',
        // Never below the run's own label (requirements FR-SEC-02).
        label: maxLabel([run.label, output.label ?? run.label]),
        inputs: [...new Set(run.inputs.map((i) => i.hash))],
        run: run.id,
        creator: { kind: 'plugin', id: plugin.manifest.id },
        project: run.project,
      })
      refs.push({ name: output.name, hash: meta.hash })
    }
    return refs
  }

  private finish(
    run: RunRecord,
    result: Partial<Pick<RunRecord, 'exitCode' | 'signal' | 'outputs' | 'metrics' | 'parse' | 'error' | 'log'>> & { execution: ExecutionStatus },
  ): void {
    this.events.transaction(() => {
      this.events.append({
        kind: 'run.finished',
        payload: {
          run: run.id,
          execution: result.execution,
          exitCode: result.exitCode ?? null,
          signal: result.signal ?? null,
          outputs: result.outputs ?? [],
          metrics: result.metrics ?? {},
          parse: result.parse ?? null,
          error: result.error ?? null,
          log: result.log ?? null,
        },
        actor: KERNEL_ACTOR,
        project: run.project,
        label: run.label,
      })
      if (RETRYABLE.has(result.execution) && run.attempt < run.maxAttempts) {
        const { plugin, handler, params, inputs, timeoutMs } = run
        try {
          this.start({ plugin, handler, params, inputs, timeoutMs, ...(run.project ? { project: run.project } : {}) }, KERNEL_ACTOR, run)
        } catch (error) {
          this.logger.error('cannot retry run', { run: run.id, error: String(error) })
        }
      }
    })
    const finished = this.get(run.id)
    for (const waiter of this.waiters.get(run.id) ?? []) waiter(finished)
    this.waiters.delete(run.id)
  }
}

/** Environment passed to plugin processes: no secrets, only what a process needs to run. */
function childEnv(): Record<string, string> {
  const keep = ['PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'TZ', 'SYSTEMROOT', 'VIRTUAL_ENV']
  const env: Record<string, string> = {}
  for (const key of keep) if (process.env[key]) env[key] = process.env[key]!
  return env
}

/**
 * Kills the process group of a run left over by a dead kernel, but only if
 * the process still carries the run's id in its environment, so a reused pid
 * is never killed by mistake.
 */
function killOrphan(pid: number, runId: string): void {
  try {
    const environ = readFileSync(`/proc/${pid}/environ`, 'utf8')
    if (!environ.split('\0').includes(`LLMCHIP_RUN=${runId}`)) return
    process.kill(-pid, 'SIGKILL')
  } catch {
    // Not running any more, or not inspectable.
  }
}
