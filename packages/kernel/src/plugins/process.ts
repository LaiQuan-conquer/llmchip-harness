import { spawn, type ChildProcess } from 'node:child_process'
import { createInterface } from 'node:readline'

export interface PluginProcessOptions {
  command: string[]
  cwd: string
  env: Record<string, string>
  /** Receives stderr lines and protocol violations. */
  onLog(level: 'debug' | 'info' | 'warn' | 'error', message: string): void
  onNotification(method: string, params: unknown): void
}

export interface ExitStatus {
  code: number | null
  signal: string | null
}

/** A JSON-RPC error returned by a plugin. */
export class PluginCallError extends Error {
  readonly rpcCode: number

  constructor(message: string, rpcCode: number) {
    super(message)
    this.name = 'PluginCallError'
    this.rpcCode = rpcCode
  }
}

/** The plugin process exited before answering. */
export class PluginExitedError extends Error {
  readonly status: ExitStatus

  constructor(status: ExitStatus) {
    super(`plugin process exited (code ${status.code}, signal ${status.signal}) before answering`)
    this.name = 'PluginExitedError'
    this.status = status
  }
}

/**
 * One out-of-process plugin speaking IF-2: newline-delimited JSON-RPC 2.0 on
 * stdin/stdout, free-form log lines on stderr. The process leads its own
 * process group so that it and everything it spawned can be killed together.
 */
export class PluginProcess {
  readonly child: ChildProcess
  readonly exited: Promise<ExitStatus>
  private nextId = 1
  private readonly pending = new Map<number, { resolve(value: unknown): void; reject(error: Error): void }>()
  private exitStatus: ExitStatus | undefined

  private constructor(options: PluginProcessOptions) {
    const [file, ...args] = options.command
    this.child = spawn(file!, args, {
      cwd: options.cwd,
      env: options.env,
      stdio: ['pipe', 'pipe', 'pipe'],
      detached: true,
    })
    this.exited = new Promise((resolve) => {
      const finish = (status: ExitStatus) => {
        if (this.exitStatus) return
        this.exitStatus = status
        for (const pending of this.pending.values()) pending.reject(new PluginExitedError(status))
        this.pending.clear()
        resolve(status)
      }
      this.child.once('exit', (code, signal) => finish({ code, signal }))
      this.child.once('error', (error) => {
        options.onLog('error', `cannot start plugin process: ${error.message}`)
        finish({ code: null, signal: null })
      })
    })
    this.child.stdin!.on('error', () => {})
    createInterface({ input: this.child.stderr! }).on('line', (line) => options.onLog('info', line))
    createInterface({ input: this.child.stdout! }).on('line', (line) => {
      if (!line.trim()) return
      let message: { id?: number; method?: string; params?: unknown; result?: unknown; error?: { code: number; message: string } }
      try {
        message = JSON.parse(line)
      } catch {
        options.onLog('warn', `protocol violation, non-JSON line on stdout: ${line.slice(0, 200)}`)
        return
      }
      if (message.method && message.id === undefined) return options.onNotification(message.method, message.params)
      const pending = message.id === undefined ? undefined : this.pending.get(message.id)
      if (!pending) return
      this.pending.delete(message.id!)
      if (message.error) pending.reject(new PluginCallError(message.error.message, message.error.code))
      else pending.resolve(message.result)
    })
  }

  static spawn(options: PluginProcessOptions): PluginProcess {
    return new PluginProcess(options)
  }

  get pid(): number | undefined {
    return this.child.pid
  }

  request<T>(method: string, params: unknown): Promise<T> {
    if (this.exitStatus) return Promise.reject(new PluginExitedError(this.exitStatus))
    const id = this.nextId++
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (value: unknown) => void, reject })
      this.child.stdin!.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n')
    })
  }

  notify(method: string, params: unknown): void {
    if (!this.exitStatus) this.child.stdin!.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n')
  }

  /** Signals the whole process group. */
  kill(signal: NodeJS.Signals): void {
    if (this.exitStatus || this.child.pid === undefined) return
    try {
      process.kill(-this.child.pid, signal)
    } catch {
      // Already gone.
    }
  }
}
