// VouchContext.tsx — global app state (root, caps, view, pending, jsonl) via useReducer.
// All vouch I/O goes through ./client (the preload bridge wrapper).
import {
  createContext,
  useCallback,
  useContext,
  useReducer,
  useRef,
  type Dispatch,
  type ReactNode,
} from 'react'
import type { Capabilities, KbPayload, KbError, HealthPayload } from '../../../shared/ipc'
import * as api from './client'

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------
export interface VouchState {
  root: string | null
  caps: Capabilities | null
  capMethods: Set<string> | null
  gitRepo: boolean
  view: string
  pending: number | null
  /** true when the vouch JSONL connection is up; drives proc up/down status dot */
  jsonl: boolean
  /** last KB-open error message, if any */
  kbError: string | null
  /** health/proc error string for the status bar (e.g. 'poll failed: …' or 'vouch process down') */
  statusError: string | null
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
export type Action =
  | { type: 'KB_SET'; payload: KbPayload }
  | { type: 'KB_ERROR'; error: string }
  | { type: 'NAVIGATE'; view: string }
  | { type: 'PENDING'; pending: number | null }
  | { type: 'HEALTH'; payload: HealthPayload }

// ---------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------
const initial: VouchState = {
  root: null,
  caps: null,
  capMethods: null,
  gitRepo: false,
  view: 'dashboard',
  pending: null,
  jsonl: true,
  kbError: null,
  statusError: null,
}

export function reducer(s: VouchState, a: Action): VouchState {
  switch (a.type) {
    case 'KB_SET': {
      const caps = a.payload.capabilities
      return {
        ...s,
        root: a.payload.root,
        caps,
        capMethods: caps?.methods ? new Set(caps.methods) : null,
        gitRepo: !!a.payload.gitRepo,
        kbError: null,
      }
    }
    case 'KB_ERROR':
      return { ...s, kbError: a.error }
    case 'NAVIGATE':
      return { ...s, view: a.view }
    case 'PENDING':
      return { ...s, pending: a.pending }
    case 'HEALTH':
      return {
        ...s,
        jsonl: a.payload.jsonl,
        pending: a.payload.pending !== undefined ? (a.payload.pending ?? null) : s.pending,
        statusError: a.payload.error ?? null,
      }
    default:
      return s
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
/** Returns true when the connected vouch advertises the method, or when no
 *  capability advertisement is available yet (assume available). */
export function isAvailable(s: VouchState, name: string): boolean {
  return !s.capMethods || s.capMethods.has(name)
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------
interface Actions {
  navigate: (view: string) => void
  openKb: (root: string) => Promise<void>
  initKb: (root: string) => Promise<void>
  pickKb: () => Promise<void>
  refreshPending: () => void
}

interface Ctx {
  state: VouchState
  dispatch: Dispatch<Action>
  actions: Actions
}

const VouchCtx = createContext<Ctx | null>(null)

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------
export function VouchProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initial)

  // Stable ref for the debounce timer — persists across renders
  const badgeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const navigate = useCallback((v: string): void => {
    dispatch({ type: 'NAVIGATE', view: v })
  }, [])

  // openKb / initKb just call the API; the vouch:kb push event (handled by
  // useVouchEvents) dispatches KB_SET / KB_ERROR and paints the new state.
  const openKb = useCallback(async (root: string): Promise<void> => {
    await api.openKb(root)
  }, [])

  const initKb = useCallback(async (root: string): Promise<void> => {
    await api.initKb(root)
  }, [])

  const pickKb = useCallback(async (): Promise<void> => {
    const dir = await api.pickKb()
    if (dir) await api.openKb(dir)
  }, [])

  // Debounced refresh — mirrors refreshPendingBadge in app.js
  const refreshPending = useCallback((): void => {
    if (badgeTimer.current) clearTimeout(badgeTimer.current)
    badgeTimer.current = setTimeout(async () => {
      try {
        const items = await api.call<unknown[]>('kb.list_pending', {})
        dispatch({ type: 'PENDING', pending: items.length })
      } catch {
        // noop — older vouch may not support list_pending
      }
    }, 300)
  }, [])

  const actions: Actions = { navigate, openKb, initKb, pickKb, refreshPending }

  return (
    <VouchCtx.Provider value={{ state, dispatch, actions }}>
      {children}
    </VouchCtx.Provider>
  )
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------
export function useVouch(): Ctx {
  const c = useContext(VouchCtx)
  if (!c) throw new Error('useVouch must be used inside <VouchProvider>')
  return c
}

// Exported for unit tests only
export { reducer as _reducer }
