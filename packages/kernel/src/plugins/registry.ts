import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import {
  KERNEL_API_VERSION,
  PLUGIN_MANIFEST,
  type HandlerSpec,
  type Message,
  type PluginInfo,
  type PluginManifest,
} from '@llmchip/kernel-api'
import type { ArtifactStore } from '../artifacts/store.ts'
import { KernelError } from '../util/errors.ts'
import type { Logger } from '../util/logger.ts'
import { assertValid } from '../util/schema.ts'

export interface RegisteredPlugin {
  manifest: PluginManifest
  /** Absolute plugin directory. */
  dir: string
  /** Data files imported as artifacts, by data id. */
  data: Map<string, { type: string; hash: string }>
}

/**
 * K9, out-of-process side (architecture §4.9): discovers plugins by their
 * `llmchip-plugin.json`, validates manifests and API compatibility, and
 * imports the data files of data plugins as artifacts so that every use of
 * them is traceable by hash. Invalid plugins are reported, not fatal.
 */
export class PluginRegistry {
  private readonly plugins = new Map<string, RegisteredPlugin>()
  private problems: Message[] = []
  private readonly searchPaths: string[]
  private readonly artifacts: ArtifactStore
  private readonly logger: Logger

  constructor(options: { searchPaths: string[]; artifacts: ArtifactStore; logger: Logger }) {
    this.searchPaths = options.searchPaths.map((path) => resolve(path))
    this.artifacts = options.artifacts
    this.logger = options.logger
  }

  /** (Re)scans the search paths. Returns the problems found. */
  async scan(): Promise<Message[]> {
    this.plugins.clear()
    this.problems = []
    for (const dir of this.candidateDirs()) {
      try {
        await this.register(dir)
      } catch (error) {
        const message = error instanceof KernelError ? error.message : String(error)
        this.problems.push({ code: 'PLUGIN_INVALID', params: { dir, error: message } })
        this.logger.warn('plugin rejected', { dir, error: message })
      }
    }
    return this.problems
  }

  get(id: string): RegisteredPlugin | undefined {
    return this.plugins.get(id)
  }

  require(id: string): RegisteredPlugin {
    const plugin = this.plugins.get(id)
    if (!plugin) throw new KernelError('PLUGIN_NOT_FOUND', `plugin ${id} is not registered`, { plugin: id })
    return plugin
  }

  handler(pluginId: string, name: string): { plugin: RegisteredPlugin; spec: HandlerSpec } {
    const plugin = this.require(pluginId)
    const spec = plugin.manifest.handlers?.find((h) => h.name === name)
    if (!spec) throw new KernelError('HANDLER_NOT_FOUND', `plugin ${pluginId} has no handler ${name}`, { plugin: pluginId, handler: name })
    return { plugin, spec }
  }

  /** The hash of the data file with this id, from the first plugin that has one. */
  dataHash(id: string): string | undefined {
    for (const plugin of this.plugins.values()) {
      const data = plugin.data.get(id)
      if (data) return data.hash
    }
    return undefined
  }

  /** Imported data files of the given type, across plugins. */
  dataOfType(type: string): { plugin: string; id: string; hash: string }[] {
    const out: { plugin: string; id: string; hash: string }[] = []
    for (const plugin of this.plugins.values()) {
      for (const [id, data] of plugin.data) if (data.type === type) out.push({ plugin: plugin.manifest.id, id, hash: data.hash })
    }
    return out
  }

  list(): { plugins: PluginInfo[]; problems: Message[] } {
    const plugins = [...this.plugins.values()].map((p) => ({
      id: p.manifest.id,
      version: p.manifest.version,
      kinds: p.manifest.kinds,
      runtime: p.manifest.runtime,
      dir: p.dir,
      handlers: (p.manifest.handlers ?? []).map((h) => h.name),
      data: [...p.data].map(([id, d]) => ({ id, type: d.type, hash: d.hash })),
    }))
    return { plugins, problems: this.problems }
  }

  private candidateDirs(): string[] {
    const dirs: string[] = []
    for (const path of this.searchPaths) {
      if (!existsSync(path)) continue
      if (existsSync(join(path, PLUGIN_MANIFEST))) dirs.push(path)
      else {
        for (const name of readdirSync(path).sort()) {
          const dir = join(path, name)
          if (statSync(dir).isDirectory() && existsSync(join(dir, PLUGIN_MANIFEST))) dirs.push(dir)
        }
      }
    }
    return dirs
  }

  private async register(dir: string): Promise<void> {
    let manifest: unknown
    try {
      manifest = JSON.parse(readFileSync(join(dir, PLUGIN_MANIFEST), 'utf8'))
    } catch (error) {
      throw new KernelError('PLUGIN_MANIFEST_UNREADABLE', `${PLUGIN_MANIFEST}: ${(error as Error).message}`)
    }
    assertValid<PluginManifest>('PluginManifest', manifest, 'PLUGIN_MANIFEST_INVALID')
    if (!compatible(manifest.kernelApi, KERNEL_API_VERSION)) {
      throw new KernelError('PLUGIN_API_INCOMPATIBLE', `plugin ${manifest.id} requires kernel API ${manifest.kernelApi}, kernel has ${KERNEL_API_VERSION}`)
    }
    if (this.plugins.has(manifest.id)) {
      throw new KernelError('PLUGIN_DUPLICATE', `plugin ${manifest.id} is already registered from ${this.plugins.get(manifest.id)!.dir}`)
    }
    if (manifest.runtime !== 'data' && !manifest.entry) {
      throw new KernelError('PLUGIN_MANIFEST_INVALID', `plugin ${manifest.id} has runtime ${manifest.runtime} but no entry`)
    }
    if (manifest.runtime === 'container' && !manifest.entry?.image) {
      throw new KernelError('PLUGIN_MANIFEST_INVALID', `plugin ${manifest.id} has runtime container but no entry.image`)
    }
    const data = new Map<string, { type: string; hash: string }>()
    for (const spec of manifest.data ?? []) {
      const path = resolve(dir, spec.path)
      if (!path.startsWith(dir + '/')) throw new KernelError('PLUGIN_MANIFEST_INVALID', `data path ${spec.path} leaves the plugin directory`)
      const meta = await this.artifacts.put(readFileSync(path), {
        type: spec.type,
        mediaType: spec.mediaType ?? 'application/json',
        label: spec.label ?? 'public',
        creator: { kind: 'plugin', id: manifest.id },
      })
      data.set(spec.id, { type: spec.type, hash: meta.hash })
    }
    this.plugins.set(manifest.id, { manifest, dir, data })
  }
}

/** Whether `version` satisfies a caret range `^MAJOR.MINOR[.PATCH]` (0.x: same minor). */
export function compatible(range: string, version: string): boolean {
  const [rMajor = 0, rMinor = 0, rPatch = 0] = range.slice(1).split('.').map(Number)
  const [major = 0, minor = 0, patch = 0] = version.split('-')[0]!.split('.').map(Number)
  if (major !== rMajor) return false
  if (major === 0) return minor === rMinor && patch >= rPatch
  return minor > rMinor || (minor === rMinor && patch >= rPatch)
}
