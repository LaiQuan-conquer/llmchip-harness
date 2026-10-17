// Client of the kernel API (IF-1) for every front end: CLI, Web UI, desktop
// shell, tests. Uses only the standard WebSocket and fetch APIs, so it runs in
// Node.js 24 and in browsers.

import {
  ARTIFACT_HTTP_PATH,
  EVENT_NOTIFICATION,
  RPC_WS_PATH,
  TOKEN_QUERY_PARAM,
  type ArtifactMeta,
  type ArtifactUploadQuery,
  type ErrorData,
  type EventNotificationParams,
  type EventRecord,
  type KernelMethod,
  type KernelMethods,
} from '@llmchip/kernel-api'

/** A failed kernel call. `code` is the kernel's stable message code. */
export class KernelCallError extends Error {
  readonly code: string
  readonly params: Record<string, unknown>
  readonly rpcCode: number | undefined

  constructor(message: string, data: Partial<ErrorData> | undefined, rpcCode?: number) {
    super(message)
    this.name = 'KernelCallError'
    this.code = data?.code ?? 'UNKNOWN_ERROR'
    this.params = (data?.params as Record<string, unknown> | undefined) ?? {}
    this.rpcCode = rpcCode
  }
}

/** Artifact content accepted by `upload`. Streams are sent without buffering. */
export type UploadBody = string | Uint8Array | Blob | ReadableStream<Uint8Array>

export interface ConnectOptions {
  /** Base URL of the kernel, e.g. `http://127.0.0.1:45357`. */
  url: string
  token: string
}

interface Pending {
  resolve(value: unknown): void
  reject(error: Error): void
}

export class KernelClient {
  private readonly url: string
  private readonly token: string
  private readonly ws: WebSocket
  private readonly pending = new Map<number, Pending>()
  private readonly subscriptions = new Map<string, (event: EventRecord) => void>()
  /** Receives notifications for subscriptions whose `events.subscribe` response is still in flight. */
  private readonly earlyListeners = new Set<(params: EventNotificationParams) => void>()
  private nextId = 1
  /** Resolves when the connection closes. */
  readonly closed: Promise<void>

  private constructor(options: ConnectOptions, ws: WebSocket) {
    this.url = options.url.replace(/\/$/, '')
    this.token = options.token
    this.ws = ws
    this.closed = new Promise((resolve) => {
      ws.addEventListener('close', () => {
        for (const pending of this.pending.values()) pending.reject(new KernelCallError('connection closed', { code: 'CONNECTION_CLOSED' }))
        this.pending.clear()
        resolve()
      })
    })
    ws.addEventListener('message', (message) => this.onMessage(String(message.data)))
  }

  static connect(options: ConnectOptions): Promise<KernelClient> {
    const wsUrl = new URL(RPC_WS_PATH, options.url.replace(/^http/, 'ws'))
    wsUrl.searchParams.set(TOKEN_QUERY_PARAM, options.token)
    const ws = new WebSocket(wsUrl)
    return new Promise((resolve, reject) => {
      ws.addEventListener('open', () => resolve(new KernelClient(options, ws)), { once: true })
      ws.addEventListener('error', () => reject(new KernelCallError(`cannot connect to ${options.url}`, { code: 'CONNECTION_FAILED' })), {
        once: true,
      })
    })
  }

  call<M extends KernelMethod>(method: M, ...args: {} extends KernelMethods[M]['params'] ? [params?: KernelMethods[M]['params']] : [params: KernelMethods[M]['params']]): Promise<KernelMethods[M]['result']> {
    const id = this.nextId++
    const message = JSON.stringify({ jsonrpc: '2.0', id, method, params: args[0] ?? {} })
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (value: unknown) => void, reject })
      this.ws.send(message)
    })
  }

  /**
   * Streams events with seq greater than `since` (default: from now on).
   * Returns a function that ends the subscription.
   */
  async subscribe(onEvent: (event: EventRecord) => void, since?: number): Promise<() => Promise<void>> {
    // Register before the response arrives: replayed events may follow it immediately.
    const buffered: EventNotificationParams[] = []
    const early = (params: EventNotificationParams) => buffered.push(params)
    this.earlyListeners.add(early)
    let subscription: string
    try {
      ;({ subscription } = await this.call('events.subscribe', since === undefined ? {} : { since }))
    } finally {
      this.earlyListeners.delete(early)
    }
    this.subscriptions.set(subscription, onEvent)
    for (const params of buffered) if (params.subscription === subscription) onEvent(params.event)
    return async () => {
      this.subscriptions.delete(subscription)
      if (this.ws.readyState === WebSocket.OPEN) await this.call('events.unsubscribe', { subscription })
    }
  }

  /** Uploads artifact content. */
  async upload(body: UploadBody, query: ArtifactUploadQuery): Promise<ArtifactMeta> {
    const url = new URL(ARTIFACT_HTTP_PATH, this.url)
    url.searchParams.set('type', query.type)
    if (query.mediaType) url.searchParams.set('mediaType', query.mediaType)
    if (query.label) url.searchParams.set('label', query.label)
    for (const input of query.inputs ?? []) url.searchParams.append('input', input)
    const init: RequestInit & { duplex?: 'half' } = { method: 'PUT', body, headers: this.headers() }
    if (body instanceof ReadableStream) init.duplex = 'half'
    return (await (await this.http(url, init)).json()) as ArtifactMeta
  }

  /** Downloads artifact content. */
  async download(hash: string): Promise<Response> {
    return this.http(new URL(`${ARTIFACT_HTTP_PATH}/${hash}`, this.url), { headers: this.headers() })
  }

  close(): Promise<void> {
    this.ws.close()
    return this.closed
  }

  private headers(): Record<string, string> {
    return { authorization: `Bearer ${this.token}` }
  }

  private async http(url: URL, init: RequestInit): Promise<Response> {
    const response = await fetch(url, init)
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { error?: ErrorData & { message?: string } }
      throw new KernelCallError(body.error?.message ?? `HTTP ${response.status}`, body.error)
    }
    return response
  }

  private onMessage(text: string): void {
    const message = JSON.parse(text)
    if (message.method === EVENT_NOTIFICATION) {
      const params = message.params as EventNotificationParams
      const listener = this.subscriptions.get(params.subscription)
      if (listener) listener(params.event)
      else for (const early of this.earlyListeners) early(params)
      return
    }
    const pending = this.pending.get(message.id)
    if (!pending) return
    this.pending.delete(message.id)
    if (message.error) pending.reject(new KernelCallError(message.error.message, message.error.data, message.error.code))
    else pending.resolve(message.result)
  }
}
