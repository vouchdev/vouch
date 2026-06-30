// MethodCard.tsx — renders a single method with its form, Run button, and result area.
//
// Ported from src/renderer/app.js:152-187 (methodCard).
// On Run: collect() → spinner → api.call() → render result → refreshPending on mutates.

import { useRef, useState } from 'react'
import type { Method } from '../../../shared/methods.types'
import { useVouch, isAvailable } from '../lib/VouchContext'
import { MethodForm, type MethodFormHandle } from './MethodForm'
import type { FormCtx } from './controls/index'
import * as api from '../lib/client'
import { ResultView } from './results/ResultView'

// ---------------------------------------------------------------------------
// toErrLike — safe conversion from unknown catch value to ErrLike
// ---------------------------------------------------------------------------
function toErrLike(e: unknown): ErrLike {
  if (e instanceof Error) {
    const ve = e as Error & { code?: string; traceback?: string }
    return { message: ve.message, code: ve.code, traceback: ve.traceback }
  }
  return { message: String(e) }
}

// ---------------------------------------------------------------------------
// OnOpen type — callback to open a detail view for a KB object
// ---------------------------------------------------------------------------
export type OnOpen = (kind: string, id: string) => void

// ---------------------------------------------------------------------------
// ErrorBox — mirrors errorBox() in app.js:373-375
// ---------------------------------------------------------------------------
interface ErrLike {
  message?: string
  code?: string
  traceback?: string
}

function ErrorBox({ err }: { err: ErrLike }) {
  return (
    <div className="errbox">
      <span className="err-code">{err.code ?? 'error'}</span>
      <span>{err.message ?? String(err)}</span>
      {err.traceback && (
        <details className="err-tb">
          <summary>traceback</summary>
          <pre>{err.traceback}</pre>
        </details>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Build the FormCtx — mirrors formCtx in app.js:36-45
// ---------------------------------------------------------------------------
async function search(query: string, kind?: string): Promise<Array<{ id: string; kind?: string; snippet?: string }>> {
  try {
    const r = await api.call<{ hits?: Array<{ id: string; kind?: string; snippet?: string }> }>(
      'kb.search',
      { query, limit: 12 },
    )
    const hits = (r && r.hits) || []
    const readable = new Set(['claim', 'page', 'entity', 'relation', 'source'])
    return kind && readable.has(kind) ? hits.filter((h) => h.kind === kind) : hits
  } catch {
    return []
  }
}

const formCtx: FormCtx = {
  search,
  pickFile: api.pickFile,
  pickSave: api.pickSave,
}

// ---------------------------------------------------------------------------
// MethodCard
// ---------------------------------------------------------------------------
interface MethodCardProps {
  method: Method
  onOpen: OnOpen
}

export default function MethodCard({ method, onOpen }: MethodCardProps) {
  const { state, actions } = useVouch()
  const avail = isAvailable(state, method.name)

  const formRef = useRef<MethodFormHandle>(null)

  // running: spinner is visible; result: the api response; err: caught error
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<unknown>(undefined)
  const [error, setError] = useState<ErrLike | null>(null)
  const [hasResult, setHasResult] = useState(false)

  async function handleRun() {
    if (!formRef.current) return

    let params: Record<string, unknown>
    try {
      params = formRef.current.collect()
    } catch (e) {
      setError(toErrLike(e))
      setResult(undefined)
      setHasResult(false)
      return
    }

    setRunning(true)
    setError(null)
    setResult(undefined)
    setHasResult(false)

    try {
      const res = await api.call(method.name, params)
      setResult(res)
      setHasResult(true)
      if (method.mutates) actions.refreshPending()
    } catch (e) {
      setError(toErrLike(e))
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className={`method-card${avail ? '' : ' unavail'}`}>
      <div className="mc-head">
        <code className="mc-name">{method.name}</code>
        {method.gated && <span className="tag gate">review-gated</span>}
        {method.mutates && <span className="tag mut">writes</span>}
        {method.longRunning && <span className="tag">may be slow</span>}
        <span className="spacer" />
        {!avail && (
          <span
            className="tag warn"
            title="not advertised by the connected vouch version"
          >
            unavailable
          </span>
        )}
      </div>

      <p className="mc-summary">{method.summary ?? ''}</p>

      <MethodForm method={method} ctx={formCtx} ref={formRef} />

      <div className="mc-actions">
        <button
          className="btn"
          disabled={!avail || running}
          onClick={() => void handleRun()}
        >
          {running ? 'running…' : 'Run'}
        </button>
        {method.returns && (
          <span className="muted small returns">{'→ ' + method.returns}</span>
        )}
      </div>

      {/* Result area — mirrors resultArea in app.js:154,164,167 */}
      <div className="result-area">
        {running && (
          <div className="spinner-row">
            <span className="spinner" />
            {'calling '}
            <code>{method.name}</code>
          </div>
        )}
        {error && <ErrorBox err={error} />}
        {hasResult && !running && !error && (
          <ResultView result={result} onOpen={onOpen} />
        )}
      </div>
    </div>
  )
}
