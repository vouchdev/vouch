/*
 * http-client.ts — lazily spawns `vouch review-ui` and drives the ONE thing
 * JSONL can't do: the dual-solve runner (long-running, two engines, streamed
 * progress). The review queue itself runs entirely over JSONL, so this child is
 * only started when the user opens the Dual-Solve view.
 *
 * Launch (verified): vouch review-ui --bind 127.0.0.1:PORT --no-open-browser
 *                    --kb <root> --allow-dual-solve --dual-solve-sandbox
 *                    (tokenless on loopback)
 * Endpoints:  GET /healthz · POST /dual-solve/run · GET /dual-solve/job/{id}
 *             POST /dual-solve/choose · WS /ws (type:"dual_solve" frames)
 */
import { spawn, type ChildProcess } from 'node:child_process'
import * as net from 'node:net'
import { EventEmitter } from 'node:events'
import type { Launcher } from './vouch-locator'

type VouchErr = Error & { code?: string | number; traceback?: string; data?: unknown }

/** Only the fields HttpClient actually reads from a Launcher. */
type LauncherRef = Pick<Launcher, 'cmd' | 'baseArgs'>

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.unref()
    srv.on('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const port = (srv.address() as net.AddressInfo).port
      srv.close(() => resolve(port))
    })
  })
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

export class HttpClient extends EventEmitter {
  private launcher: LauncherRef
  private root: string
  private env: Record<string, string>
  private sandboxDualSolve: boolean
  private sandboxImage: string | null
  private child: ChildProcess | null
  port: number | null
  private base: string | null
  private ws: WebSocket | null
  private _stderr: string[]
  private _wantWs: boolean

  constructor(
    launcher: LauncherRef,
    opts: {
      root: string
      env?: Record<string, string>
      sandboxDualSolve?: boolean
      sandboxImage?: string | null
    },
  ) {
    super()
    this.launcher = launcher
    this.root = opts.root
    this.env = opts.env || {}
    this.sandboxDualSolve = opts.sandboxDualSolve !== false
    this.sandboxImage = opts.sandboxImage || null
    this.child = null
    this.port = null
    this.base = null
    this.ws = null
    this._stderr = []
    this._wantWs = false
  }

  get up(): boolean {
    return !!this.child && !!this.base
  }

  /** Idempotent: spawn review-ui (if needed) and wait until /healthz answers. */
  async ensure({ allowDualSolve = true } = {}): Promise<{ up: boolean; port: number; allowDualSolve: boolean }> {
    if (this.up) return { up: true, port: this.port!, allowDualSolve: true }
    this.port = await freePort()
    this.base = `http://127.0.0.1:${this.port}`
    const args: string[] = [
      ...this.launcher.baseArgs, 'review-ui',
      '--bind', `127.0.0.1:${this.port}`,
      '--no-open-browser',
      '--kb', this.root,
    ]
    if (allowDualSolve) {
      args.push('--allow-dual-solve')
      if (this.sandboxDualSolve) {
        args.push('--dual-solve-sandbox')
        if (this.sandboxImage) args.push('--dual-solve-sandbox-image', this.sandboxImage)
      }
    }
    const child = spawn(this.launcher.cmd, args, {
      cwd: this.root,
      env: { ...process.env, VOUCH_KB_PATH: this.root.replace(/\/?$/, '') + '/.vouch',
        VOUCH_AGENT: this.env.VOUCH_AGENT || 'vouch-desktop', ...this.env },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    this.child = child
    child.stdout!.on('data', (d: Buffer) => this.emit('stdout', d.toString()))
    child.stderr!.on('data', (d: Buffer) => {
      const s = d.toString()
      this._stderr.push(s)
      if (this._stderr.length > 200) this._stderr = this._stderr.slice(-200)
      this.emit('stderr', s)
    })
    child.on('exit', (code: number | null, signal: NodeJS.Signals | null) => {
      this.child = null
      this.base = null
      this._closeWs()
      this.emit('exit', { code, signal })
    })

    // wait for liveness (review-ui needs the [web] extra; if missing, it exits)
    const deadline = Date.now() + 15_000
    while (Date.now() < deadline) {
      if (!this.child) {
        throw new Error('review-ui exited before becoming ready (is the vouch [web] extra installed?): ' +
          this._stderr.join('').slice(-400))
      }
      try {
        const r = await fetch(`${this.base}/healthz`)
        if (r.ok) {
          this._connectWs()
          this.emit('up', { port: this.port })
          return { up: true, port: this.port!, allowDualSolve }
        }
      } catch { /* not listening yet */ }
      await sleep(300)
    }
    throw new Error('review-ui did not become healthy within 15s')
  }

  private _connectWs(): void {
    this._wantWs = true
    try {
      this.ws = new WebSocket(`ws://127.0.0.1:${this.port}/ws`)
      this.ws.addEventListener('message', (ev: MessageEvent) => {
        let f: { type?: string } & Record<string, unknown>
        try { f = JSON.parse(ev.data as string) as typeof f } catch { return }
        if (f.type === 'dual_solve') this.emit('dual_solve', f)
        else if (f.type === 'refresh') this.emit('refresh', f)
      })
      this.ws.addEventListener('close', () => {
        if (this._wantWs && this.up) setTimeout(() => this._connectWs(), 1000)
      })
      this.ws.addEventListener('error', () => { /* close handler reconnects */ })
    } catch (e) {
      this.emit('stderr', 'ws connect failed: ' + (e as Error).message)
    }
  }

  private _closeWs(): void {
    this._wantWs = false
    if (this.ws) try { this.ws.close() } catch { /* noop */ }
    this.ws = null
  }

  private async _json(method: string, path: string, body?: unknown): Promise<unknown> {
    const r = await fetch(this.base! + path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
    const text = await r.text()
    let data: Record<string, unknown> | null = null
    try { data = text ? (JSON.parse(text) as Record<string, unknown>) : null } catch { data = { raw: text } }
    if (!r.ok) {
      const e: VouchErr = new Error((data && ((data.detail as string) || (data.message as string))) || `HTTP ${r.status}`)
      e.code = r.status
      e.data = data
      throw e
    }
    return data
  }

  dualSolveRun(body: unknown): Promise<unknown> { return this._json('POST', '/dual-solve/run', body) }
  dualSolveJob(jobId: string): Promise<unknown> { return this._json('GET', `/dual-solve/job/${encodeURIComponent(jobId)}`) }
  dualSolveChoose(body: unknown): Promise<unknown> { return this._json('POST', '/dual-solve/choose', body) }

  async stop(): Promise<void> {
    this._closeWs()
    if (!this.child) return
    const child = this.child
    return new Promise<void>((resolve) => {
      child.once('exit', () => resolve())
      try { child.kill('SIGINT') } catch { /* noop */ }
      setTimeout(() => { try { child.kill('SIGKILL') } catch { /* noop */ } }, 4000)
    })
  }
}
