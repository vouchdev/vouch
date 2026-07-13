import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { fetchCapabilities, fetchHealth, VouchHttpError } from '../lib/rpc'
import type { Capabilities, VouchConnectionInfo } from '../lib/types'

/** Legacy single-endpoint storage — still read as a migration source. */
export const STORAGE_KEY = 'vouch-ui.connection.v1'
/** Multi-project storage: `{ projects: VouchConnectionInfo[], scope: string }`. */
export const STORAGE_KEY_V2 = 'vouch-ui.connections.v2'
export const ALL_SCOPE = 'all'
const HEALTH_POLL_MS = 15_000

export type HealthState = 'connecting' | 'ok' | 'down'

/** One connected vouch endpoint (= one project's KB) plus its live state. */
export interface ProjectState {
  conn: VouchConnectionInfo
  label: string
  caps: Capabilities | null
  health: HealthState
}

interface ConnectionValue {
  /** Every connected project, in the order they were added. */
  projects: ProjectState[]
  /** Projects the active scope selects — all of them, or exactly one. */
  scoped: ProjectState[]
  /** `'all'` or one project's endpoint. */
  scope: string
  setScope: (scope: string) => void
  /** True when the scope spans more than one project — views tag rows then. */
  aggregated: boolean
  /**
   * The single-target project (the scoped one, or the first when scope is
   * 'all'). Chat and other one-endpoint surfaces talk to this; aggregate
   * views ignore it and fan out over `scoped` instead.
   */
  active: ProjectState | null
  /** `active`'s connection/capabilities — the legacy single-endpoint shape. */
  conn: VouchConnectionInfo | null
  caps: Capabilities | null
  /** Worst health across the scope: any project down → down. */
  health: HealthState
  needsAuth: boolean
  /** Validate + add a project (or replace the one with the same endpoint). */
  connect: (info: VouchConnectionInfo) => Promise<void>
  removeProject: (endpoint: string) => void
  /** Remove every project. */
  disconnect: () => void
  /**
   * With `endpoint`: does that project advertise the method. Without: does
   * any project in scope — the "is this view available at all" question.
   */
  hasMethod: (method: string, endpoint?: string) => boolean
  /** Feed errors from rpc() calls made outside TanStack Query into the 401 gate. */
  reportError: (err: unknown) => void
}

const Ctx = createContext<ConnectionValue | null>(null)

export function useConnection(): ConnectionValue {
  const v = useContext(Ctx)
  if (!v) throw new Error('useConnection outside ConnectionProvider')
  return v
}

export function projectLabel(info: VouchConnectionInfo): string {
  const custom = info.label?.trim()
  if (custom) return custom
  try {
    return new URL(info.endpoint).host
  } catch {
    return info.endpoint
  }
}

interface Stored {
  projects: VouchConnectionInfo[]
  scope: string
}

function loadStored(): Stored {
  try {
    const v2 = localStorage.getItem(STORAGE_KEY_V2)
    if (v2) {
      const parsed = JSON.parse(v2) as Stored
      const projects = Array.isArray(parsed.projects)
        ? parsed.projects.filter((p) => typeof p?.endpoint === 'string')
        : []
      return { projects, scope: typeof parsed.scope === 'string' ? parsed.scope : ALL_SCOPE }
    }
    const v1 = localStorage.getItem(STORAGE_KEY)
    if (v1) {
      const parsed = JSON.parse(v1) as VouchConnectionInfo
      if (typeof parsed.endpoint === 'string') return { projects: [parsed], scope: ALL_SCOPE }
    }
  } catch {
    // fall through to the empty state
  }
  return { projects: [], scope: ALL_SCOPE }
}

function persist(stored: Stored): void {
  localStorage.setItem(STORAGE_KEY_V2, JSON.stringify(stored))
}

export function ConnectionProvider({ children }: { children: ReactNode }) {
  const [stored, setStored] = useState<Stored>(loadStored)
  const [capsMap, setCapsMap] = useState<Record<string, Capabilities>>({})
  const [healthMap, setHealthMap] = useState<Record<string, HealthState>>({})
  const [needsAuth, setNeedsAuth] = useState(false)

  // One QueryClient for the app; any 401 anywhere re-opens the connect dialog.
  const flagAuth = useCallback((err: unknown) => {
    if (err instanceof VouchHttpError && err.status === 401) setNeedsAuth(true)
  }, [])
  const clientRef = useRef<QueryClient | null>(null)
  if (!clientRef.current) {
    clientRef.current = new QueryClient({
      queryCache: new QueryCache({ onError: flagAuth }),
      mutationCache: new MutationCache({ onError: flagAuth }),
      // retryDelay 0: this is a loopback tool — an immediate single retry is
      // fine, and it keeps failing-query tests inside RTL's 1s findBy timeout.
      defaultOptions: { queries: { retry: 1, retryDelay: 0, staleTime: 5_000 } },
    })
  }

  // Endpoints already validated (or being validated) — connect() marks its
  // endpoint here so the mount effect below doesn't re-validate what connect()
  // just checked. Restored projects are validated once by the effect.
  const checkedRef = useRef<Set<string>>(new Set())

  const validate = useCallback(async (info: VouchConnectionInfo) => {
    const healthy = await fetchHealth(info)
    if (!healthy) throw new Error(`no healthy vouch endpoint at ${info.endpoint}`)
    return fetchCapabilities(info)
  }, [])

  const connect = useCallback(
    async (info: VouchConnectionInfo) => {
      const c = await validate(info) // throws → caller (dialog) shows the error
      checkedRef.current.add(info.endpoint)
      setCapsMap((m) => ({ ...m, [info.endpoint]: c }))
      setHealthMap((m) => ({ ...m, [info.endpoint]: 'ok' }))
      setStored((prev) => {
        // Replace-by-endpoint so re-connecting with a fresh token or label
        // updates the project in place instead of duplicating it.
        const at = prev.projects.findIndex((p) => p.endpoint === info.endpoint)
        const projects =
          at === -1 ? [...prev.projects, info] : prev.projects.map((p, i) => (i === at ? info : p))
        const next = { projects, scope: prev.scope }
        persist(next)
        return next
      })
      setNeedsAuth(false)
      clientRef.current?.clear()
    },
    [validate],
  )

  const removeProject = useCallback((endpoint: string) => {
    checkedRef.current.delete(endpoint)
    setStored((prev) => {
      const next = {
        projects: prev.projects.filter((p) => p.endpoint !== endpoint),
        scope: prev.scope === endpoint ? ALL_SCOPE : prev.scope,
      }
      persist(next)
      return next
    })
    clientRef.current?.clear()
  }, [])

  const disconnect = useCallback(() => {
    localStorage.removeItem(STORAGE_KEY)
    localStorage.removeItem(STORAGE_KEY_V2)
    checkedRef.current.clear()
    setStored({ projects: [], scope: ALL_SCOPE })
    setCapsMap({})
    setHealthMap({})
    setNeedsAuth(false)
    clientRef.current?.clear()
  }, [])

  const setScope = useCallback((scope: string) => {
    setStored((prev) => {
      const valid = scope === ALL_SCOPE || prev.projects.some((p) => p.endpoint === scope)
      const next = { ...prev, scope: valid ? scope : ALL_SCOPE }
      persist(next)
      return next
    })
  }, [])

  // Validate restored projects once, then poll every project's health.
  useEffect(() => {
    let stop = false
    // Endpoints whose validation THIS run started. The cleanup un-marks them
    // so a discarded in-flight validation (StrictMode's mount/unmount/mount,
    // or a projects-list change) is retried by the next run instead of being
    // skipped forever — connect()-validated endpoints stay marked and skipped.
    const started: string[] = []
    for (const info of stored.projects) {
      if (checkedRef.current.has(info.endpoint)) continue
      checkedRef.current.add(info.endpoint)
      started.push(info.endpoint)
      validate(info)
        .then((c) => {
          if (stop) return
          setCapsMap((m) => ({ ...m, [info.endpoint]: c }))
          setHealthMap((m) => ({ ...m, [info.endpoint]: 'ok' }))
        })
        .catch((err) => {
          if (stop) return
          setHealthMap((m) => ({ ...m, [info.endpoint]: 'down' }))
          flagAuth(err)
        })
    }
    const timer = setInterval(() => {
      for (const info of stored.projects) {
        fetchHealth(info).then((ok) => {
          if (!stop) setHealthMap((m) => ({ ...m, [info.endpoint]: ok ? 'ok' : 'down' }))
        })
      }
    }, HEALTH_POLL_MS)
    return () => {
      stop = true
      for (const endpoint of started) checkedRef.current.delete(endpoint)
      clearInterval(timer)
    }
  }, [stored.projects, validate, flagAuth])

  const value = useMemo<ConnectionValue>(() => {
    const projects: ProjectState[] = stored.projects.map((info) => ({
      conn: info,
      label: projectLabel(info),
      caps: capsMap[info.endpoint] ?? null,
      health: healthMap[info.endpoint] ?? 'connecting',
    }))
    // A stale scope (its project was removed elsewhere) degrades to 'all'.
    const scope =
      stored.scope === ALL_SCOPE || projects.some((p) => p.conn.endpoint === stored.scope)
        ? stored.scope
        : ALL_SCOPE
    const scoped = scope === ALL_SCOPE ? projects : projects.filter((p) => p.conn.endpoint === scope)
    const active = scoped[0] ?? null
    const health: HealthState =
      scoped.length === 0
        ? 'down'
        : scoped.some((p) => p.health === 'down')
          ? 'down'
          : scoped.some((p) => p.health === 'connecting')
            ? 'connecting'
            : 'ok'
    return {
      projects,
      scoped,
      scope,
      setScope,
      aggregated: scoped.length > 1,
      active,
      conn: active?.conn ?? null,
      caps: active?.caps ?? null,
      health,
      needsAuth,
      connect,
      removeProject,
      disconnect,
      hasMethod: (method, endpoint) => {
        if (endpoint !== undefined) {
          const p = projects.find((x) => x.conn.endpoint === endpoint)
          return p?.caps?.methods.includes(method) ?? false
        }
        return scoped.some((p) => p.caps?.methods.includes(method) ?? false)
      },
      reportError: flagAuth,
    }
  }, [stored, capsMap, healthMap, needsAuth, setScope, connect, removeProject, disconnect, flagAuth])

  return (
    <Ctx.Provider value={value}>
      <QueryClientProvider client={clientRef.current}>{children}</QueryClientProvider>
    </Ctx.Provider>
  )
}
