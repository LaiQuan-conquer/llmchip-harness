import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { WorkspaceConfig } from '@llmchip/kernel-api'
import { KernelError } from '../util/errors.ts'
import { assertValid } from '../util/schema.ts'

type Plugins = NonNullable<WorkspaceConfig['plugins']>

/** Fully defaulted workspace configuration. The kernel fills in `plugins.python` itself. */
export type ResolvedConfig = Required<{ [K in Exclude<keyof WorkspaceConfig, 'plugins'>]: Required<NonNullable<WorkspaceConfig[K]>> }> & {
  plugins: Required<Omit<Plugins, 'python' | 'node'>> & Pick<Plugins, 'python' | 'node'>
}

/**
 * K10: layered configuration (architecture §4.10). Layers, lowest first:
 * schema defaults, `<home>/config/config.json`, then explicit overrides (for
 * example command-line flags). Every layer is validated against the schema.
 */
export function loadConfig(configDir: string, overrides: WorkspaceConfig = {}): ResolvedConfig {
  const path = join(configDir, 'config.json')
  let file: WorkspaceConfig = {}
  if (existsSync(path)) {
    try {
      file = JSON.parse(readFileSync(path, 'utf8'))
    } catch (error) {
      throw new KernelError('CONFIG_UNREADABLE', `cannot parse ${path}: ${(error as Error).message}`, { path })
    }
  }
  assertValid<WorkspaceConfig>('WorkspaceConfig', file, 'CONFIG_INVALID')
  assertValid<WorkspaceConfig>('WorkspaceConfig', overrides, 'CONFIG_INVALID')
  const merged: WorkspaceConfig = {
    server: { ...file.server, ...overrides.server },
    events: { ...file.events, ...overrides.events },
    plugins: { ...file.plugins, ...overrides.plugins },
    scheduler: { ...file.scheduler, ...overrides.scheduler },
  }
  // Validation with useDefaults fills every defaulted field.
  assertValid<WorkspaceConfig>('WorkspaceConfig', merged, 'CONFIG_INVALID')
  return merged as ResolvedConfig
}
