import { readFileSync } from 'node:fs'
import { KERNEL_API_VERSION } from '@llmchip/kernel-api'

export interface Io {
  out(text: string): void
  err(text: string): void
}

const USAGE = `Usage: harnessd [--version | --help]

The llmchip-harness kernel daemon. Only --version and --help are implemented so far.
`

// src/ and lib/ are both one level below the package root.
function packageVersion(): string {
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
  return pkg.version
}

/** Runs the harnessd command line and returns the process exit code. */
export function main(argv: readonly string[], io: Io): number {
  const [arg, ...rest] = argv
  if (rest.length > 0) {
    io.err(`harnessd: expected at most one argument\n${USAGE}`)
    return 2
  }
  switch (arg) {
    case '--version':
      io.out(`harnessd ${packageVersion()} (kernel-api ${KERNEL_API_VERSION})\n`)
      return 0
    case undefined:
    case '--help':
      io.out(USAGE)
      return 0
    default:
      io.err(`harnessd: unknown argument: ${arg}\n${USAGE}`)
      return 2
  }
}
