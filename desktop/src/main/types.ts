/*
 * types.ts — the shared AppCtx interface that main/index.ts, ipc.ts, and
 * tray.ts all reference.  Defined here to avoid circular imports.
 *
 * tray.ts uses an inline TrayCtx structural shape instead of importing AppCtx
 * from here, so there is no import cycle.
 */
import type { BrowserWindow } from 'electron'
import type { Capabilities, KbPayload, DualSolvePre } from '../shared/ipc'
import type { Launcher } from './vouch-locator'
import type { KbStore } from './kb-store'
import type { JsonlClient } from './jsonl-client'
import type { HttpClient } from './http-client'
import type { Supervisor } from './supervisor'

// Inline structural shapes for tray/notifier to avoid importing from ./tray
// (which would create a cycle: tray.ts cannot import AppCtx from ./types while
// ./types imports from ./tray).
interface TrayHandle {
  setRoot(root: string): void
  setPending(n: number | null | undefined): void
  destroy(): void
}
interface NotifierLike {
  onDualSolve(f: { event: string; message?: string }): void
  onHealth(hp: { pending?: number | null } | null): void
  onProc(p: { state: string; error?: string }): void
}

export interface AppCtx {
  store: KbStore | null
  win: BrowserWindow | null
  tray: TrayHandle | null
  notifier: NotifierLike | null
  launcher: Launcher | null
  jsonl: JsonlClient | null
  http: HttpClient | null
  supervisor: Supervisor | null
  methodNames: Set<string>
  state: { root: string | null; capabilities: Capabilities | null }
  logDir: string | null
  getWindow: () => BrowserWindow | null
  send: (channel: string, payload: unknown) => void
  resolveLauncher: () => Launcher | null
  openKb: (root: string) => Promise<KbPayload>
  initKb: (root: string) => Promise<KbPayload>
  dualSolvePreconditions: () => DualSolvePre
  ensureHttp: () => Promise<{ up: boolean; port: number; allowDualSolve: boolean }>
  requireHttp: () => HttpClient
  teardownChildren: () => Promise<void>
}
