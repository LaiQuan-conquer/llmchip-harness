import { parseArgs } from 'node:util'
import { KERNEL_API_VERSION } from '@llmchip/kernel-api'
import { Kernel, kernelVersion } from './kernel.ts'
import { KernelError } from './util/errors.ts'

export interface Io {
  out(text: string): void
  err(text: string): void
}

const USAGE = `Usage: harnessd <command> [options]

The llmchip-harness kernel daemon.

Commands:
  serve        Run the kernel until it receives SIGINT/SIGTERM or kernel.shutdown.
    --home DIR   Workbench home (default: $LLMCHIP_HOME or ~/.llmchip)
    --host HOST  Listen address (default: 127.0.0.1)
    --port PORT  Listen port; 0 picks a free port (default: 0)
    --plugins DIR  Also load plugins from DIR (repeatable)
    --python PATH  Python interpreter for Python plugins (default: python3)

Options:
  --version    Print the version and exit.
  --help       Print this help and exit.
`

export interface MainOptions {
  /** Installs SIGINT/SIGTERM handlers that stop the kernel. */
  signals?: boolean
  /** Called once a `serve` kernel has started. */
  onStarted?: (kernel: Kernel) => void
}

/** Runs the harnessd command line and resolves to the process exit code. */
export async function main(argv: readonly string[], io: Io, options: MainOptions = {}): Promise<number> {
  const [command, ...rest] = argv
  if (command === '--version' || command === '--help' || command === undefined) {
    if (rest.length > 0) return usageError(io, 'expected no arguments after the option')
    if (command === '--version') io.out(`harnessd ${kernelVersion()} (kernel-api ${KERNEL_API_VERSION})\n`)
    else io.out(USAGE)
    return 0
  }
  if (command !== 'serve') return usageError(io, `unknown argument: ${command}`)

  let values: { home?: string; host?: string; port?: string; plugins?: string[]; python?: string }
  try {
    ;({ values } = parseArgs({
      args: [...rest],
      options: {
        home: { type: 'string' },
        host: { type: 'string' },
        port: { type: 'string' },
        plugins: { type: 'string', multiple: true },
        python: { type: 'string' },
      },
      strict: true,
      allowPositionals: false,
    }))
  } catch (error) {
    return usageError(io, (error as Error).message)
  }
  const port = values.port === undefined ? undefined : Number(values.port)
  if (port !== undefined && !(Number.isInteger(port) && port >= 0 && port <= 65535)) {
    return usageError(io, `invalid port: ${values.port}`)
  }

  let kernel: Kernel
  try {
    kernel = await Kernel.start({
      ...(values.home ? { home: values.home } : {}),
      config: {
        server: { ...(values.host ? { host: values.host } : {}), ...(port !== undefined ? { port } : {}) },
        plugins: { ...(values.plugins ? { searchPaths: values.plugins } : {}), ...(values.python ? { python: values.python } : {}) },
      },
      console: (line) => io.err(line + '\n'),
    })
  } catch (error) {
    if (error instanceof KernelError) {
      io.err(`harnessd: ${error.message}\n`)
      return 1
    }
    throw error
  }
  if (options.signals) {
    const stop = (signal: string) => void kernel.stop(`signal ${signal}`)
    process.once('SIGINT', stop)
    process.once('SIGTERM', stop)
  }
  options.onStarted?.(kernel)
  await kernel.stopped
  return 0
}

function usageError(io: Io, message: string): number {
  io.err(`harnessd: ${message}\n${USAGE}`)
  return 2
}
