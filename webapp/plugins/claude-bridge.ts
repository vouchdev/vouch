import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Plugin } from 'vite'
import { isLoopback } from './vouch-proxy'

type NextFn = (err?: unknown) => void

const MAX_PROMPT_CHARS = 20_000
const RUN_TIMEOUT_MS = 600_000

function fail(res: ServerResponse, status: number, code: string, message: string): void {
  if (res.headersSent) {
    res.destroy()
    return
  }
  res.statusCode = status
  res.setHeader('content-type', 'application/json')
  res.end(JSON.stringify({ ok: false, error: { code, message } }))
}

/** The workspace claude runs in: the project whose KB the console reviews. */
export function claudeCwd(): string {
  return process.env.VOUCH_PROJECT_DIR ?? path.resolve(process.cwd(), '../vouch')
}

/**
 * Same-origin bridge that lets the chat box drive Claude Code. POST
 * /claude/run spawns `claude -p` headless in the project workspace and
 * streams its stream-json events back verbatim, one JSON line per chunk.
 *
 * Loopback-only, and the required X-Claude-Bridge header forces a CORS
 * preflight (never answered) so third-party pages cannot reach it. The
 * review gate is unaffected: claude talks to vouch through its own MCP
 * config; nothing here calls kb.approve. Headless runs still fire the
 * project's capture hooks, so chat-driven sessions land in the review
 * queue like any other session — no extra capture needed here.
 */
export function claudeBridgeMiddleware(): (
  req: IncomingMessage,
  res: ServerResponse,
  next: NextFn,
) => void {
  return (req, res, next) => {
    if (req.url !== '/claude/run') return next()
    if (req.method !== 'POST') return fail(res, 405, 'method_not_allowed', 'POST only')
    if (process.env.VOUCH_UI_ALLOW_REMOTE !== '1' && !isLoopback(req.socket.remoteAddress)) {
      return fail(res, 403, 'forbidden', 'claude bridge is only available to loopback clients')
    }
    if (req.headers['x-claude-bridge'] !== '1') {
      return fail(res, 400, 'bad_request', 'missing X-Claude-Bridge header')
    }

    let raw = ''
    req.on('data', (c) => {
      raw += String(c)
      if (raw.length > MAX_PROMPT_CHARS * 2) req.destroy()
    })
    req.on('end', () => {
      let body: { prompt?: unknown; resume?: unknown; bypassPermissions?: unknown }
      try {
        body = JSON.parse(raw || '{}')
      } catch {
        return fail(res, 400, 'bad_request', 'body must be JSON')
      }
      const prompt = typeof body.prompt === 'string' ? body.prompt.trim() : ''
      if (prompt === '' || prompt.length > MAX_PROMPT_CHARS) {
        return fail(res, 400, 'bad_request', `prompt must be 1..${MAX_PROMPT_CHARS} chars`)
      }
      const resume = typeof body.resume === 'string' && body.resume !== '' ? body.resume : null
      if (resume && !/^[0-9a-f-]{8,64}$/i.test(resume)) {
        return fail(res, 400, 'bad_request', 'resume must be a session id')
      }
      const cwd = claudeCwd()
      if (!existsSync(cwd)) {
        return fail(res, 500, 'bad_cwd', `project dir not found: ${cwd} (set VOUCH_PROJECT_DIR)`)
      }

      const args = ['-p', prompt, '--output-format', 'stream-json', '--verbose']
      args.push('--permission-mode', body.bypassPermissions === false ? 'acceptEdits' : 'bypassPermissions')
      if (resume) args.push('--resume', resume)

      const child = spawn('claude', args, {
        cwd,
        stdio: ['ignore', 'pipe', 'pipe'],
        env: process.env,
      })

      res.statusCode = 200
      res.setHeader('content-type', 'application/x-ndjson')
      res.setHeader('cache-control', 'no-cache')

      const timer = setTimeout(() => child.kill('SIGKILL'), RUN_TIMEOUT_MS)
      let stderr = ''
      let wrote = false
      child.stdout.on('data', (chunk) => {
        wrote = true
        res.write(chunk)
      })
      child.stderr.on('data', (chunk) => {
        stderr += String(chunk)
      })
      child.on('error', (err) => {
        clearTimeout(timer)
        res.write(JSON.stringify({ type: 'bridge_error', message: `failed to spawn claude: ${err.message}` }) + '\n')
        res.end()
      })
      child.on('close', (code) => {
        clearTimeout(timer)
        if (code !== 0 && !wrote) {
          res.write(
            JSON.stringify({
              type: 'bridge_error',
              message: stderr.trim() || `claude exited with code ${code}`,
            }) + '\n',
          )
        }
        res.end()
      })
      // closed tab / navigation away: stop the run instead of orphaning it
      res.on('close', () => {
        clearTimeout(timer)
        if (child.exitCode === null) child.kill('SIGTERM')
      })
    })
  }
}

export function claudeBridge(): Plugin {
  return {
    name: 'claude-bridge',
    configureServer(server) {
      server.middlewares.use(claudeBridgeMiddleware())
    },
    configurePreviewServer(server) {
      server.middlewares.use(claudeBridgeMiddleware())
    },
  }
}
