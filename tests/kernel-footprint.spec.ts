// Startup time and idle memory of harnessd (requirements §10.1, ADR-001 exit
// criteria), measured on the built kernel the way it is deployed.

import { spawn } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { resolve, join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { describe, expect, it } from 'vitest'
import { tempDir } from './helpers.ts'

const BUILT_CLI = resolve(import.meta.dirname, '../packages/kernel/lib/cli.js')

/** Share of the §10.1 idle-memory budget (500 MB, kernel plus desktop app) allotted to the kernel. */
const KERNEL_RSS_BUDGET_MB = 250
/** §10.1 warm-start target for the whole application. */
const STARTUP_BUDGET_MS = 5000

describe('harnessd footprint', () => {
  it.skipIf(!existsSync('/proc/self/status'))('starts quickly and idles within its memory budget', { timeout: 30_000 }, async () => {
    const home = tempDir()
    const begin = performance.now()
    const child = spawn(process.execPath, [BUILT_CLI, 'serve', '--home', home], { stdio: 'ignore' })
    try {
      const runtime = join(home, 'run', 'harnessd.json')
      while (!existsSync(runtime)) {
        if (child.exitCode !== null) throw new Error(`harnessd exited with ${child.exitCode}`)
        await sleep(10)
      }
      const startupMs = performance.now() - begin
      await sleep(1000)
      const rssKb = Number(/VmRSS:\s+(\d+)/.exec(readFileSync(`/proc/${child.pid}/status`, 'utf8'))![1])
      const rssMb = rssKb / 1024
      console.log(`harnessd startup ${startupMs.toFixed(0)} ms, idle RSS ${rssMb.toFixed(1)} MB`)
      expect(startupMs).toBeLessThan(STARTUP_BUDGET_MS)
      expect(rssMb).toBeLessThan(KERNEL_RSS_BUDGET_MB)
    } finally {
      child.kill('SIGTERM')
      await new Promise((r) => child.once('exit', r))
    }
  })
})
