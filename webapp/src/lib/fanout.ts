import { useQueries } from '@tanstack/react-query'
import { useConnection } from '../connection/ConnectionContext'
import type { ProjectState } from '../connection/ConnectionContext'
import { rpc } from './rpc'

export interface FanoutRow<T> {
  project: ProjectState
  data: T
}

export interface FanoutResult<T> {
  /** One row per scoped project that has answered, in scope order. */
  rows: FanoutRow<T>[]
  /** True while nothing has answered yet and at least one fetch is in flight. */
  isPending: boolean
  /** True when every scoped project failed — a partial outage renders partial data instead. */
  isError: boolean
  /** Per-project failures, for inline error cards / toasts. */
  errors: { project: ProjectState; error: unknown }[]
  /** True when capabilities are known everywhere and no scoped project advertises the method. */
  unavailable: boolean
  /** Re-fetch every project in the fan-out. */
  refetch: () => void
}

/**
 * Run one kb.* read against every project in the active scope and collect the
 * answers with their owning project attached. Each project gets its own cache
 * entry (`[...key, endpoint]`), so `invalidateQueries({ queryKey: key })`
 * still hits the whole fan-out by prefix, and optimistic updates can target
 * one project's slice via `[...key, endpoint]`.
 *
 * A project whose capabilities haven't loaded yet is queried optimistically —
 * the same "try until caps say no" behaviour the single-endpoint views had.
 */
export function useFanout<T>(
  key: readonly unknown[],
  method: string,
  params: Record<string, unknown> = {},
  opts: { refetchInterval?: number; enabled?: boolean } = {},
): FanoutResult<T> {
  const { scoped } = useConnection()
  const results = useQueries({
    queries: scoped.map((p) => ({
      queryKey: [...key, p.conn.endpoint],
      queryFn: () => rpc<T>(p.conn, method, params),
      enabled: (opts.enabled ?? true) && (p.caps === null || p.caps.methods.includes(method)),
      refetchInterval: opts.refetchInterval,
    })),
  })

  const rows: FanoutRow<T>[] = []
  const errors: { project: ProjectState; error: unknown }[] = []
  results.forEach((r, i) => {
    if (r.data !== undefined) rows.push({ project: scoped[i], data: r.data })
    else if (r.isError) errors.push({ project: scoped[i], error: r.error })
  })
  const enabledCount = results.filter((r) => r.fetchStatus !== 'idle' || r.data !== undefined || r.isError).length
  return {
    rows,
    isPending: rows.length === 0 && results.some((r) => r.isLoading),
    isError: enabledCount > 0 && errors.length === enabledCount && rows.length === 0,
    errors,
    unavailable:
      scoped.length > 0 && scoped.every((p) => p.caps !== null && !p.caps.methods.includes(method)),
    refetch: () => results.forEach((r) => void r.refetch()),
  }
}
