/*
 * tray.ts — the always-available companion. A Tray icon with the pending-count
 * badge, a KB switcher, dual-solve/review shortcuts and process indicators, plus
 * a Notifier that raises OS notifications for the events that matter while the
 * window is hidden. Notifications are opt-in per category (kb-store prefs).
 */
import { Tray, Menu, Notification, nativeImage, app, shell } from 'electron'
import * as path from 'node:path'
import * as fs from 'node:fs'
import type { BrowserWindow } from 'electron'
import type { Prefs } from '../shared/ipc'

// Inline structural shape of the ctx fields tray.ts actually uses.
// AppCtx is defined in ./types (Task 1.9); importing it here would create
// a circular dependency (types.ts imports TrayHandle from ./tray).
interface TrayCtx {
  store: {
    get(key: 'recentRoots'): Array<{ root: string }> | null | undefined
    prefs: Prefs
  } | null
  win: BrowserWindow | null
  jsonl: { alive: boolean } | null
  logDir: string | null
  send(channel: string, payload: unknown): void
  openKb(root: string): Promise<unknown>
}

// app.isQuitting is a runtime convention used across the codebase to signal
// an intentional quit so the close handler does not intercept it.
declare global {
  namespace Electron {
    interface App {
      isQuitting?: boolean
    }
  }
}

export interface TrayHandle {
  setRoot(root: string): void
  setPending(n: number | null | undefined): void
  destroy(): void
}

function trayIcon(): Electron.NativeImage {
  // ship a real icon at build/iconTemplate.png; fall back to a 1px transparent
  // image so the tray still installs in dev without art assets.
  const p = path.join(__dirname, '..', '..', 'build', 'iconTemplate.png')
  if (fs.existsSync(p)) {
    const img = nativeImage.createFromPath(p)
    if (process.platform === 'darwin') img.setTemplateImage(true)
    return img
  }
  return nativeImage.createFromDataURL(
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=',
  )
}

export function installTray(ctx: TrayCtx): TrayHandle | null {
  let tray: Tray
  try {
    tray = new Tray(trayIcon())
  } catch {
    return null
  }
  const state: { root: string | null; pending: number | null } = { root: null, pending: null }

  function rebuild(): void {
    const recent = (ctx.store!.get('recentRoots') || []).slice(0, 8)
    const menu = Menu.buildFromTemplate([
      { label: state.root ? `KB: ${path.basename(state.root)}` : 'No KB open', enabled: false },
      state.pending != null
        ? { label: `${state.pending} pending review`, enabled: false }
        : { label: '—', visible: false },
      { type: 'separator' },
      {
        label: 'Open knowledge base…',
        click: () => {
          show()
          ctx.send('vouch:tray', { action: 'pickKb' })
        },
      },
      recent.length
        ? {
            label: 'Recent',
            submenu: recent.map((r) => ({
              label: path.basename(r.root),
              click: () => ctx.openKb(r.root).catch(() => {}),
            })),
          }
        : { label: 'Recent', enabled: false },
      { type: 'separator' },
      { label: 'Show window', click: show },
      {
        label: 'Review queue',
        click: () => {
          show()
          ctx.send('vouch:tray', { action: 'view', view: 'review' })
        },
      },
      {
        label: 'Run dual-solve…',
        click: () => {
          show()
          ctx.send('vouch:tray', { action: 'view', view: 'dual-solve' })
        },
      },
      { type: 'separator' },
      {
        label: ctx.jsonl && ctx.jsonl.alive ? 'vouch: running' : 'vouch: stopped',
        enabled: false,
      },
      {
        label: 'Open logs folder',
        click: () => {
          if (ctx.logDir) shell.openPath(ctx.logDir)
        },
      },
      { type: 'separator' },
      {
        label: 'Quit vouch',
        click: () => {
          app.isQuitting = true
          app.quit()
        },
      },
    ])
    tray.setContextMenu(menu)
    tray.setToolTip(
      state.root
        ? `vouch — ${path.basename(state.root)}` +
            (state.pending != null ? ` (${state.pending} pending)` : '')
        : 'vouch',
    )
  }

  function show(): void {
    if (!ctx.win) return
    if (ctx.win.isMinimized()) ctx.win.restore()
    ctx.win.show()
    ctx.win.focus()
  }

  tray.on('click', show)
  rebuild()

  return {
    setRoot(root: string): void {
      state.root = root
      rebuild()
    },
    setPending(n: number | null | undefined): void {
      const coerced = n ?? null
      if (coerced === state.pending) return
      state.pending = coerced
      if (process.platform === 'darwin' && app.dock) app.dock.setBadge(coerced ? String(coerced) : '')
      rebuild()
    },
    destroy(): void {
      try {
        tray.destroy()
      } catch {
        /* noop */
      }
    },
  }
}

export class Notifier {
  private ctx: TrayCtx
  private _lastPending: number | null

  constructor(ctx: TrayCtx) {
    this.ctx = ctx
    this._lastPending = null
  }

  private _enabled(key: keyof Prefs): boolean {
    return Notification.isSupported() && !!this.ctx.store?.prefs[key]
  }

  private _notify(title: string, body: string, onClick?: () => void): void {
    const n = new Notification({ title, body, silent: false })
    if (onClick) n.on('click', onClick)
    n.show()
  }

  private _focus(view?: string): void {
    if (this.ctx.win) {
      this.ctx.win.show()
      this.ctx.win.focus()
    }
    if (view) this.ctx.send('vouch:tray', { action: 'view', view })
  }

  onDualSolve(f: { event: string; message?: string }): void {
    if (f.event === 'ready' && this._enabled('notifyDualSolveReady')) {
      this._notify('Dual-solve ready', 'Two candidates are ready — pick a winner.', () =>
        this._focus('dual-solve'),
      )
    } else if (f.event === 'error' && this._enabled('notifyDualSolveReady')) {
      this._notify('Dual-solve failed', f.message || 'the run errored.', () =>
        this._focus('dual-solve'),
      )
    }
  }

  onHealth(hp: { pending?: number | null } | null): void {
    const n = hp && hp.pending
    if (typeof n === 'number') {
      const focused = this.ctx.win && this.ctx.win.isFocused()
      if (
        this._lastPending != null &&
        n > this._lastPending &&
        !focused &&
        this._enabled('notifyNewPending')
      ) {
        const delta = n - this._lastPending
        this._notify(
          `${delta} new proposal${delta > 1 ? 's' : ''}`,
          'Awaiting review in the queue.',
          () => this._focus('review'),
        )
      }
      this._lastPending = n
    }
  }

  onProc(p: { state: string; error?: string }): void {
    if (p.state === 'failed' && this._enabled('notifyProcessDown')) {
      this._notify(
        'vouch stopped responding',
        p.error || 'the vouch process needs attention.',
        () => this._focus(),
      )
    }
  }
}
