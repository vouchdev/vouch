/*
 * preload/index.ts — the trust boundary. Runs with contextIsolation + sandbox;
 * exposes exactly one frozen `window.vouch` object. The renderer never touches
 * node, the filesystem, or processes — every capability is an explicit IPC call.
 */
import { contextBridge, ipcRenderer } from 'electron'
import type { VouchApi, PushEvent, PushPayloads, PickFileOpts, Prefs } from '../shared/ipc'

const ALLOWED_EVENTS = new Set<string>([
  'vouch:health',   // {jsonl, pending, ...}
  'vouch:proc',     // {which, state, restarts, error?}
  'vouch:progress', // dual-solve frames {job_id, event, message}
  'vouch:ws',       // review refresh signal {view, ...}
  'vouch:kb',       // active KB changed {root, capabilities, status}
  'vouch:tray',     // tray-driven navigation {action, view?}
])

contextBridge.exposeInMainWorld('vouch', {
  // the universal JSONL bridge — any of the 54 kb.* methods
  call: (method: string, params?: Record<string, unknown>) =>
    ipcRenderer.invoke('vouch:call', { method, params }),

  // KB lifecycle / discovery
  openKb: (root: string) => ipcRenderer.invoke('vouch:openKb', { root }),
  pickKb: () => ipcRenderer.invoke('vouch:pickKb'),
  initKb: (root: string) => ipcRenderer.invoke('vouch:initKb', { root }),
  recentRoots: () => ipcRenderer.invoke('vouch:recentRoots'),
  capabilities: () => ipcRenderer.invoke('vouch:capabilities'),
  status: () => ipcRenderer.invoke('vouch:status'),
  health: () => ipcRenderer.invoke('vouch:health'),

  // prefs
  getPrefs: () => ipcRenderer.invoke('vouch:getPrefs'),
  setPref: (key: keyof Prefs, value: boolean) =>
    ipcRenderer.invoke('vouch:setPref', { key, value }),

  // native pickers (for path/out_path/bundle_path/queries_path params)
  pickFile: (opts?: PickFileOpts) =>
    ipcRenderer.invoke('vouch:pickFile', opts || {}),
  pickSave: (opts?: PickFileOpts) =>
    ipcRenderer.invoke('vouch:pickSave', opts || {}),

  // dual-solve (HTTP child, lazily spawned)
  ds: {
    preconditions: () => ipcRenderer.invoke('vouch:ds:pre'),
    ensure: () => ipcRenderer.invoke('vouch:ds:ensure'),
    run: (body: Record<string, unknown>) => ipcRenderer.invoke('vouch:ds:run', body),
    job: (jobId: string) => ipcRenderer.invoke('vouch:ds:job', { jobId }),
    choose: (body: Record<string, unknown>) => ipcRenderer.invoke('vouch:ds:choose', body),
  },

  // diagnostics
  openLogs: () => ipcRenderer.invoke('vouch:openLogs'),
  procInfo: () => ipcRenderer.invoke('vouch:procInfo'),

  // push channels (main -> renderer); returns an unsubscribe fn
  on: <E extends PushEvent>(event: E, cb: (payload: PushPayloads[E]) => void) => {
    if (!ALLOWED_EVENTS.has(event)) throw new Error('unknown event: ' + event)
    const handler = (_e: Electron.IpcRendererEvent, payload: PushPayloads[E]) => cb(payload)
    ipcRenderer.on(event, handler)
    return () => ipcRenderer.removeListener(event, handler)
  },
} satisfies VouchApi)
