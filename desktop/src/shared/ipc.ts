import type { MethodName } from './methods.gen'

export interface VouchError { code: string; message: string; traceback?: string }
export type Envelope<T> = { ok: true; result: T } | { ok: false; error: VouchError }

export interface Capabilities {
  name?: string; version?: string; spec?: string; level?: number
  methods?: string[]; retrieval?: string[]; transports?: string[]
  review_gated?: boolean
}
export type Status = Record<string, unknown>
export interface Prefs {
  autoOpenLast: boolean; closeToTray: boolean
  notifyDualSolveReady: boolean; notifyNewPending: boolean; notifyProcessDown: boolean
}
export interface RecentRoot { root: string; lastOpened: number }

export interface KbPayload {
  root: string; capabilities: Capabilities; status: Status | null; gitRepo: boolean
}
export interface KbError { error: string; code?: string }
export interface HealthPayload {
  jsonl: boolean; pending?: number | null; status?: Status; error?: string; code?: string
}
export interface ProcPayload {
  which: 'jsonl' | 'http'; state: 'up' | 'down' | 'restarting' | 'failed'
  restarts?: number; delay?: number; error?: string
}
export interface ProgressFrame { job_id: string; event: string; message?: string }
export interface WsFrame { view?: string; action?: string; [k: string]: unknown }
export interface TrayFrame { action: 'pickKb' | 'view'; view?: string }

export interface DualSolvePre {
  gitRepo: boolean
  tools: { ok: boolean; missing: string[]; sandbox: boolean; image: string }
  vouchSupportsSandbox: boolean
  webHint: boolean
}

export type PushPayloads = {
  'vouch:health': HealthPayload
  'vouch:proc': ProcPayload
  'vouch:progress': ProgressFrame
  'vouch:ws': WsFrame
  'vouch:kb': KbPayload | KbError
  'vouch:tray': TrayFrame
}
export type PushEvent = keyof PushPayloads

export interface PickFileOpts {
  title?: string; defaultPath?: string; directory?: boolean
  filters?: { name: string; extensions: string[] }[]
}

/** The exact object the preload exposes as window.vouch. */
export interface VouchApi {
  call: <T = unknown>(method: MethodName | string, params?: Record<string, unknown>) => Promise<Envelope<T>>
  openKb: (root: string) => Promise<Envelope<KbPayload>>
  pickKb: () => Promise<Envelope<string | null>>
  initKb: (root: string) => Promise<Envelope<KbPayload>>
  recentRoots: () => Promise<Envelope<RecentRoot[]>>
  capabilities: () => Promise<Envelope<Capabilities | null>>
  status: () => Promise<Envelope<Status>>
  health: () => Promise<Envelope<HealthPayload | null>>
  getPrefs: () => Promise<Envelope<Prefs>>
  setPref: (key: keyof Prefs, value: boolean) => Promise<Envelope<Prefs>>
  pickFile: (opts?: PickFileOpts) => Promise<Envelope<string | null>>
  pickSave: (opts?: PickFileOpts) => Promise<Envelope<string | null>>
  ds: {
    preconditions: () => Promise<Envelope<DualSolvePre>>
    ensure: () => Promise<Envelope<{ up: boolean; port: number; allowDualSolve: boolean }>>
    run: (body: Record<string, unknown>) => Promise<Envelope<{ job_id: string }>>
    job: (jobId: string) => Promise<Envelope<Record<string, unknown>>>
    choose: (body: Record<string, unknown>) => Promise<Envelope<Record<string, unknown>>>
  }
  openLogs: () => Promise<Envelope<string | null>>
  procInfo: () => Promise<Envelope<Record<string, unknown>>>
  on: <E extends PushEvent>(event: E, cb: (payload: PushPayloads[E]) => void) => () => void
}
