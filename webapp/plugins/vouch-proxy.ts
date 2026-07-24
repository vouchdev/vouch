import http from 'node:http'
import https from 'node:https'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Plugin } from 'vite'

type NextFn = (err?: unknown) => void

function fail(res: ServerResponse, status: number, code: string, message: string): void {
  if (res.headersSent) {
    // The response has already started streaming (e.g. an upstream error
    // arrived mid-body); we cannot send a fresh status/body at this point,
    // so just tear the connection down instead of crashing.
    res.destroy()
    return
  }
  res.statusCode = status
  res.setHeader('content-type', 'application/json')
  res.end(JSON.stringify({ ok: false, error: { code, message } }))
}

const LOOPBACK_ADDRESSES = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1'])

/** True only for the loopback addresses a same-origin dev-server client can have. */
export function isLoopback(addr: string | undefined): boolean {
  return addr !== undefined && LOOPBACK_ADDRESSES.has(addr)
}

/**
 * Same-origin bridge to a vouch HTTP endpoint. The browser cannot call
 * vouch cross-origin (vouch sends no CORS headers, deliberately), so the UI
 * sends every request to /proxy/* on its own origin with the real endpoint
 * in X-Vouch-Target. Third-party pages cannot drive this: a custom request
 * header forces a CORS preflight, which this middleware never answers.
 */
export function proxyMiddleware(): (req: IncomingMessage, res: ServerResponse, next: NextFn) => void {
  return (req, res, next) => {
    if (!req.url || !(req.url === '/proxy' || req.url.startsWith('/proxy/'))) return next()

    if (process.env.VOUCH_UI_ALLOW_REMOTE !== '1' && !isLoopback(req.socket.remoteAddress)) {
      return fail(res, 403, 'forbidden', 'proxy is only available to loopback clients')
    }

    const raw = req.headers['x-vouch-target']
    if (typeof raw !== 'string' || raw.length === 0) {
      return fail(res, 400, 'bad_target', 'missing X-Vouch-Target header')
    }
    let target: URL
    try {
      target = new URL(raw)
    } catch {
      return fail(res, 400, 'bad_target', `not a valid URL: ${raw}`)
    }
    if (target.protocol !== 'http:' && target.protocol !== 'https:') {
      return fail(res, 400, 'bad_target', `unsupported protocol: ${target.protocol}`)
    }

    const path = req.url.slice('/proxy'.length) || '/'
    const mod = target.protocol === 'https:' ? https : http
    const headers: Record<string, string> = {}
    if (req.headers['content-type']) headers['content-type'] = String(req.headers['content-type'])
    if (req.headers.authorization) headers.authorization = String(req.headers.authorization)
    // Reviewer identity: a human approving in the console must be attributed to
    // themselves, not left as the tokenless `unknown-agent` default (which
    // collides with the proposing agent and trips the self-approval gate).
    if (req.headers['x-vouch-agent']) headers['x-vouch-agent'] = String(req.headers['x-vouch-agent'])

    const upstream = mod.request(
      {
        hostname: target.hostname,
        port: target.port || (target.protocol === 'https:' ? 443 : 80),
        path,
        method: req.method,
        headers,
      },
      (ures) => {
        res.statusCode = ures.statusCode ?? 502
        res.setHeader('content-type', ures.headers['content-type'] ?? 'application/json')
        // A mid-stream drop on the upstream connection surfaces as an
        // 'error' here; without a listener that would throw and crash the
        // dev server. fail() already degrades to res.destroy() once headers
        // have gone out, so this is safe whether or not the body has started.
        // Also tear down the request socket itself rather than leaving it
        // half-open once the response side has failed.
        ures.on('error', (err) => {
          upstream.destroy()
          fail(res, 502, 'proxy_error', err.message)
        })
        // The client can drop mid-download (closed tab, lost connection);
        // that surfaces as an 'error' on our response stream. Without a
        // listener it would throw unhandled, and without destroying the
        // upstream request we'd keep pulling bytes nobody can receive.
        res.on('error', () => {
          upstream.destroy()
          ures.destroy()
        })
        ures.pipe(res)
      },
    )
    upstream.on('error', (err) => fail(res, 502, 'proxy_error', err.message))
    // If the client's own request errors mid-flight (e.g. it disconnects
    // while streaming a body), stop forwarding to upstream instead of
    // leaving that socket open, and avoid an unhandled 'error' throw.
    req.on('error', () => {
      upstream.destroy()
    })
    req.pipe(upstream)
  }
}

export function vouchProxy(): Plugin {
  return {
    name: 'vouch-proxy',
    configureServer(server) {
      server.middlewares.use(proxyMiddleware())
    },
    configurePreviewServer(server) {
      server.middlewares.use(proxyMiddleware())
    },
  }
}
