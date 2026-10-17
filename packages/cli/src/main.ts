import { createReadStream, createWriteStream, existsSync, readFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { Readable, Writable } from 'node:stream'
import { parseArgs, type ParseArgsConfig } from 'node:util'
import { KernelCallError, KernelClient } from '@llmchip/client'
import type { Label } from '@llmchip/kernel-api'

export interface Io {
  out(text: string): void
  err(text: string): void
  /** Standard input, for secret values. */
  stdin?: () => Promise<string>
}

const USAGE = `Usage: llmchip [--home DIR] <command> [options]

Command-line client of the llmchip-harness kernel (harnessd must be running;
start it with \`harnessd serve\`).

Commands:
  info                                 Kernel version and event log head
  events list [--since N] [--limit N] [--kind K]
  events tail [--since N]              Stream events until interrupted
  events verify                        Check the event log hash chain
  artifact put FILE --type T [--media-type M] [--label L] [--input HASH]...
  artifact get HASH [-o FILE]          Write content to FILE or stdout
  artifact stat HASH
  artifact list [--type T] [--all]
  artifact lineage HASH [--down]
  artifact rm HASH --reason TEXT       Mark deleted (content is kept)
  artifact verify                      Check stored content against hashes
  projections rebuild                  Rebuild read models from the event log
  secret set NAME                      Value is read from standard input
  secret rm NAME
  secret list
  kernel stop

Global options:
  --home DIR   Workbench home (default: $LLMCHIP_HOME or ~/.llmchip)
  --json       Print raw JSON results
  --help       Print this help
`

const COMMON = {
  home: { type: 'string' },
  json: { type: 'boolean' },
  help: { type: 'boolean' },
} satisfies ParseArgsConfig['options']

/** Runs the llmchip command line and resolves to the process exit code. */
export async function main(argv: readonly string[], io: Io): Promise<number> {
  let parsed
  try {
    parsed = parseArgs({
      args: [...argv],
      options: {
        ...COMMON,
        since: { type: 'string' },
        limit: { type: 'string' },
        kind: { type: 'string' },
        type: { type: 'string' },
        'media-type': { type: 'string' },
        label: { type: 'string' },
        input: { type: 'string', multiple: true },
        output: { type: 'string', short: 'o' },
        all: { type: 'boolean' },
        down: { type: 'boolean' },
        reason: { type: 'string' },
      },
      allowPositionals: true,
      strict: true,
    })
  } catch (error) {
    return usage(io, (error as Error).message)
  }
  const { values, positionals } = parsed
  const [group, action, arg] = positionals
  if (values.help || !group) {
    io.out(USAGE)
    return 0
  }

  const home = resolve(values.home ?? process.env.LLMCHIP_HOME ?? join(homedir(), '.llmchip'))
  const runtimePath = join(home, 'run', 'harnessd.json')
  if (!existsSync(runtimePath)) {
    io.err(`llmchip: harnessd is not running for ${home} (no ${runtimePath}); start it with \`harnessd serve\`\n`)
    return 1
  }
  const runtime = JSON.parse(readFileSync(runtimePath, 'utf8')) as { url: string; token: string }

  let client: KernelClient
  try {
    client = await KernelClient.connect(runtime)
  } catch (error) {
    io.err(`llmchip: ${(error as Error).message}\n`)
    return 1
  }
  const print = (value: unknown, text?: () => string) => io.out(values.json || !text ? JSON.stringify(value, null, 2) + '\n' : text())
  const int = (value: string | undefined) => (value === undefined ? undefined : Number(value))

  try {
    switch (`${group} ${action ?? ''}`.trim()) {
      case 'info': {
        const info = await client.call('kernel.info')
        print(info, () => `harnessd ${info.version} (kernel-api ${info.apiVersion}), pid ${info.pid}, ${info.head} events\n`)
        return 0
      }
      case 'events list': {
        const params = compact({ since: int(values.since), limit: int(values.limit), kind: values.kind })
        const { events } = await client.call('events.list', params)
        print(events, () => events.map((e) => `${e.seq}\t${e.ts}\t${e.kind}\t${e.actor.kind}:${e.actor.id}\n`).join(''))
        return 0
      }
      case 'events tail': {
        await client.subscribe((e) => print(e, () => `${e.seq}\t${e.ts}\t${e.kind}\n`), int(values.since))
        await client.closed
        return 0
      }
      case 'events verify':
        return report(io, await client.call('events.verify'), 'events')
      case 'artifact put': {
        if (!arg || !values.type) return usage(io, 'artifact put needs FILE and --type')
        if (!existsSync(arg) || !statSync(arg).isFile()) return usage(io, `not a file: ${arg}`)
        const meta = await client.upload(Readable.toWeb(createReadStream(arg)) as ReadableStream, {
          type: values.type,
          ...compact({ mediaType: values['media-type'], label: values.label as Label | undefined }),
          ...(values.input ? { inputs: values.input } : {}),
        })
        print(meta, () => `${meta.hash}\n`)
        return 0
      }
      case 'artifact get': {
        if (!arg) return usage(io, 'artifact get needs HASH')
        const response = await client.download(arg)
        const sink = values.output ? createWriteStream(values.output) : process.stdout
        await response.body!.pipeTo(Writable.toWeb(sink) as WritableStream)
        return 0
      }
      case 'artifact stat':
        if (!arg) return usage(io, 'artifact stat needs HASH')
        print(await client.call('artifacts.stat', { hash: arg }))
        return 0
      case 'artifact list': {
        const { artifacts } = await client.call('artifacts.list', compact({ type: values.type, includeDeleted: values.all }))
        print(artifacts, () => artifacts.map((a) => `${a.hash}\t${a.type}\t${a.size}\t${a.label}${a.deleted ? '\tdeleted' : ''}\n`).join(''))
        return 0
      }
      case 'artifact lineage': {
        if (!arg) return usage(io, 'artifact lineage needs HASH')
        const lineage = await client.call('artifacts.lineage', { hash: arg, direction: values.down ? 'down' : 'up' })
        print(lineage, () => lineage.edges.map((e) => `${e.from} -> ${e.to}${e.run ? ` (run ${e.run})` : ''}\n`).join('') || '(no edges)\n')
        return 0
      }
      case 'artifact rm':
        if (!arg || !values.reason) return usage(io, 'artifact rm needs HASH and --reason')
        print(await client.call('artifacts.markDeleted', { hash: arg, reason: values.reason }), () => `marked ${arg} deleted\n`)
        return 0
      case 'artifact verify':
        return report(io, await client.call('artifacts.verify'), 'artifacts')
      case 'projections rebuild': {
        const result = await client.call('projections.rebuild')
        print(result, () => result.rebuilt.map((r) => `${r.projection} v${r.version}: ${r.events} events\n`).join(''))
        return 0
      }
      case 'secret set': {
        if (!arg) return usage(io, 'secret set needs NAME')
        if (!io.stdin) return usage(io, 'no standard input available')
        const value = (await io.stdin()).replace(/\r?\n$/, '')
        await client.call('secrets.set', { name: arg, value })
        io.out(`secret ${arg} set\n`)
        return 0
      }
      case 'secret rm':
        if (!arg) return usage(io, 'secret rm needs NAME')
        await client.call('secrets.remove', { name: arg })
        io.out(`secret ${arg} removed\n`)
        return 0
      case 'secret list': {
        const { names } = await client.call('secrets.list')
        print(names, () => names.map((n) => `${n}\n`).join(''))
        return 0
      }
      case 'kernel stop':
        await client.call('kernel.shutdown')
        io.out('harnessd is stopping\n')
        return 0
      default:
        return usage(io, `unknown command: ${positionals.join(' ')}`)
    }
  } catch (error) {
    if (error instanceof KernelCallError) {
      io.err(`llmchip: ${error.message} [${error.code}]\n`)
      return 1
    }
    throw error
  } finally {
    await client.close()
  }
}

function report(io: Io, result: { ok: boolean; checked: number; problems: { code: string; params?: object }[] }, what: string): number {
  if (result.ok) {
    io.out(`ok: ${result.checked} ${what} checked\n`)
    return 0
  }
  for (const problem of result.problems) io.err(`${problem.code} ${JSON.stringify(problem.params ?? {})}\n`)
  io.err(`FAILED: ${result.problems.length} problem(s) in ${result.checked} ${what}\n`)
  return 1
}

function usage(io: Io, message: string): number {
  io.err(`llmchip: ${message}\n${USAGE}`)
  return 2
}

function compact<T extends Record<string, unknown>>(value: T): { [K in keyof T]?: Exclude<T[K], undefined> } {
  return Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined)) as never
}
