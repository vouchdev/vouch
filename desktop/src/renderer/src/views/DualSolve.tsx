// DualSolve.tsx — bespoke Dual-Solve view.
// Ported from src/renderer/views/dualsolve.js (full file).
// Drives the HTTP child's runner: run an issue through claude + codex, stream
// phase progress over the ws, show both diffs side by side, and choose a winner
// (which proposes its rationale to the kb through the review gate — nothing is
// auto-approved).

import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import * as api from '../lib/client'
import { Diff } from '../components/Diff'
import type { ProgressFrame } from '../../../shared/ipc'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Candidate {
  engine: string
  branch?: string
  ok: boolean
  changed_files?: string[]
  log?: string
  diff?: string
  error?: string
}

interface Recommendation {
  engine?: string
  reason?: string
}

interface DsState {
  id: string | null
  status: string
  progress: string[]
  candidates: Candidate[]
  recommendation: Recommendation | null
}

type DsAction =
  | { type: 'RESET' }
  | { type: 'SET_ID'; id: string }
  | { type: 'PUSH_PROGRESS'; msg: string }
  | { type: 'SET_PROGRESS'; msgs: string[] }
  | { type: 'SET_JOB'; job: Record<string, unknown> }

function dsReducer(s: DsState, a: DsAction): DsState {
  switch (a.type) {
    case 'RESET':
      return { id: null, status: 'idle', progress: [], candidates: [], recommendation: null }
    case 'SET_ID':
      return { ...s, id: a.id, status: 'running' }
    case 'PUSH_PROGRESS':
      return { ...s, progress: [...s.progress, a.msg] }
    case 'SET_PROGRESS':
      return { ...s, progress: a.msgs }
    case 'SET_JOB': {
      const j = a.job
      const candidates = Array.isArray(j.candidates) ? (j.candidates as Candidate[]) : s.candidates
      const recommendation =
        j.recommendation && typeof j.recommendation === 'object'
          ? (j.recommendation as Recommendation)
          : s.recommendation
      return {
        ...s,
        status: typeof j.status === 'string' ? j.status : s.status,
        candidates,
        recommendation,
      }
    }
    default:
      return s
  }
}

const initialDs: DsState = {
  id: null,
  status: 'idle',
  progress: [],
  candidates: [],
  recommendation: null,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function ErrBox({ msg }: { msg: string }) {
  return <div className="errbox">{msg}</div>
}

const EFFORT_OPTIONS = ['low', 'medium', 'high', 'max']

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function DualSolve() {
  const [ds, dispatch] = useReducer(dsReducer, initialDs)
  const [running, setRunning] = useState(false)
  const [runUnavailable, setRunUnavailable] = useState(false)
  const [issueUrl, setIssueUrl] = useState('')
  const [claudeEffort, setClaudeEffort] = useState('high')
  const [codexEffort, setCodexEffort] = useState('high')
  const [reason, setReason] = useState('')
  const [noticeEl, setNoticeEl] = useState<React.ReactNode>(null)
  const [issueTitle, setIssueTitle] = useState<string | null>(null)
  const [resultEl, setResultEl] = useState<React.ReactNode>(null)

  // Stable refs so effects/callbacks see the latest state without re-subscribing
  const dsRef = useRef(ds)
  dsRef.current = ds
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const progressRef = useRef<HTMLPreElement | null>(null)
  // Unmount guard — set to false on cleanup so in-flight async paths skip setState
  const mountedRef = useRef(true)

  // Mark unmounted so in-flight async callbacks skip setState
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  // Preconditions check on mount
  useEffect(() => {
    api.ds.preconditions().then((pre) => {
      if (!mountedRef.current) return
      const probs: string[] = []
      if (!pre.gitRepo) probs.push('the KB must live inside a git repository')
      if (!pre.vouchSupportsSandbox)
        probs.push('the connected vouch does not support sandboxed dual-solve')
      if (pre.tools && !pre.tools.ok)
        probs.push('missing on PATH: ' + pre.tools.missing.join(', '))
      if (probs.length) {
        setNoticeEl(
          <div className="warnbox">
            <strong>dual-solve unavailable — </strong>
            {probs.join('; ')}
            <div className="muted small">
              dual-solve runs the vouch review-ui (needs the [web] extra) and uses Docker sandbox image{' '}
              <code>{(pre.tools && pre.tools.image) || 'vouch/coder:latest'}</code>.
            </div>
          </div>,
        )
        setRunUnavailable(true)
      }
    }).catch(() => {})
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Live progress subscription — mirrors module-level unsub in dualsolve.js.
  // Uses loadJobRef so the closure always invokes the latest loadJob without
  // being captured as a stale value at mount time.
  useEffect(() => {
    const unsub = api.on('vouch:progress', (f: ProgressFrame) => {
      const cur = dsRef.current
      if (!f || (cur.id && f.job_id !== cur.id)) return
      if (f.event === 'progress') {
        dispatch({ type: 'PUSH_PROGRESS', msg: f.message ?? '' })
      } else if (f.event === 'ready' || f.event === 'done') {
        loadJobRef.current()
      } else if (f.event === 'error') {
        setResultEl(<ErrBox msg={f.message || 'run failed'} />)
        setRunning(false)
      }
    })
    return () => {
      try { unsub() } catch { /* noop */ }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Auto-scroll progress pane whenever new lines arrive
  useEffect(() => {
    const el = progressRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [ds.progress])

  // Cleanup poll timer on unmount
  useEffect(() => {
    return () => stopPolling()
  }, [])

  // -------------------------------------------------------------------------
  // Polling helpers
  // -------------------------------------------------------------------------

  function stopPolling() {
    if (pollTimerRef.current) clearInterval(pollTimerRef.current)
    pollTimerRef.current = null
  }

  // loadJob reads all state through refs/stable setters so it is safe to
  // memoise with an empty dep array.  A loadJobRef lets the vouch:progress
  // effect (mounted once) always call the current version.
  const loadJob = useCallback(async () => {
    const jobId = dsRef.current.id
    if (!jobId) return
    try {
      const j = await api.ds.job(jobId)
      if (!mountedRef.current) return
      const serverProgress = Array.isArray(j.progress) ? (j.progress as string[]) : null
      if (serverProgress && serverProgress.length > dsRef.current.progress.length) {
        dispatch({ type: 'SET_PROGRESS', msgs: serverProgress })
      }
      if (j.issue && typeof j.issue === 'object') {
        const issue = j.issue as Record<string, unknown>
        setIssueTitle(`#${issue.number} ${issue.title}`)
      }
      dispatch({ type: 'SET_JOB', job: j })
      const status = typeof j.status === 'string' ? j.status : ''
      // Mirror dualsolve.js line 102: ready → stopPolling + setRunning(false) so
      // form re-enables while the choose bar is visible.
      if (status === 'ready') { stopPolling(); setRunning(false) }
      if (status === 'done') { stopPolling(); renderDone(j); setRunning(false) }
      if (status === 'error') {
        stopPolling()
        setResultEl(<ErrBox msg={typeof j.error === 'string' ? j.error : 'run errored'} />)
        setRunning(false)
      }
    } catch (e) {
      if (!mountedRef.current) return
      setResultEl(<ErrBox msg={e instanceof Error ? e.message : String(e)} />)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Stable ref so the mount-time vouch:progress closure always calls the
  // latest loadJob (avoids stale closure without adding loadJob to the effect deps).
  const loadJobRef = useRef(loadJob)
  loadJobRef.current = loadJob

  function startPolling() {
    stopPolling()
    pollTimerRef.current = setInterval(() => loadJobRef.current(), 2000)
    loadJobRef.current()
  }

  // -------------------------------------------------------------------------
  // Result renderer
  // -------------------------------------------------------------------------

  function renderDone(r: Record<string, unknown>) {
    const keptBranch = typeof r.kept_branch === 'string' ? r.kept_branch : null
    const changedFiles = Array.isArray(r.changed_files) ? (r.changed_files as string[]) : []
    const proposedIds = Array.isArray(r.proposed_ids) ? (r.proposed_ids as string[]) : []
    setResultEl(
      <div className="card good">
        {keptBranch
          ? <p>kept branch <code>{keptBranch}</code></p>
          : <p>kept neither branch</p>}
        {changedFiles.length > 0 && (
          <div>
            <p>changed files:</p>
            <ul className="mono ds-files">
              {changedFiles.map((f, i) => <li key={i}>{f}</li>)}
            </ul>
          </div>
        )}
        {proposedIds.length > 0
          ? (
            <div>
              <p>
                {proposedIds.length} claim(s) proposed —{' '}
                <strong>review them in the queue</strong> (nothing was auto-approved):
              </p>
              <ul className="mono">
                {proposedIds.map((id, i) => <li key={i}>{id}</li>)}
              </ul>
            </div>
          )
          : <p className="muted">no claims proposed.</p>}
      </div>,
    )
  }

  // -------------------------------------------------------------------------
  // Event handlers
  // -------------------------------------------------------------------------

  async function run(e: React.FormEvent) {
    e.preventDefault()
    dispatch({ type: 'RESET' })
    setIssueTitle(null)
    setResultEl(null)
    if (!issueUrl.trim()) return
    setRunning(true)
    dispatch({ type: 'PUSH_PROGRESS', msg: 'starting dual-solve server…' })
    try {
      await api.ds.ensure()
      if (!mountedRef.current) return
      const r = await api.ds.run({
        issue_url: issueUrl.trim(),
        claude_effort: claudeEffort,
        codex_effort: codexEffort,
      })
      if (!mountedRef.current) return
      dispatch({ type: 'SET_ID', id: r.job_id })
      dispatch({
        type: 'PUSH_PROGRESS',
        msg: `job ${r.job_id} started — engines run sequentially, this can take minutes.`,
      })
      startPolling()
    } catch (e2) {
      if (!mountedRef.current) return
      setResultEl(<ErrBox msg={e2 instanceof Error ? e2.message : String(e2)} />)
      setRunning(false)
    }
  }

  async function choose(winner: string | null) {
    // Mirror dualsolve.js line 143: clear(chooseBar) runs synchronously before
    // the await so the buttons vanish immediately, preventing double-submit.
    // We achieve this by advancing status to 'done' before the API call.
    dispatch({ type: 'SET_JOB', job: { status: 'done' } })
    setResultEl(
      <div className="spinner-row">
        <span className="spinner" />
        {' '}finalizing…
      </div>,
    )
    try {
      const r = await api.ds.choose({ job_id: dsRef.current.id, winner, reason: reason.trim() })
      if (!mountedRef.current) return
      dispatch({ type: 'SET_JOB', job: { ...r, status: 'done' } })
      renderDone(r)
    } catch (e) {
      if (!mountedRef.current) return
      setResultEl(<ErrBox msg={e instanceof Error ? e.message : String(e)} />)
    }
  }

  // -------------------------------------------------------------------------
  // Sub-renders
  // -------------------------------------------------------------------------

  function renderPanes() {
    if (!ds.candidates.length) return null
    return (
      <div className="ds-panes">
        {ds.candidates.map((c, i) => (
          <div key={i} className="ds-pane">
            <div className="ds-pane-head">
              <strong>{c.engine}</strong>
              {c.branch && <code className="small">{c.branch}</code>}
              {c.ok
                ? <span className="pill good">ok</span>
                : <span className="pill warn">failed</span>}
            </div>
            {c.changed_files && c.changed_files.length > 0 && (
              <ul className="mono ds-files">
                {c.changed_files.map((f, fi) => <li key={fi}>{f}</li>)}
              </ul>
            )}
            {c.log && (
              <details className="ds-log">
                <summary>{c.engine} log</summary>
                <pre>{c.log}</pre>
              </details>
            )}
            {c.ok
              ? <Diff diff={c.diff} />
              : <p className="errbox">{c.error || 'engine produced no diff'}</p>}
          </div>
        ))}
      </div>
    )
  }

  function renderRecommendation() {
    const rec = ds.recommendation
    if (!rec || !rec.reason) return null
    return (
      <div className="ds-recommendation">
        <strong>recommendation: </strong>
        {rec.engine || 'no automatic pick'}
        {' -- '}
        {rec.reason}
      </div>
    )
  }

  function renderChoose() {
    if (ds.status !== 'ready') return null
    return (
      <div className="ds-choose">
        <div className="ds-choose-row">
          <input
            className="input"
            placeholder="one line: why the winner is better (recorded with the proposal)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </div>
        <div className="ds-choose-row">
          {ds.candidates.filter((c) => c.ok).map((c, i) => (
            <button key={i} className="btn ok" onClick={() => choose(c.engine)}>
              Choose {c.engine}
            </button>
          ))}
          <button className="btn ghost" onClick={() => choose(null)}>
            Keep neither
          </button>
        </div>
      </div>
    )
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className="ds">
      <div className="ds-notice">
        {issueTitle && <div className="ds-issue">{issueTitle}</div>}
        {noticeEl}
      </div>
      <form className="ds-run" onSubmit={run}>
        <div className="ds-run-row">
          <input
            className="input"
            placeholder="github issue url, or owner/name#42"
            value={issueUrl}
            disabled={running || runUnavailable}
            onChange={(e) => setIssueUrl(e.target.value)}
          />
          <button className="btn" type="submit" disabled={running || runUnavailable}>
            {running ? 'running…' : 'Run dual-solve'}
          </button>
        </div>
        <div className="ds-run-row efforts">
          <label>
            claude effort
            <select
              className="input"
              value={claudeEffort}
              onChange={(e) => setClaudeEffort(e.target.value)}
            >
              {EFFORT_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </label>
          <label>
            codex effort
            <select
              className="input"
              value={codexEffort}
              onChange={(e) => setCodexEffort(e.target.value)}
            >
              {EFFORT_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </label>
        </div>
      </form>
      {ds.progress.length > 0 && (
        <pre className="ds-progress" ref={progressRef}>
          {ds.progress.join('\n')}
        </pre>
      )}
      {renderRecommendation()}
      {renderPanes()}
      {renderChoose()}
      {resultEl !== null && <div className="ds-result">{resultEl}</div>}
    </div>
  )
}
