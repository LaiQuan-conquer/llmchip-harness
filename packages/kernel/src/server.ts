import { timingSafeEqual } from 'node:crypto'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { extname, join, normalize } from 'node:path'
import { pipeline } from 'node:stream/promises'
import { WebSocketServer, type WebSocket } from 'ws'
import {
  ARTIFACT_HTTP_PATH,
  EVENT_NOTIFICATION,
  KERNEL_METHOD_DEFS,
  RPC_WS_PATH,
  TOKEN_QUERY_PARAM,
  type Actor,
  type ArtifactMeta,
  type ArtifactUploadQuery,
  type EventRecord,
  type KernelMethod,
  type KernelMethods,
} from '@llmchip/kernel-api'
import type { ArtifactStore } from './artifacts/store.ts'
import { KernelError } from './util/errors.ts'
import type { Logger } from './util/logger.ts'
import { assertValid } from './util/schema.ts'
import { ulid } from './util/ulid.ts'
import type { EventStore } from './events/store.ts'

/** A connected client. */
export interface Session {
  readonly id: string
  readonly actor: Actor
}

export type MethodHandler<M extends KernelMethod> = (
  params: KernelMethods[M]['params'],
  session: Session,
) => KernelMethods[M]['result'] | Promise<KernelMethods[M]['result']>

/** Session-bound methods (subscriptions) are implemented by the server itself. */
type ServerMethod = 'events.subscribe' | 'events.unsubscribe'

export type MethodHandlers = { [M in Exclude<KernelMethod, ServerMethod>]: MethodHandler<M> }

/** Clients holding the owner token act as the workbench owner. */
const LOCAL_OWNER: Actor = { kind: 'human', id: 'local-owner' }

/** Hooks of the policy engine (K5) and egress gateway (K8) into the API. */
export interface ServerPolicy {
  authorize(actor: Actor, method: KernelMethod): void
  filterEvent(actor: Actor, event: EventRecord): EventRecord
  checkDownload(actor: Actor, meta: ArtifactMeta, content: () => string): void
  /** Called when an HTTP request is refused for lack of permission. */
  deny(actor: Actor, action: string): never
}

// JSON-RPC 2.0 error codes.
const PARSE_ERROR = -32700
const INVALID_REQUEST = -32600
const METHOD_NOT_FOUND = -32601
const INVALID_PARAMS = -32602
const INTERNAL_ERROR = -32603
const KERNEL_ERROR = -32000

interface RpcRequest {
  jsonrpc: '2.0'
  id?: string | number | null
  method: string
  params?: unknown
}

/**
 * IF-1, the kernel API (architecture §9.1): JSON-RPC 2.0 over WebSocket at
 * `/v1/rpc`, plus HTTP for artifact content at `/v1/artifacts`. Every request
 * must carry the token from the runtime file, as a bearer header or a query
 * parameter. Params and results are validated against the kernel schema.
 */
export class KernelServer {
  private readonly http: Server
  private readonly wss: WebSocketServer
  private readonly token: Buffer
  private readonly externalToken: Buffer
  private readonly policy: ServerPolicy
  private readonly staticDir: string | undefined
  private readonly handlers: MethodHandlers
  private readonly events: EventStore
  private readonly artifacts: ArtifactStore
  private readonly logger: Logger
  private readonly sockets = new Set<WebSocket>()

  constructor(options: {
    token: string
    /** Token of external clients (MCP bridges); see Policy. */
    externalToken: string
    policy: ServerPolicy
    /** Built Web UI (apps/web/dist), served without authentication: it contains no data. */
    staticDir?: string
    handlers: MethodHandlers
    events: EventStore
    artifacts: ArtifactStore
    logger: Logger
  }) {
    this.token = Buffer.from(options.token)
    this.externalToken = Buffer.from(options.externalToken)
    this.policy = options.policy
    this.staticDir = options.staticDir
    this.handlers = options.handlers
    this.events = options.events
    this.artifacts = options.artifacts
    this.logger = options.logger
    this.http = createServer((req, res) => void this.handleHttp(req, res))
    this.wss = new WebSocketServer({ noServer: true, maxPayload: 16 * 1024 * 1024 })
    this.http.on('upgrade', (req, socket, head) => {
      const url = new URL(req.url ?? '/', 'http://localhost')
      const actor = url.pathname === RPC_WS_PATH ? this.authenticate(req, url) : undefined
      if (!actor) {
        socket.end('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n')
        return
      }
      this.wss.handleUpgrade(req, socket, head, (ws) => this.handleSocket(ws, actor))
    })
  }

  async listen(host: string, port: number): Promise<string> {
    await new Promise<void>((resolve, reject) => {
      this.http.once('error', reject)
      this.http.listen(port, host, () => resolve())
    })
    const address = this.http.address() as AddressInfo
    return `http://${host.includes(':') ? `[${host}]` : host}:${address.port}`
  }

  /** Stops accepting requests and closes every connection. */
  async close(): Promise<void> {
    for (const ws of this.sockets) ws.close(1001, 'kernel shutting down')
    this.wss.close()
    this.http.closeAllConnections()
    await new Promise<void>((resolve) => this.http.close(() => resolve()))
  }

  /** The actor a request authenticates as, or undefined. */
  private authenticate(req: IncomingMessage, url: URL): Actor | undefined {
    const header = req.headers.authorization
    const given = header?.startsWith('Bearer ') ? header.slice(7) : url.searchParams.get(TOKEN_QUERY_PARAM)
    if (!given) return undefined
    const buffer = Buffer.from(given)
    const matches = (token: Buffer) => buffer.length === token.length && timingSafeEqual(buffer, token)
    if (matches(this.token)) return LOCAL_OWNER
    if (matches(this.externalToken)) {
      const client = (url.searchParams.get('client') ?? 'external-client').replace(/[^A-Za-z0-9_.-]/g, '').slice(0, 64) || 'external-client'
      return { kind: 'external', id: client }
    }
    return undefined
  }

  private handleSocket(ws: WebSocket, actor: Actor): void {
    const session: Session & { subscriptions: Map<string, () => void> } = {
      id: ulid(),
      actor,
      subscriptions: new Map(),
    }
    this.sockets.add(ws)
    ws.on('close', () => {
      for (const unsubscribe of session.subscriptions.values()) unsubscribe()
      this.sockets.delete(ws)
    })
    ws.on('message', (data, isBinary) => {
      if (isBinary) return send(ws, errorResponse(null, INVALID_REQUEST, 'binary frames are not supported'))
      void this.handleMessage(ws, session, data.toString())
    })
  }

  private async handleMessage(
    ws: WebSocket,
    session: Session & { subscriptions: Map<string, () => void> },
    text: string,
  ): Promise<void> {
    let request: RpcRequest
    try {
      request = JSON.parse(text)
    } catch {
      return send(ws, errorResponse(null, PARSE_ERROR, 'invalid JSON'))
    }
    if (!request || typeof request !== 'object' || Array.isArray(request) || request.jsonrpc !== '2.0' || typeof request.method !== 'string') {
      return send(ws, errorResponse(null, INVALID_REQUEST, 'not a JSON-RPC 2.0 request (batches are not supported)'))
    }
    const id = request.id ?? null
    const reply = (message: object) => {
      if (request.id !== undefined) send(ws, message)
    }
    const method = request.method as KernelMethod
    const defs = KERNEL_METHOD_DEFS[method]
    if (!Object.hasOwn(KERNEL_METHOD_DEFS, method) || !defs) {
      return reply(errorResponse(id, METHOD_NOT_FOUND, `unknown method ${request.method}`))
    }
    const params = request.params ?? {}
    try {
      assertValid(defs.params, params, 'INVALID_PARAMS')
    } catch (error) {
      const e = error as KernelError
      return reply(errorResponse(id, INVALID_PARAMS, e.message, e.toData()))
    }
    try {
      this.policy.authorize(session.actor, method)
      let result: unknown
      if (method === 'events.subscribe') result = this.subscribe(ws, session, params as KernelMethods['events.subscribe']['params'])
      else if (method === 'events.unsubscribe') {
        const { subscription } = params as KernelMethods['events.unsubscribe']['params']
        const unsubscribe = session.subscriptions.get(subscription)
        if (!unsubscribe) throw new KernelError('SUBSCRIPTION_NOT_FOUND', `no subscription ${subscription}`, { subscription })
        unsubscribe()
        session.subscriptions.delete(subscription)
        result = { ok: true }
      } else {
        const handler = this.handlers[method as Exclude<KernelMethod, ServerMethod>] as MethodHandler<KernelMethod>
        result = await handler(params as never, session)
      }
      if (method === 'events.list') {
        const list = result as KernelMethods['events.list']['result']
        result = { ...list, events: list.events.map((event) => this.policy.filterEvent(session.actor, event)) }
      }
      assertValid(defs.result, result, 'RESULT_INVALID')
      reply({ jsonrpc: '2.0', id, result })
    } catch (error) {
      if (error instanceof KernelError && error.code !== 'RESULT_INVALID') {
        return reply(errorResponse(id, KERNEL_ERROR, error.message, error.toData()))
      }
      this.logger.error('request failed', { method, error: error instanceof Error ? error.stack : String(error) })
      reply(errorResponse(id, INTERNAL_ERROR, 'internal error', { code: 'INTERNAL_ERROR' }))
    }
  }

  /**
   * Replays events after `since`, then streams new ones, without gaps or
   * duplicates: live events are buffered until the replay has caught up.
   */
  private subscribe(
    ws: WebSocket,
    session: Session & { subscriptions: Map<string, () => void> },
    params: KernelMethods['events.subscribe']['params'],
  ): KernelMethods['events.subscribe']['result'] {
    const subscription = ulid()
    let last = params.since ?? this.events.head()
    let replaying = true
    const buffered: EventRecord[] = []
    const deliver = (event: EventRecord) => {
      if (event.seq <= last) return
      last = event.seq
      send(ws, { jsonrpc: '2.0', method: EVENT_NOTIFICATION, params: { subscription, event: this.policy.filterEvent(session.actor, event) } })
    }
    const unsubscribe = this.events.subscribe((event) => (replaying ? buffered.push(event) : deliver(event)))
    session.subscriptions.set(subscription, unsubscribe)
    // Deliver after the subscribe response has been sent.
    setImmediate(() => {
      for (;;) {
        const page = this.events.list({ since: last, limit: 500 })
        if (page.length === 0) break
        page.forEach(deliver)
      }
      replaying = false
      buffered.splice(0).forEach(deliver)
    })
    return { subscription }
  }

  /** Serves the Web UI; unknown paths get index.html (client-side routing). */
  private serveStatic(pathname: string, res: ServerResponse): void {
    const root = this.staticDir!
    let file = normalize(join(root, decodeURIComponent(pathname)))
    if (!file.startsWith(root) || !existsSync(file) || statSync(file).isDirectory()) file = join(root, 'index.html')
    if (!existsSync(file)) {
      res.writeHead(404, { 'content-type': 'text/plain' }).end('the Web UI is not built; run `yarn build`')
      return
    }
    res.writeHead(200, {
      'content-type': CONTENT_TYPES[extname(file)] ?? 'application/octet-stream',
      'cache-control': file.endsWith('index.html') ? 'no-store' : 'public, max-age=31536000, immutable',
      'x-content-type-options': 'nosniff',
      'content-security-policy': "default-src 'self'; connect-src 'self' ws: wss:; style-src 'self' 'unsafe-inline'",
    })
    res.end(readFileSync(file))
  }

  private async handleHttp(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const url = new URL(req.url ?? '/', 'http://localhost')
    if (this.staticDir && req.method === 'GET' && !url.pathname.startsWith('/v1/')) return this.serveStatic(url.pathname, res)
    try {
      const actor = this.authenticate(req, url)
      if (!actor) throw httpError(401, 'UNAUTHORIZED', 'missing or invalid token')
      if (url.pathname === ARTIFACT_HTTP_PATH && req.method === 'PUT') {
        if (actor.kind !== 'human') {
          try {
            this.policy.deny(actor, 'upload artifacts')
          } catch (error) {
            throw Object.assign(error as KernelError, { status: 403 })
          }
        }
        const query: ArtifactUploadQuery = { type: url.searchParams.get('type') ?? '' }
        const mediaType = url.searchParams.get('mediaType')
        const label = url.searchParams.get('label')
        const inputs = url.searchParams.getAll('input')
        if (mediaType) query.mediaType = mediaType
        if (label) query.label = label as ArtifactUploadQuery['label']
        if (inputs.length) query.inputs = inputs
        try {
          assertValid('ArtifactUploadQuery', query, 'INVALID_PARAMS')
        } catch (error) {
          throw Object.assign(error as KernelError, { status: 400 })
        }
        const meta = await this.artifacts.put(req, { ...query, creator: LOCAL_OWNER })
        return json(res, 201, meta)
      }
      const match = url.pathname.startsWith(`${ARTIFACT_HTTP_PATH}/`) && url.pathname.slice(ARTIFACT_HTTP_PATH.length + 1)
      if (match && req.method === 'GET') {
        if (!/^[0-9a-f]{64}$/.test(match)) throw httpError(400, 'INVALID_PARAMS', 'malformed artifact hash')
        const meta = this.artifacts.stat(match)
        if (!meta) throw httpError(404, 'ARTIFACT_NOT_FOUND', `artifact ${match} does not exist`, { hash: match })
        try {
          this.policy.checkDownload(actor, meta, () => readFileSync(this.artifacts.objectPath(meta.hash), 'latin1'))
        } catch (error) {
          throw Object.assign(error as KernelError, { status: 403 })
        }
        res.writeHead(200, {
          'content-type': meta.mediaType,
          'content-length': meta.size,
          'x-llmchip-label': meta.label,
        })
        await pipeline(this.artifacts.read(match), res)
        return
      }
      throw httpError(404, 'NOT_FOUND', `no route for ${req.method} ${url.pathname}`)
    } catch (error) {
      const status = (error as { status?: number }).status ?? (error instanceof KernelError ? 400 : 500)
      if (status === 500) this.logger.error('http request failed', { path: url.pathname, error: String(error) })
      if (res.headersSent) return void res.destroy()
      const data = error instanceof KernelError ? error.toData() : { code: 'INTERNAL_ERROR' }
      json(res, status, { error: { ...data, message: status === 500 ? 'internal error' : (error as Error).message } })
    }
  }
}

const CONTENT_TYPES: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.json': 'application/json',
}

function send(ws: WebSocket, message: object): void {
  if (ws.readyState === ws.OPEN) ws.send(JSON.stringify(message))
}

function errorResponse(id: string | number | null, code: number, message: string, data?: object) {
  return { jsonrpc: '2.0', id, error: { code, message, ...(data ? { data } : {}) } }
}

function httpError(status: number, code: string, message: string, params: Record<string, unknown> = {}) {
  return Object.assign(new KernelError(code, message, params), { status })
}

function json(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(text) })
  res.end(text)
}
