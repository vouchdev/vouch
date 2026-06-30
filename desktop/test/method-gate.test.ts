// method-gate.test.ts — covers the vouch:call method-name gate in src/main/ipc.ts.
// Runs in the Node environment (see vitest.config.ts environmentMatchGlobs).
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { methods } from '../src/shared/methods.gen'
import type { AppCtx } from '../src/main/types'

// ---------------------------------------------------------------------------
// Part 1 — catalog guard: the generated Set must be non-empty and contain the
// two sentinel names that the gate relies on.
// ---------------------------------------------------------------------------
describe('methods catalog', () => {
  const methodNames = new Set(methods.map((m) => m.name))

  it('is non-empty', () => {
    expect(methodNames.size).toBeGreaterThan(0)
  })

  it('contains kb.status', () => {
    expect(methodNames.has('kb.status')).toBe(true)
  })

  it('contains kb.search', () => {
    expect(methodNames.has('kb.search')).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Part 2 — vouch:call gate in registerIpc.
// We mock electron so ipc.ts can be imported without a real Electron runtime.
// ---------------------------------------------------------------------------

type Handler = (event: unknown, arg: unknown) => Promise<unknown>

let capturedHandlers: Map<string, Handler>

vi.mock('electron', () => {
  capturedHandlers = new Map()
  return {
    ipcMain: {
      handle: (channel: string, fn: Handler) => {
        capturedHandlers.set(channel, fn)
      },
    },
    dialog: {
      showOpenDialog: vi.fn(),
      showSaveDialog: vi.fn(),
    },
    shell: {
      openPath: vi.fn(),
    },
  }
})

describe('registerIpc vouch:call gate', () => {
  let ctx: AppCtx

  beforeEach(async () => {
    // Reset handlers map before each test
    capturedHandlers = new Map()

    const jsonlCall = vi.fn(async () => ({ ok: true }))
    ctx = {
      methodNames: new Set(methods.map((m) => m.name)),
      jsonl: { alive: true, call: jsonlCall, pid: 1, stderrTail: [] } as unknown as AppCtx['jsonl'],
      store: { get: vi.fn(() => []), prefs: {}, setPref: vi.fn() } as unknown as AppCtx['store'],
      win: null,
      tray: null,
      notifier: null,
      launcher: null,
      http: null,
      supervisor: null,
      state: { root: null, capabilities: null },
      logDir: null,
      getWindow: () => null,
      send: vi.fn(),
      resolveLauncher: vi.fn(),
      openKb: vi.fn(),
      initKb: vi.fn(),
      dualSolvePreconditions: vi.fn(),
      ensureHttp: vi.fn(),
      requireHttp: vi.fn(),
      teardownChildren: vi.fn(),
    } as unknown as AppCtx

    const { registerIpc } = await import('../src/main/ipc')
    registerIpc(ctx)
  })

  it('rejects an unknown method and does not call jsonl', async () => {
    const handler = capturedHandlers.get('vouch:call')!
    expect(handler).toBeDefined()

    const result = (await handler(null, { method: 'kb.nope', params: {} })) as {
      ok: boolean
      error?: { message: string }
    }

    expect(result.ok).toBe(false)
    expect(result.error?.message).toMatch(/unknown method/)
    expect((ctx.jsonl as { call: ReturnType<typeof vi.fn> }).call).not.toHaveBeenCalled()
  })

  it('delegates to jsonl.call for a known method', async () => {
    const handler = capturedHandlers.get('vouch:call')!
    expect(handler).toBeDefined()

    const result = (await handler(null, { method: 'kb.status', params: {} })) as {
      ok: boolean
      result?: unknown
    }

    expect((ctx.jsonl as { call: ReturnType<typeof vi.fn> }).call).toHaveBeenCalledWith(
      'kb.status',
      {},
    )
    expect(result.ok).toBe(true)
  })
})
