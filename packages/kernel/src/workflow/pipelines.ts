import type {
  Actor,
  FlowPack,
  Message,
  PipelineKind,
  PipelineNode,
  PipelineNodeSpec,
  PipelinePlan,
  PipelineRecord,
  PipelineState,
  PipelineSummary,
  PipelineTestSpec,
  PipelinesStartParams,
  RunRecord,
  RunRef,
} from '@llmchip/kernel-api'
import type { EventStore, Projection } from '../events/store.ts'
import type { PluginRegistry } from '../plugins/registry.ts'
import type { Scheduler } from '../runs/scheduler.ts'
import { canonicalJson } from '../util/canonical.ts'
import { KernelError } from '../util/errors.ts'
import type { Logger } from '../util/logger.ts'
import { ulid } from '../util/ulid.ts'
import type { Workflow } from './engine.ts'

const KERNEL_ACTOR: Actor = { kind: 'kernel', id: 'pipelines' }
const FINAL: ReadonlySet<PipelineNode['state']> = new Set(['done', 'failed', 'skipped', 'cancelled'])
/** How long one wait for a run lasts before it is renewed. */
const WAIT_MS = 600_000

/** What a node does, from the flow pack. */
type NodeSpec =
  | { kind: 'defaults'; stage: string; defaults: Record<string, string> }
  | { kind: 'enter'; stage: string }
  | { kind: 'run'; stage: string; spec: PipelineNodeSpec }
  | { kind: 'test'; stage: string; spec: PipelineTestSpec }

/** Projection of `pipeline.*` events: one row per pipeline with its current record. */
export const pipelinesProjection: Projection = {
  name: 'pipelines',
  version: 1,
  setup(db) {
    db.exec(`
      CREATE TABLE IF NOT EXISTS pipelines (id TEXT PRIMARY KEY, project TEXT NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS pipelines_project ON pipelines (project, seq);
    `)
  },
  reset(db) {
    db.exec('DROP TABLE IF EXISTS pipelines')
  },
  apply(event, db) {
    const p = event.payload as Record<string, any>
    if (event.kind === 'pipeline.started') {
      const record = p.pipeline as PipelineRecord
      db.prepare('INSERT INTO pipelines (id, project, seq, state, record) VALUES (?, ?, ?, ?, ?)').run(record.id, record.project, event.seq, record.state, JSON.stringify(record))
      return
    }
    if (!event.kind.startsWith('pipeline.')) return
    const row = db.prepare('SELECT record FROM pipelines WHERE id = ?').get(p.pipeline) as { record: string } | undefined
    if (!row) return
    const record = JSON.parse(row.record) as PipelineRecord
    if (event.kind === 'pipeline.node') {
      record.nodes = record.nodes.map((n) => (n.id === p.node.id ? p.node : n))
    } else if (event.kind === 'pipeline.resumed') {
      Object.assign(record, { state: 'running', endedAt: null })
    } else if (event.kind === 'pipeline.finished') {
      Object.assign(record, { state: p.state, endedAt: event.ts })
    }
    record.summary = summarize(record.nodes)
    db.prepare('UPDATE pipelines SET state = ?, record = ? WHERE id = ?').run(record.state, JSON.stringify(record), record.id)
  },
}

/**
 * K3 pipelines (requirements FR-PRJ-07/08): a stage's development pipeline and
 * its required tests, as declared in the project's flow pack, run by the
 * kernel. Each pipeline is a set of nodes with dependencies: `defaults` binds
 * the stage's default slots, `enter` enters the stage (its admission checks
 * judge it), `run` starts a handler, binds its outputs and judges its checks,
 * `test` judges a check on a run with the declared inputs (reusing a finished
 * one). A node whose dependency did not succeed is skipped; a failed node stays
 * failed until the pipeline is resumed. The state is in events only, so after a
 * kernel restart `recover` restarts the nodes whose runs were interrupted, from
 * their latest checkpoint when the handler takes one.
 */
export class Pipelines {
  private readonly events: EventStore
  private readonly registry: PluginRegistry
  private readonly scheduler: Scheduler
  private readonly workflow: Workflow
  private readonly logger: Logger
  private readonly watching = new Set<string>()
  private stopping = false

  constructor(options: { events: EventStore; registry: PluginRegistry; scheduler: Scheduler; workflow: Workflow; logger: Logger }) {
    this.events = options.events
    this.registry = options.registry
    this.scheduler = options.scheduler
    this.workflow = options.workflow
    this.logger = options.logger
    this.events.registerProjection(pipelinesProjection)
  }

  /** What `start` would run, and the inputs that are missing for it now. */
  plan(params: PipelinesStartParams): PipelinePlan {
    const branch = params.branch ?? 'main'
    const { stages, specs } = this.build(this.workflow.flow(params.project), params.stage, params.kind)
    const bound = new Set(Object.keys(this.workflow.project(params.project, branch).slots))
    const missing: PipelinePlan['missing'] = []
    // Slots an earlier node of this pipeline binds count as available.
    for (const [id, spec] of specs) {
      const refs = spec.kind === 'defaults' ? Object.values(spec.defaults) : spec.kind === 'run' || spec.kind === 'test' ? inputRefs(spec.spec.inputs) : []
      for (const ref of refs) {
        if (ref.startsWith('slot:') && !bound.has(ref.slice(5))) missing.push({ node: id, ref })
        if (ref.startsWith('data:') && !this.registry.dataHash(ref.slice(5))) missing.push({ node: id, ref })
      }
      if (spec.kind === 'defaults') for (const slot of Object.keys(spec.defaults)) bound.add(slot)
      if (spec.kind === 'run') for (const slot of Object.values(strings(spec.spec.bind))) bound.add(slot)
    }
    return { stages, nodes: specs.size, missing, ready: specs.size > 0 && missing.length === 0 }
  }

  start(params: PipelinesStartParams, actor: Actor): PipelineRecord {
    const branch = params.branch ?? 'main'
    const plan = this.plan(params)
    if (plan.nodes === 0) throw new KernelError('PIPELINE_EMPTY', `stage ${params.stage} declares no ${params.kind === 'develop' ? 'development pipeline' : 'tests'}`, { stage: params.stage })
    // One pipeline at a time per stage: two would bind the same slots.
    const busy = this.running().find((p) => p.project === params.project && p.branch === branch && p.stages.some((s) => plan.stages.includes(s)))
    if (busy) throw new KernelError('PIPELINE_RUNNING', `pipeline ${busy.id} is running on ${busy.stages.join(', ')}`, { pipeline: busy.id, stages: busy.stages })
    if (!plan.ready) {
      throw new KernelError('PIPELINE_INPUTS_MISSING', `missing inputs: ${plan.missing.map((m) => `${m.node} needs ${m.ref}`).join(', ')}`, { missing: plan.missing })
    }
    const { nodes } = this.build(this.workflow.flow(params.project), params.stage, params.kind)
    const record: PipelineRecord = {
      id: ulid(),
      project: params.project,
      branch,
      kind: params.kind,
      stages: plan.stages,
      state: 'running',
      nodes,
      by: actor,
      startedAt: new Date().toISOString(),
      endedAt: null,
      summary: summarize(nodes),
    }
    this.events.append({ kind: 'pipeline.started', payload: { pipeline: record }, actor, project: record.project })
    this.advance(record.id)
    return this.get(record.id)
  }

  get(id: string): PipelineRecord {
    const row = this.events.db.prepare('SELECT record FROM pipelines WHERE id = ?').get(id) as { record: string } | undefined
    if (!row) throw new KernelError('PIPELINE_NOT_FOUND', `pipeline ${id} does not exist`, { pipeline: id })
    return JSON.parse(row.record)
  }

  list(options: { project?: string; limit?: number } = {}): PipelineRecord[] {
    const rows = (
      options.project
        ? this.events.db.prepare('SELECT record FROM pipelines WHERE project = ? ORDER BY seq DESC LIMIT ?').all(options.project, options.limit ?? 50)
        : this.events.db.prepare('SELECT record FROM pipelines ORDER BY seq DESC LIMIT ?').all(options.limit ?? 50)
    ) as { record: string }[]
    return rows.map((r) => JSON.parse(r.record))
  }

  /** Cancels a running pipeline: its running runs are cancelled, its pending nodes do not start. */
  cancel(id: string, actor: Actor): PipelineRecord {
    const record = this.get(id)
    if (record.state !== 'running') return record
    this.events.transaction(() => {
      for (const node of record.nodes) {
        if (FINAL.has(node.state)) continue
        this.setNode(id, { ...node, state: 'cancelled', reason: { code: 'PIPELINE_CANCELLED', params: { by: `${actor.kind}:${actor.id}` } } })
      }
      this.events.append({ kind: 'pipeline.finished', payload: { pipeline: id, state: 'cancelled' }, actor, project: record.project })
    })
    for (const node of record.nodes) {
      const run = node.state === 'running' ? node.runs.at(-1) : undefined
      if (run) this.scheduler.cancel(run, actor)
    }
    return this.get(id)
  }

  /**
   * Resumes a failed or cancelled pipeline after the cause was fixed: its failed, skipped
   * and cancelled nodes run again; the nodes that succeeded keep their results.
   */
  resume(id: string, actor: Actor): PipelineRecord {
    const record = this.get(id)
    if (record.state === 'running') return record
    if (record.state === 'passed') throw new KernelError('PIPELINE_PASSED', `pipeline ${id} passed; start a new one to run it again`, { pipeline: id })
    this.events.transaction(() => {
      this.events.append({ kind: 'pipeline.resumed', payload: { pipeline: id }, actor, project: record.project })
      for (const node of record.nodes) {
        if (node.state === 'done') continue
        this.setNode(id, { ...node, state: 'pending', reused: null, verdicts: [], reason: null })
      }
    })
    this.advance(id)
    return this.get(id)
  }

  /**
   * Startup recovery, after the scheduler's: a running pipeline continues where it stopped.
   * A node whose run was interrupted with the kernel starts again, from the run's latest
   * checkpoint when the handler takes one; a node whose run finished meanwhile is completed.
   */
  recover(): void {
    for (const record of this.running()) {
      for (const node of record.nodes) {
        const runId = node.state === 'running' ? node.runs.at(-1) : undefined
        if (!runId) continue
        const run = this.scheduler.get(runId)
        if (run.state !== 'finished') this.watch(record.id, node.id, runId)
        else if (run.execution === 'interrupted') this.restartNode(record, node, run)
        else this.completeRun(record.id, node.id, run)
      }
      this.advance(record.id)
    }
  }

  /** Kernel shutdown: runs being interrupted now are restarted by the next `recover`. */
  stop(): void {
    this.stopping = true
  }

  // ---- execution -----------------------------------------------------------

  private running(): PipelineRecord[] {
    return (this.events.db.prepare("SELECT record FROM pipelines WHERE state = 'running' ORDER BY seq").all() as { record: string }[]).map((r) => JSON.parse(r.record))
  }

  /** Starts every node whose dependencies succeeded, skips those whose dependencies did not, and finishes the pipeline when nothing is left. */
  private advance(id: string): void {
    if (this.stopping) return
    for (;;) {
      const record = this.get(id)
      if (record.state !== 'running') return
      const states = new Map(record.nodes.map((n) => [n.id, n.state]))
      let changed = false
      for (const node of record.nodes) {
        if (node.state !== 'pending') continue
        const deps = node.after.map((d) => states.get(d))
        const blocked = node.after.find((d) => ['failed', 'skipped', 'cancelled'].includes(states.get(d) ?? ''))
        if (blocked) {
          this.setNode(id, { ...node, state: 'skipped', reason: { code: 'PIPELINE_DEPENDENCY_FAILED', params: { node: blocked } } })
          changed = true
        } else if (deps.every((s) => s === 'done')) {
          this.execute(record, node)
          changed = true
        }
      }
      if (changed) continue
      const nodes = this.get(id).nodes
      if (nodes.some((n) => n.state === 'running' || n.state === 'pending')) return
      const state: PipelineState = nodes.every((n) => n.state === 'done') ? 'passed' : 'failed'
      this.events.append({ kind: 'pipeline.finished', payload: { pipeline: id, state }, actor: KERNEL_ACTOR, project: record.project })
      return
    }
  }

  private execute(record: PipelineRecord, node: PipelineNode): void {
    const spec = this.specOf(record, node.id)
    try {
      if (spec.kind === 'defaults') {
        const bound = this.workflow.project(record.project, record.branch).slots
        for (const [slot, ref] of Object.entries(spec.defaults)) {
          if (bound[slot]) continue
          this.workflow.bind(record.project, record.branch, slot, this.resolve(record, node.stage, ref), record.by)
        }
        this.setNode(record.id, { ...node, state: 'done' })
      } else if (spec.kind === 'enter') {
        this.workflow.enterStage(record.project, record.branch, node.stage, record.by)
        this.setNode(record.id, { ...node, state: 'done' })
      } else if (spec.kind === 'run') {
        this.startRun(record, node, spec.spec.plugin, spec.spec.handler, spec.spec.params ?? {}, spec.spec.inputs, spec.spec.timeoutMs)
      } else {
        const check = this.workflow.effectiveCheck(spec.spec.check, record.project)
        const params = this.scheduler.params(check.plugin, check.handler, spec.spec.params ?? {})
        const inputs = this.resolveInputs(record, node.stage, spec.spec.inputs)
        const reused = this.reusable(record.project, check.plugin, check.handler, params, inputs)
        if (reused) {
          // One transaction: a crash cannot leave the node running without a run to wait for.
          this.events.transaction(() => {
            this.setNode(record.id, { ...node, state: 'running', reused: reused.id })
            this.completeRun(record.id, node.id, reused)
          })
        } else {
          this.startRun(record, node, check.plugin, check.handler, params, spec.spec.inputs, spec.spec.timeoutMs)
        }
      }
    } catch (error) {
      this.setNode(record.id, { ...node, state: 'failed', reason: reasonOf(error) })
    }
  }

  private startRun(
    record: PipelineRecord,
    node: PipelineNode,
    plugin: string,
    handler: string,
    params: Record<string, unknown>,
    inputSpec: PipelineNodeSpec['inputs'],
    timeoutMs: number | undefined,
  ): void {
    let inputs = this.resolveInputs(record, node.stage, inputSpec)
    // A rerun of the same work after a failure continues from the last run's checkpoint.
    const last = node.runs.length ? this.scheduler.get(node.runs.at(-1)!) : undefined
    if (last?.checkpoint && sameWork(last, plugin, handler, this.scheduler.params(plugin, handler, params), inputs) && this.takesResume(plugin, handler)) {
      inputs = [...inputs, { name: 'resume', hash: last.checkpoint }]
    }
    // One transaction: a crash cannot leave a queued run that its node does not know.
    const run = this.events.transaction(() => {
      const queued = this.scheduler.start({ plugin, handler, params, inputs, project: record.project, ...(timeoutMs ? { timeoutMs } : {}) }, record.by)
      this.setNode(record.id, { ...node, state: 'running', runs: [...node.runs, queued.id], reused: null, reason: null })
      return queued
    })
    this.watch(record.id, node.id, run.id)
  }

  /** Starts an interrupted node's run again, from its latest checkpoint when the handler takes one. */
  private restartNode(record: PipelineRecord, node: PipelineNode, run: RunRecord): void {
    try {
      const again = this.events.transaction(() => {
        const queued = this.scheduler.restart(run, record.by)
        this.setNode(record.id, { ...node, state: 'running', runs: [...node.runs, queued.id] })
        return queued
      })
      this.watch(record.id, node.id, again.id)
    } catch (error) {
      this.setNode(record.id, { ...node, state: 'failed', reason: reasonOf(error) })
    }
  }

  private watch(pipelineId: string, nodeId: string, runId: string): void {
    if (this.watching.has(runId)) return
    this.watching.add(runId)
    const loop = async () => {
      let run = await this.scheduler.wait(runId, WAIT_MS)
      while (run.state !== 'finished' && !this.stopping) run = await this.scheduler.wait(runId, WAIT_MS)
      this.watching.delete(runId)
      if (this.stopping) return
      this.completeRun(pipelineId, nodeId, run)
      this.advance(pipelineId)
    }
    loop().catch((error) => this.logger.error('pipeline watch failed', { pipeline: pipelineId, node: nodeId, error: String(error) }))
  }

  /** Records a finished run on its node: binds its outputs and judges its checks (run), or judges the test's check. */
  private completeRun(pipelineId: string, nodeId: string, run: RunRecord): void {
    const record = this.get(pipelineId)
    const node = record.nodes.find((n) => n.id === nodeId)
    if (!node || node.state !== 'running') return // cancelled meanwhile
    // Only the node's current run completes it: a run of an earlier attempt (cancelled, then resumed) does not.
    if (run.id !== node.runs.at(-1) && run.id !== node.reused) return
    if (run.execution === 'interrupted' && this.stopping) return
    if (run.execution !== 'exited_zero') {
      this.setNode(pipelineId, { ...node, state: 'failed', reason: { code: 'PIPELINE_RUN_FAILED', params: { run: run.id, execution: run.execution, error: run.error } } })
      return
    }
    const spec = this.specOf(record, nodeId)
    try {
      const checks = spec.kind === 'run' ? (spec.spec.judge ?? []) : spec.kind === 'test' ? [spec.spec.check] : []
      if (spec.kind === 'run') {
        const bound = this.workflow.project(record.project, record.branch).slots
        for (const [output, slot] of Object.entries(strings(spec.spec.bind))) {
          const hash = run.outputs.find((o) => o.name === output)?.hash
          if (!hash) throw new KernelError('PIPELINE_OUTPUT_MISSING', `run ${run.id} has no output ${output}`, { output })
          // A hard constraint a person bound is not replaced by a pipeline; it binds one only into an empty slot.
          if (this.workflow.hardConstraintSlot(record.project, slot) && bound[slot] && bound[slot] !== hash) {
            throw new KernelError('PIPELINE_HARD_CONSTRAINT_BOUND', `slot ${slot} holds a hard constraint already; bind the new one by hand`, { slot, hash })
          }
          this.workflow.bind(record.project, record.branch, slot, hash, record.by)
        }
      }
      const verdicts = checks.map((check) => this.workflow.evaluate(check, run.id, record.by, record.project))
      const failed = verdicts.find((v) => v.verdict !== 'pass')
      this.setNode(pipelineId, {
        ...node,
        state: failed ? 'failed' : 'done',
        verdicts: verdicts.map((v) => ({ check: v.check, verdict: v.verdict, id: v.id })),
        reason: failed ? { code: 'PIPELINE_CHECK_FAILED', params: { check: failed.check, verdict: failed.verdict } } : null,
      })
    } catch (error) {
      this.setNode(pipelineId, { ...node, state: 'failed', reason: reasonOf(error) })
    }
  }

  /** A finished, successful run of the same handler, params and inputs in the project, the latest one. */
  private reusable(project: string, plugin: string, handler: string, params: Record<string, unknown>, inputs: RunRef[]): RunRecord | undefined {
    return this.scheduler
      .list({ project, state: 'finished', limit: 100_000 })
      .filter((r) => r.execution === 'exited_zero' && sameWork(r, plugin, handler, params, inputs))
      .at(-1)
  }

  private takesResume(plugin: string, handler: string): boolean {
    return this.registry.get(plugin)?.manifest.handlers?.find((h) => h.name === handler)?.inputs?.some((i) => i.name === 'resume') ?? false
  }

  private setNode(pipelineId: string, node: PipelineNode): void {
    const record = this.get(pipelineId)
    this.events.append({ kind: 'pipeline.node', payload: { pipeline: pipelineId, node }, actor: KERNEL_ACTOR, project: record.project })
  }

  // ---- building and resolving ---------------------------------------------

  /** The stages, nodes and node specs of a pipeline, from the flow pack. */
  private build(flow: FlowPack, stage: string, kind: PipelineKind): { stages: string[]; nodes: PipelineNode[]; specs: Map<string, NodeSpec> } {
    const declares = (s: FlowPack['stages'][number]) => (kind === 'develop' ? !!s.develop?.nodes?.length || !!Object.keys(s.develop?.defaults ?? {}).length : !!s.tests?.length)
    let stages: FlowPack['stages'][number][]
    if (stage === 'all') stages = flow.stages.filter(declares)
    else {
      const found = flow.stages.find((s) => s.id === stage)
      if (!found) throw new KernelError('STAGE_NOT_FOUND', `flow ${flow.id} has no stage ${stage}`, { stage })
      stages = declares(found) ? [found] : []
    }
    const nodes: PipelineNode[] = []
    const specs = new Map<string, NodeSpec>()
    const add = (id: string, spec: NodeSpec, after: string[]) => {
      specs.set(id, spec)
      nodes.push({ id, stage: spec.stage, kind: spec.kind, after, state: 'pending', runs: [], reused: null, verdicts: [], reason: null })
    }
    let previous: string[] = []
    for (const s of stages) {
      const ids: string[] = []
      if (kind === 'test') {
        for (const test of s.tests ?? []) add(`${s.id}/${test.check}`, { kind: 'test', stage: s.id, spec: test }, [])
        continue
      }
      const defaults = `${s.id}/defaults`
      add(defaults, { kind: 'defaults', stage: s.id, defaults: strings(s.develop?.defaults) }, previous)
      ids.push(defaults)
      const local = (n: PipelineNodeSpec) => [...new Set([...(n.after ?? []), ...inputRefs(n.inputs).filter((r) => r.startsWith('node:')).map((r) => r.slice(5).split('.')[0]!)])].map((d) => `${s.id}/${d}`)
      const before = (s.develop?.nodes ?? []).filter((n) => n.beforeEntry)
      for (const n of before) {
        add(`${s.id}/${n.id}`, { kind: 'run', stage: s.id, spec: n }, [defaults, ...local(n)])
        ids.push(`${s.id}/${n.id}`)
      }
      const enter = `${s.id}/enter`
      add(enter, { kind: 'enter', stage: s.id }, [defaults, ...before.map((n) => `${s.id}/${n.id}`)])
      ids.push(enter)
      for (const n of (s.develop?.nodes ?? []).filter((n) => !n.beforeEntry)) {
        add(`${s.id}/${n.id}`, { kind: 'run', stage: s.id, spec: n }, [enter, ...local(n)])
        ids.push(`${s.id}/${n.id}`)
      }
      previous = ids
    }
    for (const node of nodes) {
      const unknown = node.after.find((d) => !specs.has(d))
      if (unknown) throw new KernelError('PIPELINE_INVALID', `node ${node.id} depends on ${unknown}, which the stage does not declare`, { node: node.id })
    }
    const cycle = findCycle(nodes)
    if (cycle) throw new KernelError('PIPELINE_INVALID', `nodes depend on each other in a cycle: ${cycle.join(' -> ')}`, { node: cycle[0]! })
    return { stages: stages.map((s) => s.id), nodes, specs }
  }

  private specOf(record: PipelineRecord, nodeId: string): NodeSpec {
    const spec = this.build(this.workflow.flow(record.project), record.stages.length === 1 ? record.stages[0]! : 'all', record.kind).specs.get(nodeId)
    if (!spec) throw new KernelError('PIPELINE_INVALID', `the flow pack no longer declares node ${nodeId}`, { node: nodeId })
    return spec
  }

  private resolveInputs(record: PipelineRecord, stage: string, spec: PipelineNodeSpec['inputs']): RunRef[] {
    const inputs: RunRef[] = []
    for (const [name, value] of Object.entries(spec ?? {})) {
      if (value === undefined) continue
      for (const ref of Array.isArray(value) ? value : [value]) inputs.push({ name, hash: this.resolve(record, stage, ref) })
    }
    return inputs
  }

  /** The artifact a reference stands for now. */
  private resolve(record: PipelineRecord, stage: string, ref: string): string {
    let hash: string | undefined
    if (ref.startsWith('slot:')) hash = this.workflow.project(record.project, record.branch).slots[ref.slice(5)]
    else if (ref.startsWith('data:')) hash = this.registry.dataHash(ref.slice(5))
    else if (ref.startsWith('node:')) {
      const [node, output] = ref.slice(5).split('.') as [string, string]
      const runId = record.nodes.find((n) => n.id === `${stage}/${node}`)?.runs.at(-1)
      hash = runId ? this.scheduler.get(runId).outputs.find((o) => o.name === output)?.hash : undefined
    }
    if (!hash) throw new KernelError('PIPELINE_INPUT_MISSING', `${ref} is not available`, { ref })
    return hash
  }
}

/** A dependency cycle among the nodes, as node ids, or undefined. */
function findCycle(nodes: PipelineNode[]): string[] | undefined {
  const after = new Map(nodes.map((n) => [n.id, n.after]))
  const state = new Map<string, 'open' | 'done'>()
  const path: string[] = []
  const visit = (id: string): string[] | undefined => {
    if (state.get(id) === 'done') return undefined
    if (state.get(id) === 'open') return [...path.slice(path.indexOf(id)), id]
    state.set(id, 'open')
    path.push(id)
    for (const dep of after.get(id) ?? []) {
      const cycle = visit(dep)
      if (cycle) return cycle
    }
    path.pop()
    state.set(id, 'done')
    return undefined
  }
  for (const node of nodes) {
    const cycle = visit(node.id)
    if (cycle) return cycle
  }
  return undefined
}

function inputRefs(inputs: PipelineNodeSpec['inputs']): string[] {
  return Object.values(inputs ?? {}).flatMap((v) => (v === undefined ? [] : Array.isArray(v) ? v : [v]))
}

/** A string map from the generated types (whose values may be `undefined`), without the undefined entries. */
function strings(map: Record<string, string | undefined> | undefined): Record<string, string> {
  return Object.fromEntries(Object.entries(map ?? {}).filter((e): e is [string, string] => e[1] !== undefined))
}

/** Whether a run did the same work: handler, params (with defaults) and inputs (`resume` aside). */
function sameWork(run: RunRecord, plugin: string, handler: string, params: Record<string, unknown>, inputs: RunRef[]): boolean {
  const key = (refs: RunRef[]) => canonicalJson(refs.filter((r) => r.name !== 'resume').map((r) => `${r.name}=${r.hash}`).sort())
  return run.plugin === plugin && run.handler === handler && canonicalJson(run.params) === canonicalJson(params) && key(run.inputs) === key(inputs)
}

function reasonOf(error: unknown): Message {
  if (error instanceof KernelError) return { code: error.code, params: { ...(error.params ?? {}), message: error.message } }
  return { code: 'PIPELINE_NODE_ERROR', params: { message: String(error) } }
}

export function summarize(nodes: PipelineNode[]): PipelineSummary {
  const verdicts = nodes.flatMap((n) => (n.kind === 'test' ? n.verdicts : []))
  const count = (state: PipelineNode['state']) => nodes.filter((n) => n.state === state).length
  return {
    nodes: nodes.length,
    done: count('done'),
    failed: count('failed'),
    skipped: count('skipped'),
    cancelled: count('cancelled'),
    pass: verdicts.filter((v) => v.verdict === 'pass').length,
    fail: verdicts.filter((v) => v.verdict === 'fail').length,
    indeterminate: verdicts.filter((v) => v.verdict === 'indeterminate').length,
  }
}
