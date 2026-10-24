import { readFileSync } from 'node:fs'
import type {
  Actor,
  CheckDefinition,
  CheckOverride,
  ConstraintProposal,
  Criterion,
  CriterionCondition,
  FlowPack,
  Message,
  MigrationApplyResult,
  ProjectRecord,
  ProjectState,
  RunRecord,
  VerdictRecord,
} from '@llmchip/kernel-api'
import type { ArtifactStore } from '../artifacts/store.ts'
import type { EventStore } from '../events/store.ts'
import type { PluginRegistry } from '../plugins/registry.ts'
import type { Scheduler } from '../runs/scheduler.ts'
import type { Incidents } from '../security/incidents.ts'
import { canonicalJson, sha256 } from '../util/canonical.ts'
import { KernelError } from '../util/errors.ts'
import { assertValid } from '../util/schema.ts'
import { ulid } from '../util/ulid.ts'
import { slotsAt, workflowProjection } from './projection.ts'

/** Artifact type of flow pack data files. */
export const FLOW_PACK_TYPE = 'flow.pack'
/** Artifact type of a migration impact assessment (FR-MIG). */
export const MIGRATION_REPORT_TYPE = 'migration.report'
/** Stages from which a migration needs the owner's approval (FR-MIG-04). */
const OWNER_MIGRATION_STAGES = new Set(['S0', 'S1', 'S2', 'S3'])

/**
 * Workflow and verdicts: K3 (projects, stages, slot bindings, branches) and K6
 * (verdict engine). Flow packs, which define stages and check definitions,
 * come from data plugins (architecture AP-2). There are no approval gates and
 * no waivers (owner decision 2026-10-24): a failed check stays failed until
 * the cause is fixed and the work and its tests run again.
 */
export class Workflow {
  private readonly events: EventStore
  private readonly artifacts: ArtifactStore
  private readonly registry: PluginRegistry
  private readonly scheduler: Scheduler
  private readonly incidents: Incidents
  private flows = new Map<string, { pack: FlowPack; hash: string }>()
  private checkDefs = new Map<string, CheckDefinition>()

  constructor(options: { events: EventStore; artifacts: ArtifactStore; registry: PluginRegistry; scheduler: Scheduler; incidents: Incidents }) {
    this.incidents = options.incidents
    this.events = options.events
    this.artifacts = options.artifacts
    this.registry = options.registry
    this.scheduler = options.scheduler
    this.events.registerProjection(workflowProjection)
  }

  /** Loads flow packs and their check definitions from the registered data plugins. */
  loadFlows(): Message[] {
    const problems: Message[] = []
    this.flows = new Map()
    this.checkDefs = new Map()
    for (const { plugin, hash } of this.registry.dataOfType(FLOW_PACK_TYPE)) {
      try {
        const pack = this.readFlow(hash)
        if (this.flows.has(pack.id)) throw new KernelError('FLOW_DUPLICATE', `flow pack ${pack.id} is defined twice`)
        this.flows.set(pack.id, { pack, hash })
        for (const check of pack.checks ?? []) {
          const existing = this.checkDefs.get(check.id)
          if (existing && canonicalJson(existing) !== canonicalJson(check)) {
            throw new KernelError('CHECK_CONFLICT', `check ${check.id} has conflicting definitions`)
          }
          this.checkDefs.set(check.id, check)
        }
      } catch (error) {
        problems.push({ code: 'FLOW_INVALID', params: { plugin, error: (error as Error).message } })
      }
    }
    return problems
  }

  checks(): CheckDefinition[] {
    return [...this.checkDefs.values()]
  }

  // ---- K6: verdict engine ------------------------------------------------

  /**
   * Judges a finished run against a check definition (requirements §3.9,
   * DP-8, DP-10). A run that did not execute acceptably, a report that was
   * not parsed completely, an unqualified tool capability, an unmet coverage
   * condition or a missing metric all give `indeterminate`, never `pass`.
   */
  evaluate(checkId: string, runId: string, actor: Actor, project?: string): VerdictRecord {
    const run = this.scheduler.get(runId)
    const projectId = project || run.project || ''
    const check = this.effectiveCheck(checkId, projectId)
    if (run.state !== 'finished') throw new KernelError('RUN_NOT_FINISHED', `run ${runId} has not finished`, { run: runId })
    if (run.plugin !== check.plugin || run.handler !== check.handler) {
      throw new KernelError('CHECK_RUN_MISMATCH', `check ${checkId} judges ${check.plugin}/${check.handler}, run ${runId} is ${run.plugin}/${run.handler}`)
    }
    const subjects = [...new Set(run.inputs.map((i) => i.hash))].sort()
    const reasons: Message[] = []
    const verdict = this.judge(check, run, reasons)
    const record: VerdictRecord = {
      id: ulid(),
      check: check.id,
      checkVersion: check.version,
      subjects,
      run: run.id,
      parse: run.parse?.status ?? null,
      verdict,
      reasons,
      reqRef: null,
      createdAt: new Date().toISOString(),
    }
    const previous = this.verdicts({ check: check.id, limit: 1000 }).find((v) => canonicalJson(v.subjects) === canonicalJson(subjects))
    this.events.append({ kind: 'verdict.recorded', payload: { verdict: record }, actor, project: projectId || null, label: run.label })
    // A failed or undecidable check is an incident, with the run's localization of the
    // first divergence when it reports one (requirements FR-VER-06, AC-09).
    if (verdict === 'fail' || verdict === 'indeterminate') {
      const divergence = Object.fromEntries(Object.entries(run.metrics).filter(([key]) => key.startsWith('divergence_')))
      this.incidents.open({
        kind: 'check_failed',
        severity: 'warning',
        summary: { code: 'divergence_operation' in divergence ? 'CHECK_FAILED_DIVERGENCE' : 'CHECK_FAILED', params: { check: check.id, verdict, verdictId: record.id, ...divergence } },
        actor,
        project: projectId || null,
        refs: { artifacts: subjects, runs: [run.id] },
      })
    }
    // A check that newly passes only because its standard changed must be reviewed by a human (AC-M1-13).
    if (verdict === 'pass' && previous && previous.verdict !== 'pass' && previous.checkVersion !== record.checkVersion) {
      this.incidents.open({
        kind: 'standard_lowered',
        severity: 'critical',
        summary: { code: 'CHECK_PASSES_AFTER_STANDARD_CHANGE', params: { check: check.id, before: previous.checkVersion, after: record.checkVersion } },
        actor,
        project: projectId || null,
        refs: { artifacts: subjects, runs: [run.id] },
      })
    }
    return record
  }

  // ---- hard constraints: project check overrides (requirements §3.2) ------

  /**
   * Requests a project-specific change of a check's standards. Check standards
   * are hard constraints: a human's change applies at once; anyone else's is
   * recorded as a pending proposal and as an incident, and applies only after
   * a human approves it.
   */
  overrideCheck(projectId: string, checkId: string, override: CheckOverride, reason: string, actor: Actor): ConstraintProposal {
    this.requireProject(projectId)
    this.requireCheck(checkId)
    const proposal: ConstraintProposal = {
      id: ulid(),
      project: projectId,
      check: checkId,
      override,
      reason,
      by: actor,
      at: new Date().toISOString(),
      status: 'pending',
    }
    this.events.transaction(() => {
      this.events.append({ kind: 'constraint.proposed', payload: { proposal }, actor, project: projectId })
      if (actor.kind === 'human') {
        this.events.append({ kind: 'constraint.decided', payload: { proposal: proposal.id, decision: 'approved', comment: 'set by a human' }, actor, project: projectId })
      } else {
        this.incidents.open({
          kind: 'hard_constraint_change',
          severity: 'warning',
          summary: { code: 'HARD_CONSTRAINT_CHANGE_REQUESTED', params: { check: checkId, by: `${actor.kind}:${actor.id}` } },
          actor,
          project: projectId,
        })
      }
    })
    return this.proposal(proposal.id)
  }

  decideConstraint(proposalId: string, decision: 'approved' | 'rejected', comment: string, actor: Actor): ConstraintProposal {
    if (actor.kind !== 'human') throw new KernelError('APPROVAL_REQUIRES_HUMAN', 'only a human can change a hard constraint')
    const proposal = this.proposal(proposalId)
    if (proposal.status !== 'pending') throw new KernelError('PROPOSAL_DECIDED', `proposal ${proposalId} is already ${proposal.status}`)
    this.events.append({ kind: 'constraint.decided', payload: { proposal: proposalId, decision, comment }, actor, project: proposal.project })
    return this.proposal(proposalId)
  }

  constraints(projectId?: string): ConstraintProposal[] {
    const rows = (
      projectId
        ? this.events.db.prepare('SELECT record FROM constraint_proposals WHERE project = ? ORDER BY seq').all(projectId)
        : this.events.db.prepare('SELECT record FROM constraint_proposals ORDER BY seq').all()
    ) as { record: string }[]
    return rows.map((r) => JSON.parse(r.record))
  }

  /** A check with the project's latest approved override applied; its version identifies the standard. */
  effectiveCheck(checkId: string, projectId: string): CheckDefinition {
    const check = this.requireCheck(checkId)
    const row = this.events.db
      .prepare("SELECT record FROM constraint_proposals WHERE project = ? AND check_id = ? AND status = 'approved' ORDER BY seq DESC LIMIT 1")
      .get(projectId, checkId) as { record: string } | undefined
    if (!row) return check
    const { override } = JSON.parse(row.record) as ConstraintProposal
    return { ...check, ...override, version: `${check.version}+${sha256(canonicalJson(override)).slice(0, 12)}` }
  }

  private proposal(id: string): ConstraintProposal {
    const row = this.events.db.prepare('SELECT record FROM constraint_proposals WHERE id = ?').get(id) as { record: string } | undefined
    if (!row) throw new KernelError('PROPOSAL_NOT_FOUND', `constraint proposal ${id} does not exist`, { proposal: id })
    return JSON.parse(row.record)
  }

  verdicts(options: { check?: string; subject?: string; limit?: number } = {}): VerdictRecord[] {
    const rows = (
      options.check
        ? this.events.db.prepare('SELECT record FROM verdicts WHERE check_id = ? ORDER BY seq DESC').all(options.check)
        : this.events.db.prepare('SELECT record FROM verdicts ORDER BY seq DESC').all()
    ) as { record: string }[]
    return rows
      .map((r) => JSON.parse(r.record) as VerdictRecord)
      .filter((v) => !options.subject || v.subjects.includes(options.subject))
      .slice(0, options.limit ?? 100)
  }

  private judge(check: CheckDefinition, run: RunRecord, reasons: Message[]): VerdictRecord['verdict'] {
    const accepted = run.execution === 'exited_zero' || (run.exitCode !== null && (check.acceptExitCodes ?? [0]).includes(run.exitCode) && run.execution === 'exited_nonzero')
    if (!accepted) {
      reasons.push({ code: 'EXECUTION_NOT_ACCEPTED', params: { execution: run.execution, exitCode: run.exitCode } })
      return 'indeterminate'
    }
    if (check.requireParse !== false && run.parse?.status !== 'complete') {
      reasons.push({ code: 'PARSE_INCOMPLETE', params: { parse: run.parse?.status ?? null } })
      return 'indeterminate'
    }
    if (check.requiredCapability && !this.qualified(run.plugin, check.requiredCapability)) {
      reasons.push({ code: 'CAPABILITY_NOT_QUALIFIED', params: { plugin: run.plugin, capability: check.requiredCapability } })
      return 'indeterminate'
    }
    for (const criterion of check.completeness ?? []) {
      const applies = applicable(criterion, run.metrics, reasons)
      if (applies === undefined) return 'indeterminate'
      if (!applies) continue
      const outcome = evaluateCriterion(criterion, run.metrics)
      if (outcome !== true) {
        reasons.push({ code: 'COVERAGE_INCOMPLETE', params: { ...comparison(criterion), actual: run.metrics[criterion.metric] ?? null } })
        return 'indeterminate'
      }
    }
    let failed = false
    for (const criterion of check.criteria) {
      const applies = applicable(criterion, run.metrics, reasons)
      if (applies === undefined) return 'indeterminate'
      if (!applies) continue
      const outcome = evaluateCriterion(criterion, run.metrics)
      if (outcome === undefined) {
        reasons.push({ code: 'METRIC_MISSING', params: { metric: criterion.metric } })
        return 'indeterminate'
      }
      if (!outcome) {
        failed = true
        reasons.push({ code: 'CRITERION_FAILED', params: { ...comparison(criterion), actual: run.metrics[criterion.metric] ?? null } })
      }
    }
    return failed ? 'fail' : 'pass'
  }

  /**
   * A capability is qualified when its qualification check, `<plugin>.qualify.<capability>`,
   * has a passing verdict for a run of that plugin (requirements FR-VER-01).
   */
  private qualified(pluginId: string, capability: string): boolean {
    const plugin = this.registry.get(pluginId)
    if (!plugin?.manifest.capabilities?.some((c) => c.name === capability)) return false
    return this.verdicts({ check: `${pluginId}.qualify.${capability}`, limit: 1000 }).some((v) => v.verdict === 'pass')
  }

  // ---- K3: projects, stages, slots, branches -----------------------------

  createProject(id: string, title: string, flowId: string, actor: Actor): ProjectRecord {
    if (this.findProject(id)) throw new KernelError('PROJECT_EXISTS', `project ${id} already exists`, { project: id })
    const flow = this.flows.get(flowId)
    if (!flow) throw new KernelError('FLOW_NOT_FOUND', `flow pack ${flowId} is not registered`, { flow: flowId })
    const project: ProjectRecord = { id, title, flow: flowId, flowHash: flow.hash, createdAt: new Date().toISOString() }
    this.events.append({ kind: 'project.created', payload: { project }, actor, project: id })
    return project
  }

  projects(): ProjectRecord[] {
    return (this.events.db.prepare('SELECT record FROM projects ORDER BY id').all() as { record: string }[]).map((r) => JSON.parse(r.record))
  }

  project(id: string, branch = 'main'): ProjectState {
    const project = this.requireProject(id)
    this.requireBranch(id, branch)
    const pack = this.projectFlow(project)
    const entered = new Set((this.events.db.prepare('SELECT stage FROM stages WHERE project = ? AND branch = ?').all(id, branch) as { stage: string }[]).map((r) => r.stage))
    return {
      project,
      branches: (this.events.db.prepare('SELECT branch FROM branches WHERE project = ? ORDER BY seq').all(id) as { branch: string }[]).map((r) => r.branch),
      branch,
      slots: Object.fromEntries(slotsAt(this.events.db, id, branch).map((s) => [s.slot, s.hash])),
      stages: pack.stages.map((s) => ({ id: s.id, entered: entered.has(s.id) })),
    }
  }

  /** Binds a project slot to an artifact. */
  bind(projectId: string, branch: string, slot: string, hash: string, actor: Actor): void {
    this.requireProject(projectId)
    this.requireBranch(projectId, branch)
    const meta = this.artifacts.require(hash)
    const previous = slotsAt(this.events.db, projectId, branch).find((s) => s.slot === slot)?.hash ?? null
    // Hard constraints (for example the timing exception registry): an agent may propose
    // an artifact, only a human binds it (requirements §3.2, FR-SGN-02b).
    const project = this.requireProject(projectId)
    if (actor.kind !== 'human' && (this.projectFlow(project).hardConstraintSlots ?? []).includes(slot)) {
      this.incidents.open({
        kind: 'hard_constraint_change',
        severity: 'warning',
        summary: { code: 'HARD_CONSTRAINT_REQUIRES_HUMAN', params: { slot, by: actor.id, hash } },
        actor,
        project: projectId,
        refs: { artifacts: [hash] },
      })
      throw new KernelError('HARD_CONSTRAINT_REQUIRES_HUMAN', `slot ${slot} holds a hard constraint; only a human may bind it`, { slot })
    }
    if (previous === hash) return
    this.events.append({ kind: 'project.slot_bound', payload: { project: projectId, branch, slot, hash, previous }, actor, project: projectId, label: meta.label })
  }

  /** Creates a branch from another branch's bindings at a checkpoint (FR-TRC-04, FR-PRJ-04). */
  branch(projectId: string, from: string, name: string, atSeq: number | undefined, actor: Actor): void {
    this.requireProject(projectId)
    this.requireBranch(projectId, from)
    if (this.branchExists(projectId, name)) throw new KernelError('BRANCH_EXISTS', `branch ${name} already exists`, { branch: name })
    const seq = atSeq ?? this.events.head()
    this.events.append({ kind: 'project.branched', payload: { project: projectId, branch: name, from, atSeq: seq }, actor, project: projectId })
  }

  /** Enters a stage when every admission check of the stage passes on the artifact bound to its slot. */
  enterStage(projectId: string, branch: string, stageId: string, actor: Actor): void {
    const project = this.requireProject(projectId)
    this.requireBranch(projectId, branch)
    const stage = this.projectFlow(project).stages.find((s) => s.id === stageId)
    if (!stage) throw new KernelError('STAGE_NOT_FOUND', `flow ${project.flow} has no stage ${stageId}`, { stage: stageId })
    // Stage admission checks on bound artifacts (requirements FR-PHY-08): the latest
    // verdict of each check on the artifact bound to the slot must pass.
    const bound = new Map(slotsAt(this.events.db, projectId, branch).map((s) => [s.slot, s.hash]))
    const failed: { check: string; slot: string; verdict: string; reasons: Message[]; details: Record<string, unknown> }[] = []
    for (const { check, slot } of stage.entry?.checks ?? []) {
      const hash = bound.get(slot)
      const latest = hash ? this.verdicts({ check, subject: hash, limit: 1 })[0] : undefined
      if (latest?.verdict === 'pass') continue
      // Explanations the judged run reported, such as `views.S4.missing`.
      const metrics = latest?.run ? this.scheduler.get(latest.run).metrics : {}
      // Only explanations for this stage (or not stage-specific at all).
      const relevant = (key: string) => key.endsWith('.missing') && (key.includes(`.${stageId}.`) || !/\.S\d+\./.test(key))
      const details = Object.fromEntries(Object.entries(metrics).filter(([key, value]) => relevant(key) && value !== ''))
      failed.push({ check, slot, verdict: latest?.verdict ?? (hash ? 'missing' : 'unbound'), reasons: latest?.reasons ?? [], details })
    }
    if (failed.length > 0) {
      const parts = failed.map((f) => `${f.check} on ${f.slot} (${f.verdict}${Object.keys(f.details).length ? `; ${Object.values(f.details).join('; ')}` : ''})`)
      throw new KernelError('STAGE_ENTRY_BLOCKED', `stage ${stageId} needs ${parts.join(', ')}`, { stage: stageId, checks: failed })
    }
    this.events.append({ kind: 'stage.entered', payload: { project: projectId, branch, stage: stageId }, actor, project: projectId })
  }

  // ---- FR-MIG: confirmed migration impact assessments ---------------------

  /**
   * Records a confirmed migration impact assessment and the earliest stage to
   * restart (FR-MIG-03): the work of that stage and every later one has to run
   * again on the new process. Restarting at S3 or earlier is the owner's
   * decision (FR-MIG-04).
   */
  applyMigration(projectId: string, branch: string, reportHash: string, comment: string, actor: Actor): MigrationApplyResult {
    const project = this.requireProject(projectId)
    this.requireBranch(projectId, branch)
    const meta = this.artifacts.require(reportHash)
    if (meta.type !== MIGRATION_REPORT_TYPE) throw new KernelError('MIGRATION_REPORT_INVALID', `artifact ${reportHash} is not a ${MIGRATION_REPORT_TYPE}`)
    const report = JSON.parse(readFileSync(this.artifacts.objectPath(reportHash), 'utf8')) as { restartStage?: string | null }
    const stages = this.projectFlow(project).stages.map((s) => s.id)
    const restart = report.restartStage
    if (!restart) return { restartStage: 'none' }
    if (!stages.includes(restart)) throw new KernelError('MIGRATION_REPORT_INVALID', `unknown restart stage ${restart}`)
    if (OWNER_MIGRATION_STAGES.has(restart) && actor.kind !== 'human') {
      throw new KernelError('MIGRATION_REQUIRES_OWNER', `restarting at ${restart} needs the owner's approval`, { restartStage: restart })
    }
    this.events.append({
      kind: 'migration.applied',
      payload: { project: projectId, branch, report: reportHash, restartStage: restart, ...(comment ? { comment } : {}) },
      actor,
      project: projectId,
    })
    return { restartStage: restart }
  }

  // ---- helpers -----------------------------------------------------------

  /**
   * Reads a stored flow pack. A registered pack must match the schema as it is (no gates, no
   * waivers); the pack a project was created with may come from before approval gates and
   * waivers were removed (owner decision 2026-10-24), so its history-only fields are dropped.
   */
  private readFlow(hash: string, history = false): FlowPack {
    const pack = JSON.parse(readFileSync(this.artifacts.objectPath(hash), 'utf8'))
    assertValid<FlowPack>('FlowPack', history ? withoutHistory(pack) : pack, 'FLOW_INVALID')
    return pack
  }

  /** The flow pack a project was created with. */
  private projectFlow(project: ProjectRecord): FlowPack {
    return this.readFlow(project.flowHash, true)
  }

  private findProject(id: string): ProjectRecord | undefined {
    const row = this.events.db.prepare('SELECT record FROM projects WHERE id = ?').get(id) as { record: string } | undefined
    return row ? JSON.parse(row.record) : undefined
  }

  private requireProject(id: string): ProjectRecord {
    const project = this.findProject(id)
    if (!project) throw new KernelError('PROJECT_NOT_FOUND', `project ${id} does not exist`, { project: id })
    return project
  }

  private branchExists(project: string, branch: string): boolean {
    return this.events.db.prepare('SELECT 1 FROM branches WHERE project = ? AND branch = ?').get(project, branch) !== undefined
  }

  private requireBranch(project: string, branch: string): void {
    if (!this.branchExists(project, branch)) throw new KernelError('BRANCH_NOT_FOUND', `project ${project} has no branch ${branch}`, { branch })
  }

  private requireCheck(id: string): CheckDefinition {
    const check = this.checkDefs.get(id)
    if (!check) throw new KernelError('CHECK_NOT_FOUND', `check ${id} is not defined by any flow pack`, { check: id })
    return check
  }
}

/**
 * A flow pack written before approval gates and waivers were removed, without its history-only
 * fields: `gates`, the stages' `entry.gates`, and the checks' `waiver`. Mutates and returns `pack`.
 */
function withoutHistory(pack: Record<string, any>): Record<string, any> {
  delete pack.gates
  for (const stage of pack.stages ?? []) {
    if (stage.entry) {
      delete stage.entry.gates
      if (Object.keys(stage.entry).length === 0) delete stage.entry
    }
  }
  for (const check of pack.checks ?? []) delete check.waiver
  return pack
}

/**
 * Whether a conditional criterion applies (`when`). A condition that does not hold is recorded
 * as CRITERION_NOT_APPLIED; a missing condition metric is a missing metric (indeterminate).
 */
function applicable(criterion: Criterion, metrics: RunRecord['metrics'], reasons: Message[]): boolean | undefined {
  if (!criterion.when) return true
  const holds = evaluateCriterion(criterion.when, metrics)
  if (holds === undefined) {
    reasons.push({ code: 'METRIC_MISSING', params: { metric: criterion.when.metric } })
    return undefined
  }
  if (!holds) {
    const when = criterion.when
    reasons.push({ code: 'CRITERION_NOT_APPLIED', params: { ...comparison(criterion), condition: `${when.metric} ${when.op} ${String(when.value)}`, actual: metrics[when.metric] ?? null } })
  }
  return holds
}

/** The comparison of a criterion, without its condition (message parameters). */
function comparison(criterion: Criterion): CriterionCondition {
  return { metric: criterion.metric, op: criterion.op, value: criterion.value }
}

/** true / false, or undefined when the metric is missing or not comparable. */
function evaluateCriterion(criterion: Criterion | CriterionCondition, metrics: RunRecord['metrics']): boolean | undefined {
  const actual = metrics[criterion.metric]
  if (actual === undefined) return undefined
  const expected = criterion.value
  switch (criterion.op) {
    case '==':
      return actual === expected
    case '!=':
      return actual !== expected
    default:
      if (typeof actual !== 'number' || typeof expected !== 'number') return undefined
      if (criterion.op === '<') return actual < expected
      if (criterion.op === '<=') return actual <= expected
      if (criterion.op === '>') return actual > expected
      return actual >= expected
  }
}
