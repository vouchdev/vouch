/*
 * jsonl-client.ts — the workhorse bridge to `vouch serve --transport jsonl`.
 *
 * Wire protocol (verified against vouch/src/vouch/jsonl_server.py):
 *   request  : {"id": <n>, "method": "kb.search", "params": {...}}\n
 *   success  : {"id": <n>, "ok": true,  "result": <any>}\n
 *   failure  : {"id": <n>, "ok": false, "error": {"code","message","traceback?"}}\n
 * One JSON object per line each way; the server processes strictly in order but
 * we correlate by `id`, so pipelining is safe and head-of-line stalls can't
 * strand later calls.
 */
import { spawn } from 'node:child_process'
import * as readline from 'node:readline'
import { EventEmitter } from 'node:events'
/** Minimal subset of a Launcher that JsonlClient actually uses. */
type LauncherLike = { cmd: string; baseArgs: string[] }

export const DEFAULT_TIMEOUT_MS = 30_000
export const LONG_TIMEOUT_MS = 10 * 60_000 // index_rebuild, reindex_embeddings, export, ...
const STDERR_RING = 400 // lines kept for diagnostics

type VouchErr = Error & { code?: string; traceback?: string }

type PendingEntry = {
  resolve: (v: unknown) => void
  reject: (e: Error) => void
  timer: ReturnType<typeof setTimeout>
  method: string
}

export class JsonlClient extends EventEmitter {
  private launcher: LauncherLike
  private root: string
  private env: Record<string, string>
  private longMethods: Set<string>
  private child: ReturnType<typeof spawn> | null
  private rl: readline.Interface | null
  private _nextId: number
  private _pending: Map<number, PendingEntry>
  private _stderr: string[]
  private _alive: boolean
  private _stopping: boolean

  constructor(
    launcher: LauncherLike,
    opts: { root: string; env?: Record<string, string>; longMethods?: Set<string> }
  ) {
    super()
    this.launcher = launcher
    this.root = opts.root
    this.env = opts.env || {}
    this.longMethods = opts.longMethods || new Set()
    this.child = null
    this.rl = null
    this._nextId = 1
    this._pending = new Map()
    this._stderr = []
    this._alive = false
    this._stopping = false
  }

  get alive(): boolean {
    return this._alive
  }

  get pid(): number | undefined | null {
    return this.child ? this.child.pid : null
  }

  get stderrTail(): string[] {
    return this._stderr.slice(-STDERR_RING)
  }

  start(): void {
    if (this.child) return
    this._stopping = false
    const args = [...this.launcher.baseArgs, 'serve', '--transport', 'jsonl']
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      VOUCH_KB_PATH: this.root.replace(/\/?$/, '') + '/.vouch',
      VOUCH_AGENT: this.env.VOUCH_AGENT || 'vouch-desktop',
      VOUCH_LOG_FORMAT: 'json',
      VOUCH_LOG_LEVEL: 'WARNING',
      ...this.env,
    }
    const child = spawn(this.launcher.cmd, args, {
      cwd: this.root,
      env,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    this.child = child
    this._alive = true

    this.rl = readline.createInterface({ input: child.stdout! })
    this.rl.on('line', (line) => this._onLine(line))

    const errRl = readline.createInterface({ input: child.stderr! })
    errRl.on('line', (line) => {
      this._stderr.push(line)
      if (this._stderr.length > STDERR_RING * 2) this._stderr = this._stderr.slice(-STDERR_RING)
      this.emit('stderr', line)
    })

    child.on('error', (err) => {
      this._alive = false
      this.emit('error', err)
    })
    child.on('exit', (code, signal) => {
      this._alive = false
      this.child = null
      // reject everything still in flight — the pipe is gone
      for (const [, p] of this._pending) {
        clearTimeout(p.timer)
        p.reject(new Error(`vouch process exited (code=${code} signal=${signal})`))
      }
      this._pending.clear()
      this.emit('exit', { code, signal, stopping: this._stopping })
    })

    this.emit('start', { pid: child.pid })
  }

  private _onLine(line: string): void {
    line = line.trim()
    if (!line) return
    let env: any
    try {
      env = JSON.parse(line)
    } catch {
      // not a protocol line (stray print) — surface for diagnostics, ignore
      this.emit('noise', line)
      return
    }
    const id = env.id
    const p = this._pending.get(id)
    if (!p) return // unknown/expired id — drop
    this._pending.delete(id)
    clearTimeout(p.timer)
    if (env.ok) p.resolve(env.result)
    else p.reject(asError(env.error))
  }

  /**
   * Call a method. Resolves with the raw `result`, rejects with an Error whose
   * `.code` carries the vouch error code.
   */
  call(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    return new Promise<unknown>((resolve, reject) => {
      if (!this.child || !this._alive) {
        const e: VouchErr = new Error('vouch process is not running')
        e.code = 'process_down'
        return reject(e)
      }
      const id = this._nextId++
      const timeoutMs = this.longMethods.has(method) ? LONG_TIMEOUT_MS : DEFAULT_TIMEOUT_MS
      const timer = setTimeout(() => {
        this._pending.delete(id)
        const e: VouchErr = new Error(`timeout after ${timeoutMs}ms calling ${method}`)
        e.code = 'timeout'
        reject(e)
      }, timeoutMs)
      this._pending.set(id, { resolve, reject, timer, method })
      const payload = JSON.stringify({ id, method, params }) + '\n'
      try {
        this.child.stdin!.write(payload)
      } catch (err) {
        this._pending.delete(id)
        clearTimeout(timer)
        reject(err as Error)
      }
    })
  }

  /** Graceful stop: close stdin (EOF-driven exit), then escalate. */
  stop({ graceMs = 2000, killMs = 5000 }: { graceMs?: number; killMs?: number } = {}): Promise<void> {
    if (!this.child) return Promise.resolve()
    this._stopping = true
    const child = this.child
    return new Promise<void>((resolve) => {
      const done = () => resolve()
      child.once('exit', done)
      try {
        child.stdin!.end()
      } catch { /* already closed */ }
      setTimeout(() => {
        if (this.child) try { child.kill('SIGINT') } catch { /* noop */ }
      }, graceMs)
      setTimeout(() => {
        if (this.child) try { child.kill('SIGKILL') } catch { /* noop */ }
      }, killMs)
    })
  }
}

function asError(err: any): VouchErr {
  const e: VouchErr = new Error((err && err.message) || 'unknown error')
  e.code = (err && err.code) || 'error'
  if (err && err.traceback) e.traceback = err.traceback
  return e
}
