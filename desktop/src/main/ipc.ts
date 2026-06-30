/*
 * ipc.ts — the only place renderer calls cross into Node. Every handler is a
 * thin delegate over the app context (ctx) built in index.ts. Results are
 * normalized to {ok, result} / {ok:false, error} so the renderer never has to
 * wrap each call in try/catch.
 */
import { ipcMain, dialog, shell } from 'electron'
import type { Envelope, VouchError, Prefs, PickFileOpts } from '../shared/ipc'
import type { AppCtx } from './types'

type VouchErr = Error & { code?: string; traceback?: string }

function ok<T>(result: T): Envelope<T> { return { ok: true, result } }
function fail(err: VouchErr): Envelope<never> {
  const e: VouchError = { code: err.code || 'error', message: err.message || String(err) }
  if (err.traceback !== undefined) e.traceback = err.traceback
  return { ok: false, error: e }
}

export function registerIpc(ctx: AppCtx): void {
  const h = (channel: string, fn: (arg: unknown) => unknown) =>
    ipcMain.handle(channel, async (_e, arg) => {
      try { return ok(await fn(arg)) } catch (err) { return fail(err as VouchErr) }
    })

  // --- the universal JSONL bridge -------------------------------------------
  h('vouch:call', async (arg) => {
    const { method, params } = arg as { method: string; params?: Record<string, unknown> }
    if (!ctx.methodNames.has(method)) throw new Error(`unknown method: ${method}`)
    if (!ctx.jsonl || !ctx.jsonl.alive) {
      const e = Object.assign(new Error('no knowledge base is open'), { code: 'no_kb' })
      throw e
    }
    return ctx.jsonl.call(method, params || {})
  })

  // --- KB lifecycle ----------------------------------------------------------
  h('vouch:openKb', (arg) => { const { root } = arg as { root: string }; return ctx.openKb(root) })
  h('vouch:initKb', (arg) => { const { root } = arg as { root: string }; return ctx.initKb(root) })
  h('vouch:recentRoots', () => ctx.store!.get('recentRoots') || [])
  h('vouch:capabilities', () => ctx.state.capabilities)
  h('vouch:status', () => {
    if (!ctx.jsonl || !ctx.jsonl.alive) {
      const e = Object.assign(new Error('no knowledge base is open'), { code: 'no_kb' })
      throw e
    }
    return ctx.jsonl.call('kb.status', {})
  })
  h('vouch:health', () => ctx.supervisor ? ctx.supervisor.lastHealth : null)

  ipcMain.handle('vouch:pickKb', async () => {
    const win = ctx.getWindow()
    const r = await (win
      ? dialog.showOpenDialog(win, { title: 'Choose a vouch knowledge base (a folder containing .vouch/)', properties: ['openDirectory', 'createDirectory'] })
      : dialog.showOpenDialog({ title: 'Choose a vouch knowledge base (a folder containing .vouch/)', properties: ['openDirectory', 'createDirectory'] }))
    return r.canceled ? { ok: true, result: null } : { ok: true, result: r.filePaths[0] }
  })

  // --- prefs -----------------------------------------------------------------
  h('vouch:getPrefs', () => ctx.store!.prefs)
  h('vouch:setPref', (arg) => {
    const { key, value } = arg as { key: keyof Prefs; value: boolean }
    ctx.store!.setPref(key, value)
    return ctx.store!.prefs
  })

  // --- native pickers --------------------------------------------------------
  ipcMain.handle('vouch:pickFile', async (_e, opts: PickFileOpts = {}) => {
    const win = ctx.getWindow()
    const pickOpts = { title: opts.title || 'Choose a file', defaultPath: opts.defaultPath || ctx.state.root || undefined,
      properties: [opts.directory ? 'openDirectory' : 'openFile'] as ('openDirectory' | 'openFile')[], filters: opts.filters }
    const r = await (win ? dialog.showOpenDialog(win, pickOpts) : dialog.showOpenDialog(pickOpts))
    return { ok: true, result: r.canceled ? null : r.filePaths[0] }
  })
  ipcMain.handle('vouch:pickSave', async (_e, opts: PickFileOpts = {}) => {
    const win = ctx.getWindow()
    const saveOpts = { title: opts.title || 'Save as', defaultPath: opts.defaultPath || ctx.state.root || undefined, filters: opts.filters }
    const r = await (win ? dialog.showSaveDialog(win, saveOpts) : dialog.showSaveDialog(saveOpts))
    return { ok: true, result: r.canceled ? null : r.filePath }
  })

  // --- dual-solve (HTTP child) ----------------------------------------------
  h('vouch:ds:pre', () => ctx.dualSolvePreconditions())
  h('vouch:ds:ensure', () => ctx.ensureHttp())
  h('vouch:ds:run', (arg) => ctx.requireHttp().dualSolveRun(arg as Record<string, unknown>))
  h('vouch:ds:job', (arg) => { const { jobId } = arg as { jobId: string }; return ctx.requireHttp().dualSolveJob(jobId) })
  h('vouch:ds:choose', (arg) => ctx.requireHttp().dualSolveChoose(arg as Record<string, unknown>))

  // --- diagnostics -----------------------------------------------------------
  h('vouch:procInfo', () => ({
    jsonl: ctx.jsonl ? { alive: ctx.jsonl.alive, pid: ctx.jsonl.pid,
      stderrTail: ctx.jsonl.stderrTail.slice(-40) } : null,
    http: ctx.http ? { up: ctx.http.up, port: ctx.http.port } : null,
    vouch: ctx.launcher ? { cmd: ctx.launcher.cmd, kind: ctx.launcher.kind, version: ctx.launcher.version } : null,
    root: ctx.state.root,
  }))
  ipcMain.handle('vouch:openLogs', async () => {
    if (ctx.logDir) await shell.openPath(ctx.logDir)
    return { ok: true, result: ctx.logDir }
  })
}
