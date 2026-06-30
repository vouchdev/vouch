// client.ts — thin wrapper over the preload's window.vouch bridge. Unwraps the
// {ok, result} / {ok:false, error} envelope into a value-or-throw API so views
// can `await call(...)` and catch a real Error (with .code).
import type {
  Envelope,
  KbPayload,
  Capabilities,
  Status,
  Prefs,
  RecentRoot,
  DualSolvePre,
  PickFileOpts,
  PushEvent,
  PushPayloads,
} from '../../../shared/ipc'

const v = window.vouch

type VouchErr = Error & { code?: string; traceback?: string }

function unwrap<T>(r: Envelope<T>): T {
  if (r && r.ok) return r.result
  const err = new Error(
    (r && !r.ok && r.error?.message) || "request failed"
  ) as VouchErr
  err.code = (r && !r.ok) ? r.error?.code : undefined
  err.traceback = (r && !r.ok) ? r.error?.traceback : undefined
  throw err
}

export const call = async <T = unknown>(method: string, params?: Record<string, unknown>): Promise<T> =>
  unwrap(await v.call<T>(method, params ?? {}))

export const openKb = async (root: string): Promise<KbPayload> =>
  unwrap(await v.openKb(root))

export const pickKb = async (): Promise<string | null> =>
  unwrap(await v.pickKb())

export const initKb = async (root: string): Promise<KbPayload> =>
  unwrap(await v.initKb(root))

export const recentRoots = async (): Promise<RecentRoot[]> =>
  unwrap(await v.recentRoots())

export const capabilities = async (): Promise<Capabilities | null> =>
  unwrap(await v.capabilities())

export const status = async (): Promise<Status> =>
  unwrap(await v.status())

export const getPrefs = async (): Promise<Prefs> =>
  unwrap(await v.getPrefs())

export const setPref = async (k: keyof Prefs, val: boolean): Promise<Prefs> =>
  unwrap(await v.setPref(k, val))

export const pickFile = async (opts?: PickFileOpts): Promise<string | null> =>
  unwrap(await v.pickFile(opts))

export const pickSave = async (opts?: PickFileOpts): Promise<string | null> =>
  unwrap(await v.pickSave(opts))

export const procInfo = async (): Promise<Record<string, unknown>> =>
  unwrap(await v.procInfo())

export const ds = {
  preconditions: async (): Promise<DualSolvePre> =>
    unwrap(await v.ds.preconditions()),
  ensure: async (): Promise<{ up: boolean; port: number; allowDualSolve: boolean }> =>
    unwrap(await v.ds.ensure()),
  run: async (body: Record<string, unknown>): Promise<{ job_id: string }> =>
    unwrap(await v.ds.run(body)),
  job: async (id: string): Promise<Record<string, unknown>> =>
    unwrap(await v.ds.job(id)),
  choose: async (body: Record<string, unknown>): Promise<Record<string, unknown>> =>
    unwrap(await v.ds.choose(body)),
}

export const on = <E extends PushEvent>(event: E, cb: (payload: PushPayloads[E]) => void): (() => void) =>
  v.on(event, cb)
