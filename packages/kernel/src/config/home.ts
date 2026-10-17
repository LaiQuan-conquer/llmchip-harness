import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'

/** Directory layout of a workbench home (architecture §8). */
export interface HomeLayout {
  root: string
  config: string
  secrets: string
  db: string
  store: string
  run: string
  logs: string
  runs: string
  plugins: string
}

/** The home directory: explicit argument, then `LLMCHIP_HOME`, then `~/.llmchip`. */
export function resolveHome(explicit?: string): string {
  return resolve(explicit ?? process.env.LLMCHIP_HOME ?? join(homedir(), '.llmchip'))
}

export function homeLayout(root: string): HomeLayout {
  return {
    root,
    config: join(root, 'config'),
    secrets: join(root, 'secrets'),
    db: join(root, 'db'),
    store: join(root, 'store'),
    run: join(root, 'run'),
    logs: join(root, 'logs'),
    runs: join(root, 'runs'),
    plugins: join(root, 'plugins'),
  }
}

export function ensureHome(layout: HomeLayout): void {
  for (const [key, dir] of Object.entries(layout)) {
    mkdirSync(dir, { recursive: true, mode: key === 'secrets' || key === 'run' ? 0o700 : 0o755 })
  }
}
