import type { Capabilities, Envelope, VouchConnectionInfo } from './types'

export class VouchRpcError extends Error {
  constructor(
    public code: string,
    message: string,
  ) {
    super(message)
    this.name = 'VouchRpcError'
  }
}

export class VouchHttpError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
    this.name = 'VouchHttpError'
  }
}

let seq = 0

function baseHeaders(conn: VouchConnectionInfo): Record<string, string> {
  const h: Record<string, string> = { 'x-vouch-target': conn.endpoint }
  if (conn.token) h.authorization = `Bearer ${conn.token}`
  return h
}

export async function rpc<T>(
  conn: VouchConnectionInfo,
  method: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  const res = await fetch('/proxy/rpc', {
    method: 'POST',
    headers: { ...baseHeaders(conn), 'content-type': 'application/json' },
    body: JSON.stringify({ id: `ui-${++seq}`, method, params }),
  })
  if (res.status === 401) throw new VouchHttpError(401, 'unauthorized — check the bearer token')
  if (!res.ok) throw new VouchHttpError(res.status, `endpoint returned HTTP ${res.status}`)
  const body = (await res.json()) as Envelope<T>
  if (!body.ok || body.error) {
    throw new VouchRpcError(body.error?.code ?? 'unknown', body.error?.message ?? 'unknown error')
  }
  return unwrapListEnvelope(method, body.result) as T
}

/**
 * `kb.list_*` results moved from a bare array to a `{ items, _meta }` dict
 * envelope (server deprecation, remove_in 1.4.0). Consumers still type these
 * as flat arrays, so unwrap `items` here and keep tolerating the old shape —
 * one place, so the fan-out, optimistic caches, and views are untouched.
 * `kb.list_sessions` keeps its own `{ sessions }` key and has no `items`, so it
 * passes through unchanged.
 */
function unwrapListEnvelope<T>(method: string, result: T): T {
  if (
    method.startsWith('kb.list_') &&
    result !== null &&
    typeof result === 'object' &&
    !Array.isArray(result) &&
    Array.isArray((result as { items?: unknown }).items)
  ) {
    return (result as unknown as { items: T }).items
  }
  return result
}

export async function fetchHealth(conn: VouchConnectionInfo): Promise<boolean> {
  try {
    const res = await fetch('/proxy/health', { headers: baseHeaders(conn) })
    if (!res.ok) return false
    const body = (await res.json()) as { ok?: boolean }
    return body.ok === true
  } catch {
    return false
  }
}

export async function fetchCapabilities(conn: VouchConnectionInfo): Promise<Capabilities> {
  const res = await fetch('/proxy/capabilities', { headers: baseHeaders(conn) })
  if (res.status === 401) throw new VouchHttpError(401, 'unauthorized — check the bearer token')
  if (!res.ok) throw new VouchHttpError(res.status, `capabilities returned HTTP ${res.status}`)
  return (await res.json()) as Capabilities
}
