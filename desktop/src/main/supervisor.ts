/*
 * supervisor.ts — keeps the JSONL child healthy. Polls kb.status on an interval
 * (cheap, read-only), restarts the child on unexpected exit with exponential
 * backoff, and emits health/proc events the tray + status bar consume.
 */
import { EventEmitter } from 'node:events'
import type { JsonlClient } from './jsonl-client'

const POLL_MS = 8000
const BACKOFF = [250, 500, 1000, 2000, 4000]
const MAX_RESTARTS_WINDOW = 5
const WINDOW_MS = 60_000

type VouchErr = Error & { code?: string; traceback?: string }

function pendingCount(status: unknown): number | null {
  if (!status || typeof status !== 'object') return null
  const s = status as Record<string, unknown>
  // tolerate a few shapes vouch may return
  if (typeof s.pending === 'number') return s.pending
  const counts = (s.counts || s.artifacts || {}) as Record<string, unknown>
  if (typeof counts.proposals === 'number') return counts.proposals
  if (typeof counts.pending === 'number') return counts.pending
  return null
}

export class Supervisor extends EventEmitter {
  private client: JsonlClient
  private restarts: number[]
  private timer: ReturnType<typeof setInterval> | null
  lastHealth: unknown
  lastPending: number | null

  constructor(client: JsonlClient) {
    super()
    this.client = client
    this.restarts = []
    this.timer = null
    this.lastHealth = null
    this.lastPending = null
    this._wireClient()
  }

  private _wireClient(): void {
    this.client.on('exit', ({ stopping }: { stopping: boolean }) => {
      this.emit('proc', { which: 'jsonl', state: 'down', restarts: this.restarts.length })
      if (stopping) return // intentional shutdown
      this._maybeRestart()
    })
    this.client.on('start', () => {
      this.emit('proc', { which: 'jsonl', state: 'up', restarts: this.restarts.length })
    })
  }

  start(): void {
    this.stop()
    this.timer = setInterval(() => this._poll(), POLL_MS)
    this._poll()
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
  }

  private async _poll(): Promise<void> {
    if (!this.client.alive) return
    try {
      const status = await this.client.call('kb.status', {})
      const pending = pendingCount(status)
      this.lastHealth = { up: true, status }
      this.lastPending = pending
      this.emit('health', { jsonl: true, pending, status })
    } catch (err) {
      const e = err as VouchErr
      this.lastHealth = { up: false, error: e.message }
      this.emit('health', { jsonl: this.client.alive, error: e.message, code: e.code })
    }
  }

  private _maybeRestart(): void {
    const now = Date.now()
    this.restarts = this.restarts.filter((t) => now - t < WINDOW_MS)
    if (this.restarts.length >= MAX_RESTARTS_WINDOW) {
      this.emit('proc', {
        which: 'jsonl',
        state: 'failed',
        restarts: this.restarts.length,
        error: 'too many restarts; manual retry needed',
      })
      return
    }
    const delay = BACKOFF[Math.min(this.restarts.length, BACKOFF.length - 1)]
    this.restarts.push(now)
    this.emit('proc', { which: 'jsonl', state: 'restarting', restarts: this.restarts.length, delay })
    setTimeout(() => {
      try {
        this.client.start()
      } catch (e) {
        this.emit('proc', { which: 'jsonl', state: 'failed', error: (e as Error).message })
      }
    }, delay)
  }
}
