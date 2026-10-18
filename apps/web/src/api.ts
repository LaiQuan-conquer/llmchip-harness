import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { KernelClient } from '@llmchip/client'
import type { EventRecord } from '@llmchip/kernel-api'

export interface Kernel {
  client: KernelClient
  /** Most recent events, newest last (live). */
  events: EventRecord[]
  /** Sequence number of the latest event; changes whenever anything happens. */
  head: number
}

export const KernelContext = createContext<Kernel | null>(null)

export function useKernel(): Kernel {
  const kernel = useContext(KernelContext)
  if (!kernel) throw new Error('no kernel connection')
  return kernel
}

const KEEP_EVENTS = 500

/**
 * Connects to the kernel that served this page. The token comes from the URL
 * fragment, which browsers never send to a server (`llmchip ui` builds it).
 */
export function useKernelConnection(): { kernel: Kernel | null; error: string | null } {
  const [client, setClient] = useState<KernelClient | null>(null)
  const [events, setEvents] = useState<EventRecord[]>([])
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    // A new token in the fragment (e.g. `llmchip ui` after a kernel restart) needs a fresh connection.
    const onHash = () => location.hash.includes('token=') && location.reload()
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  useEffect(() => {
    const token = new URLSearchParams(location.hash.slice(1)).get('token') ?? sessionStorage.getItem('llmchip.token')
    if (!token) {
      setError('missing-token')
      return
    }
    sessionStorage.setItem('llmchip.token', token)
    // Keep the token out of the address bar and history once it is stored.
    if (location.hash.includes('token=')) history.replaceState(null, '', location.pathname)
    let closed = false
    let connected: KernelClient | undefined
    KernelClient.connect({ url: location.origin, token })
      .then(async (c) => {
        if (closed) return c.close()
        connected = c
        const { head } = await c.call('events.list', { limit: 1 })
        await c.subscribe((event) => setEvents((prev) => [...prev.slice(-KEEP_EVENTS + 1), event]), Math.max(0, head - 200))
        setClient(c)
        void c.closed.then(() => !closed && setError('closed'))
      })
      .catch((e: Error) => setError(e.message))
    return () => {
      closed = true
      void connected?.close()
    }
  }, [])
  const head = events.at(-1)?.seq ?? 0
  return { kernel: client ? { client, events, head } : null, error }
}

/** Runs a query and re-runs it when its dependencies or the event head change. */
export function useQuery<T>(query: (client: KernelClient) => Promise<T>, deps: unknown[], live = true): { data: T | undefined; error: string | null; reload(): void } {
  const { client, head } = useKernel()
  const [data, setData] = useState<T>()
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  const queryRef = useRef(query)
  queryRef.current = query
  useEffect(() => {
    let current = true
    queryRef.current(client).then(
      (value) => current && (setData(value), setError(null)),
      (e: Error) => current && setError(e.message),
    )
    return () => {
      current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, nonce, live ? head : 0, ...deps])
  const reload = useCallback(() => setNonce((n) => n + 1), [])
  return { data, error, reload }
}
