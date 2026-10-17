import { readFileSync } from 'node:fs'
import {
  type Actor,
  type ApprovalRecord,
  type ApprovalStatus,
  type ApprovalsDecideParams,
  type CheckDefinition,
  type Criterion,
  type FlowGate,
  type FlowPack,
  type Message,
  type ProjectRecord,
  type ProjectState,
  type ReleaseResult,
  type RunRecord,
  type Snapshot,
  type SnapshotMember,
  type VerdictRecord,
  type WaiverRecord,
  type WaiversCreateParams,
} from '@llmchip/kernel-api'
import type { ArtifactStore } from '../artifacts/store.ts'
import type { EventStore } from '../events/store.ts'
import type { PluginRegistry } from '../plugins/registry.ts'
import type { Scheduler } from '../runs/scheduler.ts'
import { canonicalJson, sha256 } from '../util/canonical.ts'
import { KernelError } from '../util/errors.ts'
import { assertValid } from '../util/schema.ts'
import { ulid } from '../util/ulid.ts'
import { slotsAt, workflowProjection } from './projection.ts'

/** Artifact type of flow pack data files. */
export const FLOW_PACK_TYPE = 'flow.pack'
/** Artifact type of a foundry's written acknowledgement of a waiver. */
export const FOUNDRY_ACK_TYPE = 'foundry.waiver_ack'

const KERNEL_ACTOR: Actor = { kind: 'kernel', id: 'workflow' }

/**
 * Workflow, verdicts and approvals: K3 (projects, stages, slot bindings,
 * branches, invalidation), K6 (verdict engine) and K7 (snapshots, approvals,
 * conditions, waivers, the release check). Flow packs, which define stages,
 * gates and check definitions, come from data plugins (architecture AP-2).
 */
export class Workflow {
  private readonly events: EventStore
  private readonly artifacts: ArtifactStore
  private readonly registry: PluginRegistry
  private readonly scheduler: Scheduler
  private flows = new Map<string, { pack: FlowPack; hash: string }>()
  private checkDefs = new Map<string, CheckDefinition>()

  constructor(options: { events: EventStore; artifacts: ArtifactStore; registry: PluginRegistry; scheduler: Scheduler }) {
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
    const check = this.requireCheck(checkId)
    const run = this.scheduler.get(runId)
    if (run.state !== 'finished') throw new KernelError('RUN_NOT_FINISHED', `run ${runId} has not finished`, { run: runId })
    if (run.plugin !== check.plugin || run.handler !== check.handler) {
      throw new KernelError('CHECK_RUN_MISMATCH', `check ${checkId} judges ${check.plugin}/${check.handler}, run ${runId} is ${run.plugin}/${run.handler}`)
    }
    const subjects = [...new Set(run.inputs.map((i) => i.hash))].sort()
    const reasons: Message[] = []
    let verdict = this.judge(check, run, reasons)
    let waiver: string | null = null
    if (verdict === 'fail' && check.waiver !== 'none') {
      const applicable = this.findWaiver(check, subjects)
      if (applicable) {
        verdict = 'waived'
        waiver = applicable.id
        reasons.push({ code: 'WAIVED', params: { waiver: applicable.id, kind: applicable.kind } })
      }
    }
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
      waiver,
      createdAt: new Date().toISOString(),
    }
    this.events.append({ kind: 'verdict.recorded', payload: { verdict: record }, actor, project: project ?? run.project, label: run.label })
    return record
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
      const outcome = evaluateCriterion(criterion, run.metrics)
      if (outcome !== true) {
        reasons.push({ code: 'COVERAGE_INCOMPLETE', params: { ...criterion, actual: run.metrics[criterion.metric] ?? null } })
        return 'indeterminate'
      }
    }
    let failed = false
    for (const criterion of check.criteria) {
      const outcome = evaluateCriterion(criterion, run.metrics)
      if (outcome === undefined) {
        reasons.push({ code: 'METRIC_MISSING', params: { metric: criterion.metric } })
        return 'indeterminate'
      }
      if (!outcome) {
        failed = true
        reasons.push({ code: 'CRITERION_FAILED', params: { ...criterion, actual: run.metrics[criterion.metric] ?? null } })
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

  private findWaiver(check: CheckDefinition, subjects: string[]): WaiverRecord | undefined {
    const rows = this.events.db.prepare('SELECT record FROM waivers WHERE check_id = ?').all(check.id) as { record: string }[]
    return rows
      .map((r) => JSON.parse(r.record) as WaiverRecord)
      .find((w) => w.kind === check.waiver && canonicalJson([...w.subjects].sort()) === canonicalJson(subjects))
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
    const pack = this.readFlow(project.flowHash)
    const entered = new Set((this.events.db.prepare('SELECT stage FROM stages WHERE project = ? AND branch = ?').all(id, branch) as { stage: string }[]).map((r) => r.stage))
    return {
      project,
      branches: (this.events.db.prepare('SELECT branch FROM branches WHERE project = ? ORDER BY seq').all(id) as { branch: string }[]).map((r) => r.branch),
      branch,
      slots: Object.fromEntries(slotsAt(this.events.db, id, branch).map((s) => [s.slot, s.hash])),
      stages: pack.stages.map((s) => ({ id: s.id, entered: entered.has(s.id) })),
      gates: (pack.gates ?? []).map((g) => this.release(id, branch, g.id)),
    }
  }

  /**
   * Binds a project slot to an artifact. Approvals whose design snapshot
   * contained the slot become invalid (requirements §3.7.2, FR-TRC-05).
   */
  bind(projectId: string, branch: string, slot: string, hash: string, actor: Actor): void {
    this.requireProject(projectId)
    this.requireBranch(projectId, branch)
    const meta = this.artifacts.require(hash)
    const previous = slotsAt(this.events.db, projectId, branch).find((s) => s.slot === slot)?.hash ?? null
    if (previous === hash) return
    this.events.transaction(() => {
      this.events.append({ kind: 'project.slot_bound', payload: { project: projectId, branch, slot, hash, previous }, actor, project: projectId, label: meta.label })
      this.invalidateApprovals(projectId, branch, slot)
    })
  }

  /** Creates a branch from another branch's bindings at a checkpoint (FR-TRC-04, FR-PRJ-04). */
  branch(projectId: string, from: string, name: string, atSeq: number | undefined, actor: Actor): void {
    this.requireProject(projectId)
    this.requireBranch(projectId, from)
    if (this.branchExists(projectId, name)) throw new KernelError('BRANCH_EXISTS', `branch ${name} already exists`, { branch: name })
    const seq = atSeq ?? this.events.head()
    this.events.append({ kind: 'project.branched', payload: { project: projectId, branch: name, from, atSeq: seq }, actor, project: projectId })
  }

  /** Enters a stage if every gate its entry depends on is released. */
  enterStage(projectId: string, branch: string, stageId: string, actor: Actor): void {
    const project = this.requireProject(projectId)
    this.requireBranch(projectId, branch)
    const stage = this.readFlow(project.flowHash).stages.find((s) => s.id === stageId)
    if (!stage) throw new KernelError('STAGE_NOT_FOUND', `flow ${project.flow} has no stage ${stageId}`, { stage: stageId })
    const blocked = (stage.entry?.gates ?? []).map((gate) => this.release(projectId, branch, gate)).filter((r) => !r.released)
    if (blocked.length > 0) {
      throw new KernelError('STAGE_ENTRY_BLOCKED', `stage ${stageId} needs released gates: ${blocked.map((b) => b.gate).join(', ')}`, {
        stage: stageId,
        gates: blocked,
      })
    }
    this.events.append({ kind: 'stage.entered', payload: { project: projectId, branch, stage: stageId }, actor, project: projectId })
  }

  // ---- K7: snapshots, approvals, waivers, release ------------------------

  /** The design snapshot of a gate and its evidence manifest. */
  snapshot(projectId: string, branch: string, gateId: string): Snapshot {
    const gate = this.requireGate(projectId, gateId)
    this.requireBranch(projectId, branch)
    const bound = new Map(slotsAt(this.events.db, projectId, branch).map((s) => [s.slot, s.hash]))
    const members: SnapshotMember[] = gate.members.filter((slot) => bound.has(slot)).map((slot) => ({ slot, hash: bound.get(slot)! }))
    const missing = gate.members.filter((slot) => !bound.has(slot))
    const hashes = new Set(members.map((m) => m.hash))
    // Evidence: the latest verdict of each gate check whose subjects all belong to this snapshot.
    const evidence = gate.checks
      .map((check) => this.verdicts({ check, limit: 1000 }).find((v) => v.subjects.length > 0 && v.subjects.every((s) => hashes.has(s)))?.id)
      .filter((id): id is string => id !== undefined)
    return {
      project: projectId,
      branch,
      gate: gateId,
      members,
      hash: sha256(canonicalJson(members)),
      evidence,
      evidenceHash: sha256(canonicalJson(evidence)),
      missing,
    }
  }

  decide(params: ApprovalsDecideParams, actor: Actor): ApprovalRecord {
    if (actor.kind !== 'human') throw new KernelError('APPROVAL_REQUIRES_HUMAN', 'only a human can decide at a gate')
    const branch = params.branch ?? 'main'
    const gate = this.requireGate(params.project, params.gate)
    const snapshot = this.snapshot(params.project, branch, params.gate)
    if (snapshot.missing.length > 0) {
      throw new KernelError('SNAPSHOT_INCOMPLETE', `gate ${params.gate} needs bound slots: ${snapshot.missing.join(', ')}`, { missing: snapshot.missing })
    }
    const conditions = params.conditions ?? []
    if (params.decision === 'conditional' && conditions.length === 0) throw new KernelError('CONDITIONS_REQUIRED', 'a conditional approval needs at least one condition')
    if (params.decision !== 'conditional' && conditions.length > 0) throw new KernelError('CONDITIONS_NOT_ALLOWED', 'only conditional approvals carry conditions')
    for (const condition of conditions) this.requireCheck(condition.check)
    if (params.decision !== 'rejected') {
      const verdicts = new Map(snapshot.evidence.map((id) => [this.verdict(id).check, this.verdict(id)]))
      const unmet = gate.checks.filter((check) => !['pass', 'waived'].includes(verdicts.get(check)?.verdict ?? ''))
      if (unmet.length > 0) {
        throw new KernelError('GATE_EVIDENCE_INCOMPLETE', `gate ${params.gate} needs passing or waived checks: ${unmet.join(', ')}`, { checks: unmet })
      }
    }
    const approval: ApprovalRecord = {
      id: ulid(),
      project: params.project,
      branch,
      gate: params.gate,
      decision: params.decision,
      snapshotHash: snapshot.hash,
      evidenceHash: snapshot.evidenceHash,
      conditions: conditions.map((c) => ({ ...c, closed: null })),
      comment: params.comment ?? '',
      by: actor,
      at: new Date().toISOString(),
    }
    this.events.append({ kind: 'approval.recorded', payload: { approval }, actor, project: params.project })
    return approval
  }

  /**
   * Closes a condition of a conditional approval with a passing verdict of the
   * condition's check. Closing evidence must not change the design snapshot
   * (requirements §3.7.3).
   */
  closeCondition(approvalId: string, conditionId: string, verdictId: string, actor: Actor): ApprovalRecord {
    if (actor.kind !== 'human') throw new KernelError('APPROVAL_REQUIRES_HUMAN', 'only a human can close an approval condition')
    const approval = this.approval(approvalId)
    const condition = approval.conditions.find((c) => c.id === conditionId)
    if (!condition) throw new KernelError('CONDITION_NOT_FOUND', `approval ${approvalId} has no condition ${conditionId}`)
    if (condition.closed) return approval
    const verdict = this.verdict(verdictId)
    if (verdict.check !== condition.check || verdict.verdict !== 'pass') {
      throw new KernelError('CONDITION_EVIDENCE_INVALID', `condition ${conditionId} needs a passing verdict of ${condition.check}`, {
        check: verdict.check,
        verdict: verdict.verdict,
      })
    }
    const snapshot = this.snapshot(approval.project, approval.branch, approval.gate)
    if (snapshot.hash !== approval.snapshotHash) {
      throw new KernelError('CONDITION_SNAPSHOT_CHANGED', 'the design snapshot changed since the approval; approve the new snapshot instead')
    }
    const hashes = new Set(snapshot.members.map((m) => m.hash))
    if (!verdict.subjects.every((s) => hashes.has(s))) {
      throw new KernelError('CONDITION_EVIDENCE_INVALID', `verdict ${verdictId} does not judge the approved snapshot`)
    }
    this.events.append({ kind: 'approval.condition_closed', payload: { approval: approvalId, condition: conditionId, verdict: verdictId }, actor, project: approval.project })
    return this.approval(approvalId)
  }

  approvals(projectId: string, branch?: string): ApprovalRecord[] {
    const rows = (
      branch
        ? this.events.db.prepare('SELECT record FROM approvals WHERE project = ? AND branch = ? ORDER BY seq').all(projectId, branch)
        : this.events.db.prepare('SELECT record FROM approvals WHERE project = ? ORDER BY seq').all(projectId)
    ) as { record: string }[]
    return rows.map((r) => JSON.parse(r.record))
  }

  /**
   * The single release check before advancing the flow or exporting
   * (architecture §4.7): the latest approval of the gate must be approved, or
   * conditional with every condition closed, and bind the current snapshot.
   */
  release(projectId: string, branch: string, gateId: string): ReleaseResult {
    this.requireGate(projectId, gateId)
    const row = this.events.db
      .prepare('SELECT record FROM approvals WHERE project = ? AND branch = ? AND gate = ? ORDER BY seq DESC LIMIT 1')
      .get(projectId, branch, gateId) as { record: string } | undefined
    if (!row) return { gate: gateId, status: 'none', released: false, approval: null, reasons: [{ code: 'APPROVAL_MISSING', params: { gate: gateId } }] }
    const approval = JSON.parse(row.record) as ApprovalRecord
    const snapshot = this.snapshot(projectId, branch, gateId)
    let status: ApprovalStatus
    const reasons: Message[] = []
    if (snapshot.hash !== approval.snapshotHash) {
      status = 'invalidated'
      reasons.push({ code: 'APPROVAL_INVALIDATED', params: { gate: gateId, approval: approval.id } })
    } else if (approval.decision === 'rejected') {
      status = 'rejected'
      reasons.push({ code: 'APPROVAL_REJECTED', params: { gate: gateId, approval: approval.id } })
    } else if (approval.decision === 'conditional') {
      const open = approval.conditions.filter((c) => !c.closed).map((c) => c.id)
      status = open.length > 0 ? 'conditional_pending' : 'conditional_closed'
      if (open.length > 0) reasons.push({ code: 'CONDITIONS_OPEN', params: { gate: gateId, conditions: open } })
    } else status = 'approved'
    return { gate: gateId, status, released: reasons.length === 0, approval: approval.id, reasons }
  }

  createWaiver(params: WaiversCreateParams, actor: Actor): WaiverRecord {
    if (actor.kind !== 'human') throw new KernelError('WAIVER_REQUIRES_HUMAN', 'only a human can record a waiver')
    const check = this.requireCheck(params.check)
    if ((check.waiver ?? 'none') !== params.kind) {
      throw new KernelError('WAIVER_NOT_ALLOWED', `check ${check.id} admits ${check.waiver ?? 'none'}, not ${params.kind}`, { allowed: check.waiver ?? 'none' })
    }
    let acknowledgement: string | null = null
    if (params.kind === 'foundry_waiver') {
      // Only the foundry can waive a manufacturing rule; its written acknowledgement must be on record.
      if (!params.acknowledgement) throw new KernelError('FOUNDRY_ACK_REQUIRED', 'a foundry waiver needs the foundry\'s written acknowledgement')
      const ack = this.artifacts.require(params.acknowledgement)
      if (ack.type !== FOUNDRY_ACK_TYPE) throw new KernelError('FOUNDRY_ACK_REQUIRED', `acknowledgement must be an artifact of type ${FOUNDRY_ACK_TYPE}`)
      acknowledgement = ack.hash
    }
    for (const subject of params.subjects) this.artifacts.require(subject)
    const waiver: WaiverRecord = {
      id: ulid(),
      kind: params.kind,
      check: check.id,
      subjects: [...params.subjects].sort() as WaiverRecord['subjects'],
      reason: params.reason,
      ...(params.impact ? { impact: params.impact } : {}),
      acknowledgement,
      by: actor,
      at: new Date().toISOString(),
    }
    this.events.append({ kind: 'waiver.recorded', payload: { waiver }, actor })
    return waiver
  }

  // ---- helpers -----------------------------------------------------------

  private invalidateApprovals(projectId: string, branch: string, slot: string): void {
    const project = this.requireProject(projectId)
    const gates = (this.readFlow(project.flowHash).gates ?? []).filter((g) => g.members.includes(slot))
    for (const gate of gates) {
      const row = this.events.db
        .prepare('SELECT id, invalidated, record FROM approvals WHERE project = ? AND branch = ? AND gate = ? ORDER BY seq DESC LIMIT 1')
        .get(projectId, branch, gate.id) as { id: string; invalidated: number; record: string } | undefined
      if (!row || row.invalidated) continue
      const approval = JSON.parse(row.record) as ApprovalRecord
      if (this.snapshot(projectId, branch, gate.id).hash === approval.snapshotHash) continue
      this.events.append({
        kind: 'approval.invalidated',
        payload: { approval: approval.id, gate: gate.id, reason: { code: 'SNAPSHOT_MEMBER_CHANGED', params: { slot } } },
        actor: KERNEL_ACTOR,
        project: projectId,
      })
    }
  }

  private readFlow(hash: string): FlowPack {
    const pack = JSON.parse(readFileSync(this.artifacts.objectPath(hash), 'utf8'))
    assertValid<FlowPack>('FlowPack', pack, 'FLOW_INVALID')
    return pack
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

  private requireGate(projectId: string, gateId: string): FlowGate {
    const project = this.requireProject(projectId)
    const gate = this.readFlow(project.flowHash).gates?.find((g) => g.id === gateId)
    if (!gate) throw new KernelError('GATE_NOT_FOUND', `flow ${project.flow} has no gate ${gateId}`, { gate: gateId })
    return gate
  }

  private requireCheck(id: string): CheckDefinition {
    const check = this.checkDefs.get(id)
    if (!check) throw new KernelError('CHECK_NOT_FOUND', `check ${id} is not defined by any flow pack`, { check: id })
    return check
  }

  private verdict(id: string): VerdictRecord {
    const row = this.events.db.prepare('SELECT record FROM verdicts WHERE id = ?').get(id) as { record: string } | undefined
    if (!row) throw new KernelError('VERDICT_NOT_FOUND', `verdict ${id} does not exist`, { verdict: id })
    return JSON.parse(row.record)
  }

  private approval(id: string): ApprovalRecord {
    const row = this.events.db.prepare('SELECT record FROM approvals WHERE id = ?').get(id) as { record: string } | undefined
    if (!row) throw new KernelError('APPROVAL_NOT_FOUND', `approval ${id} does not exist`, { approval: id })
    return JSON.parse(row.record)
  }
}

/** true / false, or undefined when the metric is missing or not comparable. */
function evaluateCriterion(criterion: Criterion, metrics: RunRecord['metrics']): boolean | undefined {
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
