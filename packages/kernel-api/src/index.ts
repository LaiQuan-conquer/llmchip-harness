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
export const KERNEL_API_VERSION = '0.1.0'

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
  'artifact.created': T.ArtifactCreatedPayload
  'artifact.deletion_marked': T.ArtifactDeletionMarkedPayload
  'secret.changed': T.SecretChangedPayload
  'projection.rebuilt': T.ProjectionRebuiltPayload
  'plugin.loaded': T.PluginLoadedPayload
  'plugin.failed': T.PluginFailedPayload
  'plugin.unloaded': T.PluginUnloadedPayload
}

/** Schema definition name of each kernel event kind's payload. */
export const KERNEL_EVENT_PAYLOAD_DEFS: { readonly [K in keyof KernelEventPayloads]: string } = {
  'kernel.started': 'KernelStartedPayload',
  'kernel.stopped': 'KernelStoppedPayload',
  'artifact.created': 'ArtifactCreatedPayload',
  'artifact.deletion_marked': 'ArtifactDeletionMarkedPayload',
  'secret.changed': 'SecretChangedPayload',
  'projection.rebuilt': 'ProjectionRebuiltPayload',
  'plugin.loaded': 'PluginLoadedPayload',
  'plugin.failed': 'PluginFailedPayload',
  'plugin.unloaded': 'PluginUnloadedPayload',
}

/** JSON-RPC methods of the kernel API (IF-1): params and result types. */
export interface KernelMethods {
  'kernel.info': { params: T.EmptyParams; result: T.KernelInfo }
  'kernel.shutdown': { params: T.EmptyParams; result: T.OkResult }
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
}

export type KernelMethod = keyof KernelMethods

/** Schema definition names of each method's params and result, used for runtime validation. */
export const KERNEL_METHOD_DEFS: { readonly [M in KernelMethod]: { params: string; result: string } } = {
  'kernel.info': { params: 'EmptyParams', result: 'KernelInfo' },
  'kernel.shutdown': { params: 'EmptyParams', result: 'OkResult' },
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
}

/** Server-to-client notification carrying a subscribed event. */
export const EVENT_NOTIFICATION = 'events.event'

/** HTTP path of artifact content: `PUT` uploads, `GET <path>/<hash>` downloads. */
export const ARTIFACT_HTTP_PATH = '/v1/artifacts'

/** Path of the JSON-RPC WebSocket endpoint. */
export const RPC_WS_PATH = '/v1/rpc'

/** Query parameter carrying the client token (browsers cannot set WebSocket headers). */
export const TOKEN_QUERY_PARAM = 'token'
