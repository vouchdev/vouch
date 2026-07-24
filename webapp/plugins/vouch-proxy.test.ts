// @vitest-environment node
import http from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterAll, beforeAll, expect, test } from 'vitest'
import { isLoopback, proxyMiddleware } from './vouch-proxy'

let upstream: http.Server
let upstreamUrl: string
let proxy: http.Server
let proxyUrl: string

beforeAll(async () => {
  // Upstream echoes method, path, auth header, and body back as JSON.
  upstream = http.createServer((req, res) => {
    let body = ''
    req.on('data', (c) => (body += c))
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(
        JSON.stringify({
          method: req.method,
          path: req.url,
          auth: req.headers.authorization ?? null,
          agent: req.headers['x-vouch-agent'] ?? null,
          body,
        }),
      )
    })
  })
  await new Promise<void>((r) => upstream.listen(0, '127.0.0.1', r))
  upstreamUrl = `http://127.0.0.1:${(upstream.address() as AddressInfo).port}`

  const mw = proxyMiddleware()
  proxy = http.createServer((req, res) => {
    mw(req, res, () => {
      res.statusCode = 404
      res.end('not proxied')
    })
  })
  await new Promise<void>((r) => proxy.listen(0, '127.0.0.1', r))
  proxyUrl = `http://127.0.0.1:${(proxy.address() as AddressInfo).port}`
})

afterAll(() => {
  upstream.close()
  proxy.close()
})

test('forwards POST body and Authorization to the target, rewriting /proxy prefix', async () => {
  const res = await fetch(`${proxyUrl}/proxy/rpc`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-vouch-target': upstreamUrl,
      authorization: 'Bearer sekrit',
    },
    body: JSON.stringify({ id: '1', method: 'kb.status', params: {} }),
  })
  expect(res.status).toBe(200)
  const echoed = await res.json()
  expect(echoed.method).toBe('POST')
  expect(echoed.path).toBe('/rpc')
  expect(echoed.auth).toBe('Bearer sekrit')
  expect(JSON.parse(echoed.body).method).toBe('kb.status')
})

test('forwards the X-Vouch-Agent reviewer identity to the target', async () => {
  const res = await fetch(`${proxyUrl}/proxy/rpc`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-vouch-target': upstreamUrl,
      'x-vouch-agent': 'alice',
    },
    body: JSON.stringify({ id: '1', method: 'kb.approve', params: {} }),
  })
  expect(res.status).toBe(200)
  const echoed = await res.json()
  expect(echoed.agent).toBe('alice')
})

test('forwards GET /proxy/health to target /health', async () => {
  const res = await fetch(`${proxyUrl}/proxy/health`, {
    headers: { 'x-vouch-target': upstreamUrl },
  })
  const echoed = await res.json()
  expect(echoed.method).toBe('GET')
  expect(echoed.path).toBe('/health')
})

test('rejects a missing X-Vouch-Target with 400', async () => {
  const res = await fetch(`${proxyUrl}/proxy/rpc`, { method: 'POST', body: '{}' })
  expect(res.status).toBe(400)
  const body = await res.json()
  expect(body.error.code).toBe('bad_target')
})

test('rejects a non-http(s) target with 400', async () => {
  const res = await fetch(`${proxyUrl}/proxy/rpc`, {
    method: 'POST',
    headers: { 'x-vouch-target': 'file:///etc/passwd' },
    body: '{}',
  })
  expect(res.status).toBe(400)
})

test('returns 502 when the target is unreachable', async () => {
  const res = await fetch(`${proxyUrl}/proxy/rpc`, {
    method: 'POST',
    headers: { 'x-vouch-target': 'http://127.0.0.1:1' },
    body: '{}',
  })
  expect(res.status).toBe(502)
  const body = await res.json()
  expect(body.error.code).toBe('proxy_error')
})

test('ignores non-/proxy paths (calls next)', async () => {
  const res = await fetch(`${proxyUrl}/other`)
  expect(res.status).toBe(404)
  expect(await res.text()).toBe('not proxied')
})

test('a mid-stream upstream drop terminates the request without crashing the proxy', async () => {
  // Upstream sends headers and a partial body, then hard-drops the socket —
  // simulating a crash/restart on the real vouch server mid-response.
  const dropUpstream = http.createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.write('{"partial":true,"chunk":"start-of-body')
    res.destroy()
  })
  await new Promise<void>((r) => dropUpstream.listen(0, '127.0.0.1', r))
  const dropUrl = `http://127.0.0.1:${(dropUpstream.address() as AddressInfo).port}`

  try {
    const res = await fetch(`${proxyUrl}/proxy/rpc`, {
      headers: { 'x-vouch-target': dropUrl },
    })
    const text = await res.text()
    // If the fetch resolved at all, the body must be truncated: never a
    // complete, valid JSON document.
    expect(() => JSON.parse(text)).toThrow()
  } catch {
    // The fetch rejecting outright (e.g. premature close/ECONNRESET) is an
    // equally acceptable way for the request to terminate.
  }

  dropUpstream.close()

  // The proxy process must have survived the drop and keep serving normal
  // requests — this is the real assertion: nothing above crashed the server.
  const res2 = await fetch(`${proxyUrl}/proxy/health`, {
    headers: { 'x-vouch-target': upstreamUrl },
  })
  expect(res2.status).toBe(200)
  expect((await res2.json()).method).toBe('GET')
})

test('isLoopback accepts only loopback addresses', () => {
  expect(isLoopback('127.0.0.1')).toBe(true)
  expect(isLoopback('::1')).toBe(true)
  expect(isLoopback('::ffff:127.0.0.1')).toBe(true)
  expect(isLoopback('10.0.0.5')).toBe(false)
  expect(isLoopback(undefined)).toBe(false)
})

test('normal loopback requests still pass through the proxy (no VOUCH_UI_ALLOW_REMOTE needed)', async () => {
  expect(process.env.VOUCH_UI_ALLOW_REMOTE).toBeUndefined()
  const res = await fetch(`${proxyUrl}/proxy/health`, {
    headers: { 'x-vouch-target': upstreamUrl },
  })
  expect(res.status).toBe(200)
})
