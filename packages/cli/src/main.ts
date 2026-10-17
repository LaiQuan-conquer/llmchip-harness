import { createReadStream, createWriteStream, existsSync, readFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { Readable, Writable } from 'node:stream'
import { parseArgs, type ParseArgsConfig } from 'node:util'
import { KernelCallError, KernelClient } from '@llmchip/client'
import type { Label, RunRecord } from '@llmchip/kernel-api'

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
  kernel stop | pause [--reason TEXT] | resume
  plugins list
  run start PLUGIN HANDLER [--params JSON] [--input NAME=HASH]... [--max-attempts N] [--wait]
  run get|wait|cancel RUN
  run list [--state S]
  estimate --model PRESET [--bits N] [--process ID] [--storage ID] [--params JSON]
                                       Feasibility, area and cost estimate (prints the summary)
  project create ID --title T [--flow F]
  project list
  project show ID [--branch B]
  project bind ID SLOT HASH [--branch B]
  project branch ID NAME [--from B] [--at SEQ]
  stage enter ID STAGE [--branch B]
  gate snapshot|release ID GATE [--branch B]
  verdict evaluate CHECK RUN [--project ID]
  call METHOD [JSON]                   Any kernel API method, e.g. approvals.decide

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
        params: { type: 'string' },
        'max-attempts': { type: 'string' },
        wait: { type: 'boolean' },
        state: { type: 'string' },
        model: { type: 'string' },
        bits: { type: 'string' },
        process: { type: 'string' },
        storage: { type: 'string' },
        title: { type: 'string' },
        flow: { type: 'string' },
        branch: { type: 'string' },
        from: { type: 'string' },
        at: { type: 'string' },
        project: { type: 'string' },
      },
      allowPositionals: true,
      strict: true,
    })
  } catch (error) {
    return usage(io, (error as Error).message)
  }
  const { values, positionals } = parsed
  const [group, action, arg, arg2, arg3] = positionals
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
      case 'kernel pause':
        await client.call('kernel.pause', compact({ reason: values.reason }))
        io.out('paused: no new run starts until `llmchip kernel resume`\n')
        return 0
      case 'kernel resume':
        await client.call('kernel.resume')
        io.out('resumed\n')
        return 0
      case 'plugins list': {
        const result = await client.call('plugins.list')
        print(result, () =>
          [
            ...result.plugins.map((p) => `${p.id}\t${p.version}\t${p.runtime}\t${p.kinds.join(',')}\t${[...p.handlers, ...p.data.map((d) => `data:${d.id}`)].join(' ')}\n`),
            ...result.problems.map((p) => `problem: ${p.code} ${JSON.stringify(p.params ?? {})}\n`),
          ].join(''),
        )
        return 0
      }
      case 'run start': {
        if (!arg || !arg2) return usage(io, 'run start needs PLUGIN and HANDLER')
        const inputs = []
        for (const spec of values.input ?? []) {
          const [name, hash] = spec.split('=')
          if (!name || !hash) return usage(io, `--input must be NAME=HASH, got ${spec}`)
          inputs.push({ name, hash })
        }
        let run = await client.call('runs.start', {
          plugin: arg,
          handler: arg2,
          params: values.params ? JSON.parse(values.params) : {},
          inputs,
          ...compact({ maxAttempts: int(values['max-attempts']) }),
        })
        if (values.wait) run = await waitRun(client, run.id)
        print(run, () => runLine(run))
        return run.state === 'finished' && run.execution !== 'exited_zero' ? 1 : 0
      }
      case 'run get':
      case 'run wait':
      case 'run cancel': {
        if (!arg) return usage(io, `run ${action} needs RUN`)
        const run =
          action === 'get' ? await client.call('runs.get', { run: arg }) : action === 'wait' ? await waitRun(client, arg) : await client.call('runs.cancel', { run: arg })
        print(run, () => runLine(run))
        return 0
      }
      case 'run list': {
        const { runs } = await client.call('runs.list', compact({ state: values.state as 'queued' | undefined }))
        print(runs, () => runs.map(runLine).join(''))
        return 0
      }
      case 'estimate': {
        if (!values.model) return usage(io, 'estimate needs --model PRESET (stories260K, qwen2.5-0.5b, llama3.1-8b)')
        const processId = values.process ?? 'sephonchitech-180bcd'
        const { plugins } = await client.call('plugins.list')
        const data = plugins.flatMap((p) => p.data)
        const profile = data.find((d) => d.type === 'process.profile' && d.id === processId)
        const library = data.find((d) => d.type === 'implementation.library')
        if (!profile || !library) return usage(io, `no process profile ${processId} or implementation library is registered`)
        const params = {
          model: { preset: values.model },
          weightStorage: values.storage ?? (processId === 'gf180mcu' ? 'gf180mcu.stdcell-rom' : `${processId}.foundry-rom`),
          ...compact({ weightBits: int(values.bits) }),
          ...(values.params ? JSON.parse(values.params) : {}),
        }
        const run = await waitRun(
          client,
          (
            await client.call('runs.start', {
              plugin: 'estimator',
              handler: 'estimate',
              params,
              inputs: [
                { name: 'profile', hash: profile.hash },
                { name: 'library', hash: library.hash },
              ],
            })
          ).id,
        )
        if (run.execution !== 'exited_zero') {
          io.err(`llmchip: estimate failed: ${run.error ?? run.execution}\n`)
          return 1
        }
        const output = run.outputs.find((o) => o.name === (values.json ? 'report' : 'summary'))!
        io.out(await (await client.download(output.hash)).text())
        return 0
      }
      case 'project create': {
        if (!arg || !values.title) return usage(io, 'project create needs ID and --title')
        print(await client.call('projects.create', { id: arg, title: values.title, ...compact({ flow: values.flow }) }), () => `created project ${arg}\n`)
        return 0
      }
      case 'project list': {
        const { projects } = await client.call('projects.list')
        print(projects, () => projects.map((p) => `${p.id}\t${p.flow}\t${p.title}\n`).join(''))
        return 0
      }
      case 'project show':
        if (!arg) return usage(io, 'project show needs ID')
        print(await client.call('projects.get', { project: arg, ...compact({ branch: values.branch }) }))
        return 0
      case 'project bind':
        if (!arg || !arg2 || !arg3) return usage(io, 'project bind needs ID SLOT HASH')
        await client.call('projects.bind', { project: arg, slot: arg2, hash: arg3, ...compact({ branch: values.branch }) })
        io.out(`bound ${arg2}\n`)
        return 0
      case 'project branch':
        if (!arg || !arg2) return usage(io, 'project branch needs ID and NAME')
        await client.call('projects.branch', { project: arg, name: arg2, ...compact({ from: values.from, atSeq: int(values.at) }) })
        io.out(`created branch ${arg2}\n`)
        return 0
      case 'stage enter':
        if (!arg || !arg2) return usage(io, 'stage enter needs ID and STAGE')
        await client.call('stages.enter', { project: arg, stage: arg2, ...compact({ branch: values.branch }) })
        io.out(`entered stage ${arg2}\n`)
        return 0
      case 'gate snapshot':
      case 'gate release': {
        if (!arg || !arg2) return usage(io, `gate ${action} needs ID and GATE`)
        const params = { project: arg, gate: arg2, ...compact({ branch: values.branch }) }
        if (action === 'snapshot') print(await client.call('gates.snapshot', params))
        else {
          const release = await client.call('gates.release', params)
          print(release, () => `${release.gate}: ${release.status}${release.released ? ' (released)' : ''}\n${release.reasons.map((r) => `  ${r.code} ${JSON.stringify(r.params ?? {})}\n`).join('')}`)
        }
        return 0
      }
      case 'verdict evaluate': {
        if (!arg || !arg2) return usage(io, 'verdict evaluate needs CHECK and RUN')
        const verdict = await client.call('verdicts.evaluate', { check: arg, run: arg2, project: values.project ?? '' })
        print(verdict, () => `${verdict.check}: ${verdict.verdict}\n${verdict.reasons.map((r) => `  ${r.code} ${JSON.stringify(r.params ?? {})}\n`).join('')}`)
        return 0
      }
      default:
        if (group === 'call') {
          if (!action) return usage(io, 'call needs METHOD')
          const params = arg ? JSON.parse(arg) : {}
          print(await (client.call as (method: string, params: unknown) => Promise<unknown>)(action, params))
          return 0
        }
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

/** Waits for a run to finish, however long it takes. */
async function waitRun(client: KernelClient, run: string): Promise<RunRecord> {
  for (;;) {
    const record = await client.call('runs.wait', { run, timeoutMs: 600_000 })
    if (record.state === 'finished') return record
  }
}

function runLine(run: RunRecord): string {
  const outputs = run.outputs.map((o) => `${o.name}=${o.hash}`).join(' ')
  return `${run.id}\t${run.plugin}/${run.handler}\t${run.state}\t${run.execution}${run.error ? `\t${run.error}` : ''}${outputs ? `\t${outputs}` : ''}\n`
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
