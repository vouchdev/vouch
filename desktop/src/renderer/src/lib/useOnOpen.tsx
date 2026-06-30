// useOnOpen.tsx — returns an OnOpen callback that opens a claim/page/entity/relation
// in the Drawer via the READ map.
//
// Faithful port of onOpen()/openNeighbors() in src/renderer/app.js:285-314.
//
// Handles:
//   '__view'  → navigate to the view id
//   '__neighbors' → navigate to graph + load neighbors into drawer
//   'node'    → try claim first, fall back to entity
//   others    → READ map lookup → kb.read_* → card in drawer

import { useCallback, useRef, useLayoutEffect } from 'react'
import type { ReactNode } from 'react'
import * as api from './client'
import { useVouch, isAvailable } from './VouchContext'
import type { OnOpen } from '../components/MethodCard'
import { card } from '../components/results/Cards'
import { ResultView } from '../components/results/ResultView'

// The READ map mirrors app.js:285-286
const READ: Record<string, [string, string]> = {
  claim:    ['kb.read_claim',    'claim_id'],
  page:     ['kb.read_page',     'page_id'],
  entity:   ['kb.read_entity',   'entity_id'],
  relation: ['kb.read_relation', 'relation_id'],
}

interface UseOnOpenOpts {
  openDrawer: (content: ReactNode) => void
}

export function useOnOpen({ openDrawer }: UseOnOpenOpts): OnOpen {
  const { state, actions } = useVouch()

  // Stable refs so the useCallback below can capture the latest values without
  // being invalidated on every render.  openDrawer is a plain inline function
  // in Shell (recreated each render); actions is a plain object literal
  // (recreated each render even though its members are stable useCallbacks).
  // Capturing them via refs lets useCallback keep a stable identity.
  const openDrawerRef = useRef(openDrawer)
  const navigateRef = useRef(actions.navigate)
  const stateRef = useRef(state)

  useLayoutEffect(() => {
    openDrawerRef.current = openDrawer
  })
  useLayoutEffect(() => {
    navigateRef.current = actions.navigate
  })
  useLayoutEffect(() => {
    stateRef.current = state
  })

  // Use a ref so the callback can reference the latest version of itself
  // without stale closures (needed for recursive drawer-card calls)
  const onOpenRef = useRef<OnOpen>(() => {})

  // deps [] — all mutable values accessed through refs above; the callback
  // identity is now stable across renders.
  const onOpen: OnOpen = useCallback(
    async (kind: string, id: string) => {
      if (!id) return

      if (kind === '__view') {
        navigateRef.current(id)
        return
      }

      if (kind === '__neighbors') {
        await openNeighbors(id, openDrawerRef, navigateRef, stateRef, onOpenRef)
        return
      }

      let k = kind
      if (k === 'node') k = 'claim' // best guess for ambiguous node ids

      const spec = READ[k]
      if (!spec) {
        openDrawerRef.current(
          <div>
            <h2>{kind}</h2>
            <code>{id}</code>
            <p className="muted">no detail view for this kind</p>
          </div>,
        )
        return
      }

      openDrawerRef.current(<div className="muted">loading…</div>)

      try {
        const obj = await api.call<Record<string, unknown>>(spec[0], { [spec[1]]: id })
        openDrawerRef.current(<div>{card(k, obj, onOpenRef.current)}</div>)
      } catch (e: unknown) {
        // Fall back to entity if node guess (claim) was wrong
        if (kind === 'node' && k === 'claim') {
          try {
            const obj = await api.call<Record<string, unknown>>('kb.read_entity', { entity_id: id })
            openDrawerRef.current(card('entity', obj, onOpenRef.current))
            return
          } catch {
            // noop
          }
        }
        const err = e as Error & { code?: string; traceback?: string }
        openDrawerRef.current(
          <div className="errbox">
            <span className="err-code">{err.code ?? 'error'}</span>
            <span>{err.message ?? String(e)}</span>
            {err.traceback && (
              <details className="err-tb">
                <summary>traceback</summary>
                <pre>{err.traceback}</pre>
              </details>
            )}
          </div>,
        )
      }
    },
    [],
  )

  // Keep the ref in sync with the latest callback
  useLayoutEffect(() => {
    onOpenRef.current = onOpen
  })

  return onOpen
}

// openNeighbors mirrors app.js:308-314
async function openNeighbors(
  id: string,
  openDrawerRef: React.MutableRefObject<(content: ReactNode) => void>,
  navigateRef: React.MutableRefObject<(v: string) => void>,
  stateRef: React.MutableRefObject<import('./VouchContext').VouchState>,
  onOpenRef: React.MutableRefObject<OnOpen>,
): Promise<void> {
  navigateRef.current('graph')
  // app.js:311: silently return when kb.neighbors is not advertised
  if (!isAvailable(stateRef.current, 'kb.neighbors')) return
  try {
    const res = await api.call('kb.neighbors', { node_id: id })
    openDrawerRef.current(
      <div>
        <h2>neighbors</h2>
        <ResultView result={res} onOpen={onOpenRef.current} />
      </div>,
    )
  } catch (e: unknown) {
    const err = e as Error & { code?: string; traceback?: string }
    openDrawerRef.current(
      <div className="errbox">
        <span className="err-code">{err.code ?? 'error'}</span>
        <span>{err.message ?? String(e)}</span>
      </div>,
    )
  }
}
