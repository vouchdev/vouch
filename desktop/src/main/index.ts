/*
 * index.ts — app entry. Owns the app context (ctx): the vouch launcher, the
 * JSONL workhorse child + supervisor, the lazy dual-solve HTTP child, the
 * persisted store, the window, and the tray. Wires the supervisor/http push
 * events into the renderer and into OS notifications.
 */
import { app, BrowserWindow } from 'electron'
import { join } from 'node:path'
import * as fs from 'node:fs'

import { JsonlClient } from './jsonl-client'
import { HttpClient } from './http-client'
import { Supervisor } from './supervisor'
import { KbStore } from './kb-store'
import * as locator from './vouch-locator'
import { registerIpc } from './ipc'
import { installTray, Notifier } from './tray'
import type { AppCtx } from './types'
import type { Capabilities, KbPayload } from '../shared/ipc'
import methods from '../shared/methods.gen'

type VouchErr = Error & { code?: string; traceback?: string }

if (process.env.VOUCH_DESKTOP_DISABLE_CHROME_SANDBOX === '1') {
  app.commandLine.appendSwitch('no-sandbox')
  app.commandLine.appendSwitch('disable-setuid-sandbox')
}

const LONG_METHODS = new Set([
  'kb.index_rebuild', 'kb.reindex_embeddings', 'kb.export', 'kb.import_apply',
  'kb.provenance_rebuild', 'kb.dedup_scan', 'kb.eval_embeddings', 'kb.source_verify',
  'kb.synthesize',
])

function loadMethodNames(): Set<string> {
  // electron-vite build: read the generated catalog directly (no file read).
  return new Set(methods.map((m) => m.name))
}

const ctx: AppCtx = {
  store: null,
  win: null,
  tray: null,
  notifier: null,
  launcher: null,
  jsonl: null,
  http: null,
  supervisor: null,
  methodNames: loadMethodNames(),
  state: { root: null, capabilities: null },
  logDir: null,
  getWindow: () => ctx.win,

  send(channel: string, payload: unknown) {
    if (ctx.win && !ctx.win.isDestroyed()) ctx.win.webContents.send(channel, payload)
  },

  resolveLauncher() {
    const bundledDir = join(process.resourcesPath || '', 'vouch')
    ctx.launcher = locator.resolveVouch({
      configuredPath: ctx.store!.get('vouchPath') || undefined,
      bundledDir: fs.existsSync(bundledDir) ? bundledDir : undefined,
    })
    return ctx.launcher
  },

  async openKb(root: string) {
    root = locator.normalizeKbRoot(root)!
    if (!root) throw new Error('no path given')
    if (!ctx.launcher && !ctx.resolveLauncher()) {
      const e: VouchErr = new Error('could not find a runnable `vouch` (install it, or set its path in settings)')
      e.code = 'no_vouch'; throw e
    }
    if (!locator.hasKb(root)) {
      const e: VouchErr = new Error(`no .vouch/ found in ${root}`); e.code = 'no_kb_here'; throw e
    }
    // tear down any current children
    await ctx.teardownChildren()

    const env: Record<string, string> = { VOUCH_AGENT: locator.defaultAgent() }
    if (ctx.logDir) env.VOUCH_LOG_FILE = join(ctx.logDir, 'vouch-jsonl.log')
    ctx.jsonl = new JsonlClient(ctx.launcher!, { root, env, longMethods: LONG_METHODS })
    ctx.jsonl.on('stderr', (l: string) => appendLog(ctx, 'jsonl', l))
    ctx.jsonl.start()

    ctx.supervisor = new Supervisor(ctx.jsonl)
    ctx.supervisor.on('health', (hp: unknown) => {
      ctx.send('vouch:health', hp)
      ctx.notifier && ctx.notifier.onHealth(hp as { pending?: number | null } | null)
      ctx.tray && ctx.tray.setPending((hp as { pending?: number | null } | null)?.pending)
    })
    ctx.supervisor.on('proc', (p: unknown) => {
      ctx.send('vouch:proc', p)
      ctx.notifier && ctx.notifier.onProc(p as { state: string; error?: string })
    })

    // probe capabilities + status (short retry; the child may still be warming)
    let capabilities = null, status = null
    for (let i = 0; i < 10; i++) {
      try { capabilities = await ctx.jsonl!.call('kb.capabilities', {}); break }
      catch { await new Promise((r) => setTimeout(r, 250)) }
    }
    if (!capabilities) {
      const e: VouchErr = new Error('vouch started but did not answer kb.capabilities')
      e.code = 'no_response'; throw e
    }
    try { status = await ctx.jsonl!.call('kb.status', {}) } catch { /* tolerate bad data */ }

    ctx.state.root = root
    ctx.state.capabilities = capabilities as Capabilities
    ctx.store!.recordRoot(root)
    ctx.supervisor.start()
    ctx.tray && ctx.tray.setRoot(root)
    const payload = { root, capabilities, status, gitRepo: locator.isGitRepo(root) }
    ctx.send('vouch:kb', payload)
    return payload as KbPayload
  },

  async initKb(root: string) {
    root = locator.normalizeKbRoot(root)!
    if (!ctx.launcher && !ctx.resolveLauncher()) {
      const e: VouchErr = new Error('could not find a runnable `vouch`'); e.code = 'no_vouch'; throw e
    }
    const r = await locator.initKb(ctx.launcher!, root)
    if (!r.ok) { const e: VouchErr = new Error(r.error || 'vouch init failed'); e.code = 'init_failed'; throw e }
    return ctx.openKb(root)
  },

  dualSolvePreconditions() {
    const caps = ctx.state.capabilities || {}
    const transports = ((caps as { transports?: string[] }).transports || []).join(' ')
    return {
      gitRepo: ctx.state.root ? locator.isGitRepo(ctx.state.root) : false,
      tools: locator.dualSolveTools({ sandbox: true }),
      vouchSupportsSandbox: !!(ctx.launcher && ctx.launcher.supportsDualSolveSandbox),
      // review-ui needs the [web] extra; we can only be sure by trying to spawn,
      // but surface the capability hint when present.
      webHint: /review-ui|http/.test(transports),
    }
  },

  async ensureHttp() {
    if (!ctx.state.root) { const e: VouchErr = new Error('no knowledge base is open'); e.code = 'no_kb'; throw e }
    if (!ctx.launcher || !ctx.launcher.supportsDualSolveSandbox) {
      const e: VouchErr = new Error('connected vouch does not support sandboxed dual-solve; point vouch-desktop at a vouch build with --dual-solve-sandbox')
      e.code = 'no_dual_solve_sandbox'
      throw e
    }
    if (!ctx.http) {
      ctx.http = new HttpClient(ctx.launcher, {
        root: ctx.state.root,
        env: { VOUCH_AGENT: locator.defaultAgent() },
        sandboxDualSolve: true,
      })
      ctx.http.on('stderr', (l: string) => appendLog(ctx, 'review-ui', l))
      ctx.http.on('dual_solve', (f: unknown) => {
        ctx.send('vouch:progress', f)
        ctx.notifier && ctx.notifier.onDualSolve(f as { event: string; message?: string })
      })
      ctx.http.on('refresh', (f: unknown) => ctx.send('vouch:ws', f))
      ctx.http.on('exit', () => ctx.send('vouch:proc', { which: 'http', state: 'down' }))
    }
    return ctx.http.ensure({ allowDualSolve: true })
  },

  requireHttp() {
    if (!ctx.http || !ctx.http.up) { const e: VouchErr = new Error('dual-solve server is not running'); e.code = 'no_http'; throw e }
    return ctx.http
  },

  async teardownChildren() {
    if (ctx.supervisor) { ctx.supervisor.stop(); ctx.supervisor = null }
    if (ctx.http) { await ctx.http.stop().catch(() => {}); ctx.http = null }
    if (ctx.jsonl) { await ctx.jsonl.stop().catch(() => {}); ctx.jsonl = null }
  },
}

function appendLog(c: AppCtx, tag: string, line: string): void {
  if (!c.logDir) return
  try { fs.appendFileSync(join(c.logDir, 'vouch-desktop.log'), `[${tag}] ${line}\n`) } catch { /* noop */ }
}

function createWindow(): BrowserWindow {
  const bounds = ctx.store!.get('window') || { width: 1280, height: 860 }
  const win = new BrowserWindow({
    width: (bounds as { width: number; height: number }).width,
    height: (bounds as { width: number; height: number }).height,
    minWidth: 940,
    minHeight: 640,
    title: 'vouch',
    backgroundColor: '#15120d',
    webPreferences: {
      preload: join(__dirname, '../preload/index.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  })
  win.webContents.on('console-message', (_e, level, message, line, sourceId) => {
    if (level >= 2) appendLog(ctx, 'renderer', `${message} (${sourceId}:${line})`)
    if (process.env.VOUCH_DEBUG) console.log(`[renderer:${level}] ${message}`)
  })
  win.webContents.on('render-process-gone', (_e, d) => console.error('[renderer gone]', d && d.reason))
  win.webContents.on('did-fail-load', (_e, code, desc) => console.error('[did-fail-load]', code, desc))
  // electron-vite injects ELECTRON_RENDERER_URL in dev; loadFile the build in prod.
  if (process.env.ELECTRON_RENDERER_URL) win.loadURL(process.env.ELECTRON_RENDERER_URL)
  else win.loadFile(join(__dirname, '../renderer/index.html'))
  win.on('close', (e) => {
    ctx.store!.set('window', { width: win.getBounds().width, height: win.getBounds().height })
    if (ctx.store!.prefs.closeToTray && !app.isQuitting) {
      e.preventDefault()
      win.hide()
    }
  })
  return win
}

const gotLock = app.requestSingleInstanceLock()
if (!gotLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (ctx.win) { if (ctx.win.isMinimized()) ctx.win.restore(); ctx.win.show(); ctx.win.focus() }
  })

  app.whenReady().then(async () => {
    ctx.logDir = join(app.getPath('userData'), 'logs')
    try { fs.mkdirSync(ctx.logDir, { recursive: true }) } catch { /* noop */ }
    ctx.store = new KbStore(app.getPath('userData'))
    ctx.resolveLauncher()
    registerIpc(ctx)
    ctx.win = createWindow()
    ctx.notifier = new Notifier(ctx)
    ctx.tray = installTray(ctx)

    // smoke mode: open a KB, screenshot, report renderer errors, quit.
    if (process.env.VOUCH_DESKTOP_SMOKE) {
      const errors: string[] = []
      ctx.win.webContents.on('console-message', (_e, level, message) => { if (level >= 2) errors.push(message) })
      ctx.win.webContents.once('did-finish-load', async () => {
        const kb = process.env.VOUCH_SMOKE_KB
        if (kb) { try { await ctx.openKb(kb) } catch (e) { console.error('SMOKE openKb failed:', (e as Error).message) } }
        const wc = ctx.win!.webContents
        const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))
        await wait(1200)
        // click through every nav item to exercise each view's initial render
        const n = await wc.executeJavaScript("document.querySelectorAll('.nav-item').length") as number
        for (let i = 0; i < n; i++) {
          const label = await wc.executeJavaScript(`(function(){const b=document.querySelectorAll('.nav-item')[${i}];b.click();return b.innerText.trim();})()`);
          await wait(700)
          const shotName = (process.env.VOUCH_SMOKE_SHOT || '/tmp/vouch-shot.png').replace(/\.png$/, `-${i}-${String(label).split('\n')[0].replace(/\W+/g, '')}.png`)
          try { fs.writeFileSync(shotName, (await wc.capturePage()).toPNG()) } catch { /* noop */ }
        }
        console.log('SMOKE views-visited:', n)
        console.log('SMOKE renderer-errors:', errors.length, JSON.stringify(errors.slice(0, 25)))
        app.isQuitting = true; app.quit()
      })
      return
    }

    // Dev scripts can pin a KB so vouch starts as soon as the window loads.
    // Otherwise, auto-open the last KB if configured and still valid.
    const startupRoot = process.env.VOUCH_DESKTOP_KB
      || (ctx.store!.prefs.autoOpenLast ? ctx.store!.lastRoot : null)
    if (startupRoot) {
      ctx.win.webContents.once('did-finish-load', () => {
        if (!locator.hasKb(startupRoot)) {
          ctx.send('vouch:kb', { error: `no .vouch/ found in ${startupRoot}`, code: 'no_kb_here' })
          return
        }
        ctx.openKb(startupRoot).catch((err: VouchErr) => ctx.send('vouch:kb', { error: err.message, code: err.code }))
      })
    }
  })

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) ctx.win = createWindow()
    else if (ctx.win) ctx.win.show()
  })

  app.on('before-quit', async (e) => {
    app.isQuitting = true
    if (ctx.jsonl || ctx.http) {
      e.preventDefault()
      await ctx.teardownChildren()
      app.exit(0)
    }
  })

  // we manage our own lifecycle (tray); don't quit on all-windows-closed
  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin' && !ctx.store?.prefs.closeToTray) app.quit()
  })
}

export { ctx }
