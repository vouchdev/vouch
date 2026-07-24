import { afterEach, expect, test, vi } from 'vitest'
import { fetchCapabilities, fetchHealth, rpc, VouchHttpError, VouchRpcError } from './rpc'
import type { VouchConnectionInfo } from './types'

const conn: VouchConnectionInfo = { endpoint: 'http://127.0.0.1:8731', token: 'tok' }

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
  )
  vi.stubGlobal('fetch', fn)
  return fn
}

afterEach(() => vi.unstubAllGlobals())

test('rpc posts the envelope to /proxy/rpc with target + bearer headers and unwraps result', async () => {
  const fn = mockFetch(200, { id: 'ui-1', ok: true, result: { claims: 2 } })
  const out = await rpc<{ claims: number }>(conn, 'kb.status')
  expect(out.claims).toBe(2)
  const [url, init] = fn.mock.calls[0]
  expect(url).toBe('/proxy/rpc')
  expect(init.method).toBe('POST')
  expect(init.headers['x-vouch-target']).toBe('http://127.0.0.1:8731')
  expect(init.headers.authorization).toBe('Bearer tok')
  const sent = JSON.parse(init.body)
  expect(sent.method).toBe('kb.status')
  expect(sent.params).toEqual({})
  expect(sent.id).toMatch(/^ui-\d+$/)
})

test('rpc unwraps the {items} envelope for kb.list_* results', async () => {
  mockFetch(200, { id: 'x', ok: true, result: { items: [{ id: 'a' }, { id: 'b' }], _meta: { deprecation: {} } } })
  const out = await rpc<{ id: string }[]>(conn, 'kb.list_pending')
  expect(out).toEqual([{ id: 'a' }, { id: 'b' }])
})

test('rpc passes a bare-array kb.list_* result through unchanged (old server)', async () => {
  mockFetch(200, { id: 'x', ok: true, result: [{ id: 'a' }] })
  const out = await rpc<{ id: string }[]>(conn, 'kb.list_claims')
  expect(out).toEqual([{ id: 'a' }])
})

test('rpc leaves kb.list_sessions {sessions} envelope untouched', async () => {
  mockFetch(200, { id: 'x', ok: true, result: { sessions: [{ session_id: 's1' }] } })
  const out = await rpc<{ sessions: { session_id: string }[] }>(conn, 'kb.list_sessions')
  expect(out).toEqual({ sessions: [{ session_id: 's1' }] })
})

test('rpc does not unwrap items for non-list methods', async () => {
  mockFetch(200, { id: 'x', ok: true, result: { items: [1, 2, 3] } })
  const out = await rpc<{ items: number[] }>(conn, 'kb.synthesize')
  expect(out).toEqual({ items: [1, 2, 3] })
})

test('rpc omits authorization header when no token', async () => {
  const fn = mockFetch(200, { id: 'x', ok: true, result: {} })
  await rpc({ endpoint: 'http://127.0.0.1:8731' }, 'kb.status')
  const [, init] = fn.mock.calls[0]
  expect(init.headers.authorization).toBeUndefined()
})

test('rpc throws VouchRpcError on ok:false envelopes', async () => {
  mockFetch(200, { id: 'x', ok: false, error: { code: 'method_not_found', message: 'unknown method: bogus' } })
  const err = await rpc(conn, 'bogus').catch((e: unknown) => e)
  expect(err).toBeInstanceOf(VouchRpcError)
  if (!(err instanceof VouchRpcError)) throw new Error('unreachable')
  expect(err.code).toBe('method_not_found')
  expect(err.message).toBe('unknown method: bogus')
})

test('rpc throws VouchHttpError(401) on unauthorized', async () => {
  mockFetch(401, { detail: 'unauthorized' })
  const err = await rpc(conn, 'kb.status').catch((e: unknown) => e)
  expect(err).toBeInstanceOf(VouchHttpError)
  if (!(err instanceof VouchHttpError)) throw new Error('unreachable')
  expect(err.status).toBe(401)
})

test('fetchHealth returns true only for {ok:true}', async () => {
  mockFetch(200, { ok: true })
  expect(await fetchHealth(conn)).toBe(true)
  mockFetch(200, { ok: false })
  expect(await fetchHealth(conn)).toBe(false)
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed')))
  expect(await fetchHealth(conn)).toBe(false)
})

test('fetchCapabilities returns the descriptor', async () => {
  mockFetch(200, { name: 'vouch', level: 3, methods: ['kb.status'], review_gated: true })
  const caps = await fetchCapabilities(conn)
  expect(caps.methods).toContain('kb.status')
})
