import type { Actor, ArtifactMeta, EgressDestination, EventRecord, KernelMethod } from '@llmchip/kernel-api'
import { KernelError } from '../util/errors.ts'
import type { EgressGateway } from './egress.ts'
import type { Incidents } from './incidents.ts'

/**
 * Methods an external client (MCP or API, requirements §6.1) may call: read
 * the workbench and start work, never decide, waive, change constraints or
 * policy, or handle secrets (FR-AGT-02).
 */
const EXTERNAL_METHODS: ReadonlySet<KernelMethod> = new Set<KernelMethod>([
  'kernel.info',
  'events.list',
  'events.subscribe',
  'events.unsubscribe',
  'artifacts.stat',
  'artifacts.list',
  'artifacts.lineage',
  'plugins.list',
  'runs.start',
  'runs.get',
  'runs.wait',
  'runs.list',
  'checks.list',
  'verdicts.list',
  'projects.list',
  'projects.get',
  'incidents.list',
  'tasks.create',
  'tasks.get',
  'tasks.list',
  'changesets.list',
  'constraints.list',
])

/** The external client as an egress destination: it may receive at most `internal` data. */
export const EXTERNAL_DESTINATION: EgressDestination = { id: 'external-client', kind: 'mcp', location: 'cloud', maxLabel: 'internal' }

/**
 * K5, API side: decides what each kind of client may do through the kernel
 * API and filters what it may see. Agents managed by the kernel do not use
 * the API; their tool calls are checked by the agent runtime (§6.6).
 */
export class Policy {
  private readonly incidents: Incidents
  private readonly gateway: EgressGateway

  constructor(incidents: Incidents, gateway: EgressGateway) {
    this.incidents = incidents
    this.gateway = gateway
  }

  authorize(actor: Actor, method: KernelMethod): void {
    if (actor.kind === 'human') return
    if (actor.kind === 'external' && EXTERNAL_METHODS.has(method)) return
    this.deny(actor, `call ${method}`)
  }

  /** Throws PERMISSION_DENIED after recording an incident. */
  deny(actor: Actor, action: string, project?: string | null): never {
    const incident = this.incidents.open({
      kind: 'permission_denied',
      severity: 'warning',
      summary: { code: 'PERMISSION_DENIED', params: { actor: `${actor.kind}:${actor.id}`, action } },
      actor,
      project: project ?? null,
    })
    throw new KernelError('PERMISSION_DENIED', `${actor.kind}:${actor.id} may not ${action}`, { action, incident: incident.id })
  }

  /** Hides NDA event payloads from external clients. */
  filterEvent(actor: Actor, event: EventRecord): EventRecord {
    if (actor.kind !== 'external' || event.label !== 'nda') return event
    return { ...event, payload: { redacted: 'nda' } }
  }

  /** Lets an external client download content only through the egress gateway. */
  checkDownload(actor: Actor, meta: ArtifactMeta, content: () => string): void {
    if (actor.kind !== 'external') return
    this.gateway.check(EXTERNAL_DESTINATION, [{ label: meta.label, source: meta.hash, text: meta.size <= 16 * 1024 * 1024 ? content() : '' }], actor)
  }
}
