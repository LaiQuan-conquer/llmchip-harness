// Language-neutral contracts of the kernel: the kernel API (IF-1), the plugin
// protocol (IF-2) and the artifact type schemas. Must not depend on Cordis
// (Microkernel Architecture AP-6, ADR-001).
//
// The record and message shapes are defined as JSON Schema in
// ../schemas/kernel.schema.json; the types and the schema module under
// ./generated are produced from it by `yarn gen:schemas`.

import type * as T from './generated/kernel.types.ts'

export type * from './generated/kernel.types.ts'
export { kernelSchema } from './generated/kernel.schema.ts'

/** Version of the kernel API contract. Follows semver. */
export const KERNEL_API_VERSION = '0.3.0'

/** `$id` of the kernel schema; definitions are addressed as `${KERNEL_SCHEMA_ID}#/$defs/<Name>`. */
export const KERNEL_SCHEMA_ID = 'urn:llmchip:kernel'

/** Labels in increasing order of sensitivity (requirements §8). */
export const LABELS: readonly T.Label[] = ['public', 'internal', 'nda']

/** The most sensitive of the given labels; `public` for none. */
export function maxLabel(labels: Iterable<T.Label>): T.Label {
  let max = 0
  for (const label of labels) max = Math.max(max, LABELS.indexOf(label))
  return LABELS[max]!
}

/** Payload type of each kernel event kind. Plugin kinds are registered at runtime. */
export interface KernelEventPayloads {
  'kernel.started': T.KernelStartedPayload
  'kernel.stopped': T.KernelStoppedPayload
  'kernel.paused': T.KernelPausedPayload
  'artifact.created': T.ArtifactCreatedPayload
  'artifact.deletion_marked': T.ArtifactDeletionMarkedPayload
  'secret.changed': T.SecretChangedPayload
  'projection.rebuilt': T.ProjectionRebuiltPayload
  'plugin.loaded': T.PluginLoadedPayload
  'plugin.failed': T.PluginFailedPayload
  'plugin.unloaded': T.PluginUnloadedPayload
  'run.queued': T.RunQueuedPayload
  'run.started': T.RunStartedPayload
  'run.finished': T.RunFinishedPayload
  'verdict.recorded': T.VerdictRecordedPayload
  'project.created': T.ProjectCreatedPayload
  'project.branched': T.ProjectBranchedPayload
  'project.slot_bound': T.SlotBoundPayload
  'stage.entered': T.StageEnteredPayload
  'approval.recorded': T.ApprovalRecordedPayload
  'approval.condition_closed': T.ConditionClosedPayload
  'approval.invalidated': T.ApprovalInvalidatedPayload
  'waiver.recorded': T.WaiverRecordedPayload
  'incident.opened': T.IncidentOpenedPayload
  'incident.closed': T.IncidentClosedPayload
  'egress.decided': T.EgressDecidedPayload
  'declass.rule_set': T.DeclassRuleSetPayload
  'constraint.proposed': T.ConstraintProposedPayload
  'constraint.decided': T.ConstraintDecidedPayload
  'task.created': T.TaskCreatedPayload
  'task.state': T.TaskStatePayload
  'agent.step': T.AgentStepPayload
  'changeset.updated': T.ChangesetEventPayload
}

/** Schema definition name of each kernel event kind's payload. */
export const KERNEL_EVENT_PAYLOAD_DEFS: { readonly [K in keyof KernelEventPayloads]: string } = {
  'kernel.started': 'KernelStartedPayload',
  'kernel.stopped': 'KernelStoppedPayload',
  'kernel.paused': 'KernelPausedPayload',
  'artifact.created': 'ArtifactCreatedPayload',
  'artifact.deletion_marked': 'ArtifactDeletionMarkedPayload',
  'secret.changed': 'SecretChangedPayload',
  'projection.rebuilt': 'ProjectionRebuiltPayload',
  'plugin.loaded': 'PluginLoadedPayload',
  'plugin.failed': 'PluginFailedPayload',
  'plugin.unloaded': 'PluginUnloadedPayload',
  'run.queued': 'RunQueuedPayload',
  'run.started': 'RunStartedPayload',
  'run.finished': 'RunFinishedPayload',
  'verdict.recorded': 'VerdictRecordedPayload',
  'project.created': 'ProjectCreatedPayload',
  'project.branched': 'ProjectBranchedPayload',
  'project.slot_bound': 'SlotBoundPayload',
  'stage.entered': 'StageEnteredPayload',
  'approval.recorded': 'ApprovalRecordedPayload',
  'approval.condition_closed': 'ConditionClosedPayload',
  'approval.invalidated': 'ApprovalInvalidatedPayload',
  'waiver.recorded': 'WaiverRecordedPayload',
  'incident.opened': 'IncidentOpenedPayload',
  'incident.closed': 'IncidentClosedPayload',
  'egress.decided': 'EgressDecidedPayload',
  'declass.rule_set': 'DeclassRuleSetPayload',
  'constraint.proposed': 'ConstraintProposedPayload',
  'constraint.decided': 'ConstraintDecidedPayload',
  'task.created': 'TaskCreatedPayload',
  'task.state': 'TaskStatePayload',
  'agent.step': 'AgentStepPayload',
  'changeset.updated': 'ChangesetEventPayload',
}

/** JSON-RPC methods of the kernel API (IF-1): params and result types. */
export interface KernelMethods {
  'kernel.info': { params: T.EmptyParams; result: T.KernelInfo }
  'kernel.shutdown': { params: T.EmptyParams; result: T.OkResult }
  'kernel.pause': { params: T.PauseParams; result: T.PauseResult }
  'kernel.resume': { params: T.EmptyParams; result: T.PauseResult }
  'events.list': { params: T.EventsListParams; result: T.EventsListResult }
  'events.subscribe': { params: T.EventsSubscribeParams; result: T.EventsSubscribeResult }
  'events.unsubscribe': { params: T.EventsUnsubscribeParams; result: T.OkResult }
  'events.verify': { params: T.EmptyParams; result: T.IntegrityReport }
  'artifacts.stat': { params: T.ArtifactRefParams; result: T.ArtifactMeta }
  'artifacts.list': { params: T.ArtifactsListParams; result: T.ArtifactsListResult }
  'artifacts.lineage': { params: T.ArtifactsLineageParams; result: T.LineageResult }
  'artifacts.markDeleted': { params: T.ArtifactsMarkDeletedParams; result: T.ArtifactMeta }
  'artifacts.verify': { params: T.EmptyParams; result: T.IntegrityReport }
  'projections.rebuild': { params: T.EmptyParams; result: T.ProjectionsRebuildResult }
  'secrets.set': { params: T.SecretsSetParams; result: T.OkResult }
  'secrets.remove': { params: T.SecretsRemoveParams; result: T.OkResult }
  'secrets.list': { params: T.EmptyParams; result: T.SecretsListResult }
  'plugins.list': { params: T.EmptyParams; result: T.PluginsListResult }
  'runs.start': { params: T.RunsStartParams; result: T.RunRecord }
  'runs.get': { params: T.RunRefParams; result: T.RunRecord }
  'runs.wait': { params: T.RunsWaitParams; result: T.RunRecord }
  'runs.list': { params: T.RunsListParams; result: T.RunsListResult }
  'runs.cancel': { params: T.RunRefParams; result: T.RunRecord }
  'checks.list': { params: T.EmptyParams; result: T.ChecksListResult }
  'verdicts.evaluate': { params: T.VerdictsEvaluateParams; result: T.VerdictRecord }
  'verdicts.list': { params: T.VerdictsListParams; result: T.VerdictsListResult }
  'projects.create': { params: T.ProjectsCreateParams; result: T.ProjectRecord }
  'projects.list': { params: T.EmptyParams; result: T.ProjectsListResult }
  'projects.get': { params: T.ProjectRefParams; result: T.ProjectState }
  'projects.bind': { params: T.ProjectsBindParams; result: T.OkResult }
  'projects.branch': { params: T.ProjectsBranchParams; result: T.OkResult }
  'stages.enter': { params: T.StagesEnterParams; result: T.OkResult }
  'gates.snapshot': { params: T.GateRefParams; result: T.Snapshot }
  'gates.release': { params: T.GateRefParams; result: T.ReleaseResult }
  'approvals.decide': { params: T.ApprovalsDecideParams; result: T.ApprovalRecord }
  'approvals.closeCondition': { params: T.ApprovalsCloseConditionParams; result: T.ApprovalRecord }
  'approvals.list': { params: T.ProjectRefParams; result: T.ApprovalsListResult }
  'waivers.create': { params: T.WaiversCreateParams; result: T.WaiverRecord }
  'incidents.list': { params: T.IncidentsListParams; result: T.IncidentsListResult }
  'incidents.close': { params: T.IncidentsCloseParams; result: T.IncidentRecord }
  'declass.setRule': { params: T.DeclassRule; result: T.OkResult }
  'declass.listRules': { params: T.EmptyParams; result: T.DeclassRulesListResult }
  'declass.apply': { params: T.DeclassifyParams; result: T.ArtifactMeta }
  'checks.override': { params: T.ChecksOverrideParams; result: T.ConstraintProposal }
  'constraints.decide': { params: T.ConstraintsDecideParams; result: T.ConstraintProposal }
  'constraints.list': { params: T.ConstraintsListParams; result: T.ConstraintsListResult }
  'tasks.create': { params: T.TaskSpec; result: T.TaskRecord }
  'tasks.get': { params: T.TaskRefParams; result: T.TaskRecord }
  'tasks.list': { params: T.EmptyParams; result: T.TasksListResult }
  'tasks.run': { params: T.TasksRunParams; result: T.TaskRecord }
  'tasks.pause': { params: T.TaskRefParams; result: T.TaskRecord }
  'tasks.switch': { params: T.TasksSwitchParams; result: T.TasksSwitchResult }
  'tasks.handoff': { params: T.TaskRefParams; result: T.HandoffPack }
  'changesets.list': { params: T.ChangesetsListParams; result: T.ChangesetsListResult }
}

export type KernelMethod = keyof KernelMethods

/** Schema definition names of each method's params and result, used for runtime validation. */
export const KERNEL_METHOD_DEFS: { readonly [M in KernelMethod]: { params: string; result: string } } = {
  'kernel.info': { params: 'EmptyParams', result: 'KernelInfo' },
  'kernel.shutdown': { params: 'EmptyParams', result: 'OkResult' },
  'kernel.pause': { params: 'PauseParams', result: 'PauseResult' },
  'kernel.resume': { params: 'EmptyParams', result: 'PauseResult' },
  'events.list': { params: 'EventsListParams', result: 'EventsListResult' },
  'events.subscribe': { params: 'EventsSubscribeParams', result: 'EventsSubscribeResult' },
  'events.unsubscribe': { params: 'EventsUnsubscribeParams', result: 'OkResult' },
  'events.verify': { params: 'EmptyParams', result: 'IntegrityReport' },
  'artifacts.stat': { params: 'ArtifactRefParams', result: 'ArtifactMeta' },
  'artifacts.list': { params: 'ArtifactsListParams', result: 'ArtifactsListResult' },
  'artifacts.lineage': { params: 'ArtifactsLineageParams', result: 'LineageResult' },
  'artifacts.markDeleted': { params: 'ArtifactsMarkDeletedParams', result: 'ArtifactMeta' },
  'artifacts.verify': { params: 'EmptyParams', result: 'IntegrityReport' },
  'projections.rebuild': { params: 'EmptyParams', result: 'ProjectionsRebuildResult' },
  'secrets.set': { params: 'SecretsSetParams', result: 'OkResult' },
  'secrets.remove': { params: 'SecretsRemoveParams', result: 'OkResult' },
  'secrets.list': { params: 'EmptyParams', result: 'SecretsListResult' },
  'plugins.list': { params: 'EmptyParams', result: 'PluginsListResult' },
  'runs.start': { params: 'RunsStartParams', result: 'RunRecord' },
  'runs.get': { params: 'RunRefParams', result: 'RunRecord' },
  'runs.wait': { params: 'RunsWaitParams', result: 'RunRecord' },
  'runs.list': { params: 'RunsListParams', result: 'RunsListResult' },
  'runs.cancel': { params: 'RunRefParams', result: 'RunRecord' },
  'checks.list': { params: 'EmptyParams', result: 'ChecksListResult' },
  'verdicts.evaluate': { params: 'VerdictsEvaluateParams', result: 'VerdictRecord' },
  'verdicts.list': { params: 'VerdictsListParams', result: 'VerdictsListResult' },
  'projects.create': { params: 'ProjectsCreateParams', result: 'ProjectRecord' },
  'projects.list': { params: 'EmptyParams', result: 'ProjectsListResult' },
  'projects.get': { params: 'ProjectRefParams', result: 'ProjectState' },
  'projects.bind': { params: 'ProjectsBindParams', result: 'OkResult' },
  'projects.branch': { params: 'ProjectsBranchParams', result: 'OkResult' },
  'stages.enter': { params: 'StagesEnterParams', result: 'OkResult' },
  'gates.snapshot': { params: 'GateRefParams', result: 'Snapshot' },
  'gates.release': { params: 'GateRefParams', result: 'ReleaseResult' },
  'approvals.decide': { params: 'ApprovalsDecideParams', result: 'ApprovalRecord' },
  'approvals.closeCondition': { params: 'ApprovalsCloseConditionParams', result: 'ApprovalRecord' },
  'approvals.list': { params: 'ProjectRefParams', result: 'ApprovalsListResult' },
  'waivers.create': { params: 'WaiversCreateParams', result: 'WaiverRecord' },
  'incidents.list': { params: 'IncidentsListParams', result: 'IncidentsListResult' },
  'incidents.close': { params: 'IncidentsCloseParams', result: 'IncidentRecord' },
  'declass.setRule': { params: 'DeclassRule', result: 'OkResult' },
  'declass.listRules': { params: 'EmptyParams', result: 'DeclassRulesListResult' },
  'declass.apply': { params: 'DeclassifyParams', result: 'ArtifactMeta' },
  'checks.override': { params: 'ChecksOverrideParams', result: 'ConstraintProposal' },
  'constraints.decide': { params: 'ConstraintsDecideParams', result: 'ConstraintProposal' },
  'constraints.list': { params: 'ConstraintsListParams', result: 'ConstraintsListResult' },
  'tasks.create': { params: 'TaskSpec', result: 'TaskRecord' },
  'tasks.get': { params: 'TaskRefParams', result: 'TaskRecord' },
  'tasks.list': { params: 'EmptyParams', result: 'TasksListResult' },
  'tasks.run': { params: 'TasksRunParams', result: 'TaskRecord' },
  'tasks.pause': { params: 'TaskRefParams', result: 'TaskRecord' },
  'tasks.switch': { params: 'TasksSwitchParams', result: 'TasksSwitchResult' },
  'tasks.handoff': { params: 'TaskRefParams', result: 'HandoffPack' },
  'changesets.list': { params: 'ChangesetsListParams', result: 'ChangesetsListResult' },
}

/** Version of the plugin protocol (IF-2). */
export const PLUGIN_PROTOCOL_VERSION = '0.1.0'

/** File name of a plugin manifest (architecture §4.9). */
export const PLUGIN_MANIFEST = 'llmchip-plugin.json'

/** Server-to-client notification carrying a subscribed event. */
export const EVENT_NOTIFICATION = 'events.event'

/** HTTP path of artifact content: `PUT` uploads, `GET <path>/<hash>` downloads. */
export const ARTIFACT_HTTP_PATH = '/v1/artifacts'

/** Path of the JSON-RPC WebSocket endpoint. */
export const RPC_WS_PATH = '/v1/rpc'

/** Query parameter carrying the client token (browsers cannot set WebSocket headers). */
export const TOKEN_QUERY_PARAM = 'token'
