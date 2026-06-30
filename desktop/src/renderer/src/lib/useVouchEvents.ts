// useVouchEvents.ts — subscribes to all vouch push-events on mount and
// unsubscribes on unmount.  Mirrors wireEvents() in src/renderer/app.js:403-420.
import { useEffect, useRef } from 'react'
import type { KbError, KbPayload, TrayFrame } from '../../../shared/ipc'
import * as api from './client'
import { useVouch } from './VouchContext'

export interface VouchEventHandlers {
  /** Called when the tray sends action "pickKb" */
  pickKb?: () => void
  /** Called when the tray sends action "view" */
  navigate?: (view: string) => void
}

/** Returns true when the vouch:kb payload represents an error rather than a
 *  successful KB open.  KbError carries an `error` string field; KbPayload
 *  carries `root`. */
function isKbError(p: KbPayload | KbError): p is KbError {
  return 'error' in p && typeof (p as KbError).error === 'string'
}

/**
 * Subscribes to push events from the vouch backend on mount and tears down
 * all subscriptions on unmount.  Must be called inside a <VouchProvider>.
 *
 * @param handlers  Optional overrides for tray-driven navigation.  These are
 *                  captured via a ref so callers may pass inline objects without
 *                  causing stale-closure bugs — the latest value is always used.
 */
export function useVouchEvents(handlers?: VouchEventHandlers): void {
  const { dispatch, actions } = useVouch()

  // Keep handlers in a ref so the stable useEffect closure always reads the
  // latest value even when the caller passes a non-memoized object.
  const handlersRef = useRef<VouchEventHandlers | undefined>(handlers)
  useEffect(() => {
    handlersRef.current = handlers
  })

  // Capture the stable action callbacks via refs.  All five actions are
  // useCallback([]) inside VouchContext, so their identities are stable across
  // renders.  Using refs makes the dependency explicit and keeps this hook
  // correct even if a future edit widens the callback deps.
  const navigateRef = useRef(actions.navigate)
  const pickKbRef = useRef(actions.pickKb)
  useEffect(() => {
    navigateRef.current = actions.navigate
    pickKbRef.current = actions.pickKb
  })

  useEffect(() => {
    const unsubs: Array<() => void> = []

    // vouch:kb — a KB was opened (or failed to open)
    unsubs.push(
      api.on('vouch:kb', (p) => {
        if (isKbError(p)) {
          dispatch({ type: 'KB_ERROR', error: p.error })
          return
        }
        dispatch({ type: 'KB_SET', payload: p })
        // Mirror app.js:410: if the KB payload carries a status field the proc
        // is already up — immediately set jsonl=true so the StatusBar shows
        // 'vouch running' without waiting up to 8 s for the next health push.
        if (p.status) {
          dispatch({ type: 'HEALTH', payload: { jsonl: true } })
        }
      }),
    )

    // vouch:health — JSONL process health + pending count + optional error
    unsubs.push(
      api.on('vouch:health', (h) => {
        dispatch({ type: 'HEALTH', payload: h })
      }),
    )

    // vouch:proc — process lifecycle; surface an error when the process goes down
    unsubs.push(
      api.on('vouch:proc', (p) => {
        if (p && p.state === 'down') {
          dispatch({
            type: 'HEALTH',
            payload: { jsonl: false, error: 'vouch process down' },
          })
        }
      }),
    )

    // vouch:tray — tray menu actions (open KB picker or navigate to a view)
    unsubs.push(
      api.on('vouch:tray', (t: TrayFrame) => {
        if (!t) return
        if (t.action === 'pickKb') {
          const override = handlersRef.current?.pickKb
          if (override) {
            override()
          } else {
            void pickKbRef.current()
          }
        } else if (t.action === 'view' && t.view) {
          const override = handlersRef.current?.navigate
          if (override) {
            override(t.view)
          } else {
            navigateRef.current(t.view)
          }
        }
      }),
    )

    return () => {
      unsubs.forEach((u) => u())
    }
    // dispatch is stable from useReducer.  handlers/actions are accessed via
    // refs that are kept current by the layout effects above, so re-subscribing
    // on every render is unnecessary.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch])
}
