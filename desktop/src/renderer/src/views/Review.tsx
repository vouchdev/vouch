// Review.tsx — Review & Lifecycle bespoke view.
//
// Faithful port of renderReview / proposalActions from src/renderer/app.js:233-280.
//
// Class names: review-queue queue section-head lifecycle card-actions btn ok
//   danger sm ghost act-msg good bad input

import { useEffect, useState } from 'react'
import * as api from '../lib/client'
import { useVouch, isAvailable } from '../lib/VouchContext'
import { methods } from '../../../shared/methods.gen'
import MethodCard from '../components/MethodCard'
import type { OnOpen } from '../components/MethodCard'
import { ProposalCard } from '../components/results/Cards'

// ---------------------------------------------------------------------------
// ProposalActions — faithful port of proposalActions() in app.js:256-280
// ---------------------------------------------------------------------------

interface ProposalActionsProps {
  p: Record<string, unknown>
}

function ProposalActions({ p }: ProposalActionsProps) {
  const { state, actions } = useVouch()
  const pid = (p.proposal_id ?? p.id) as string

  const canApprove = isAvailable(state, 'kb.approve')
  const canReject = isAvailable(state, 'kb.reject')

  // approve state — approving locks the button until success or error
  const [approveInFlight, setApproveInFlight] = useState(false)
  const [actionMsg, setActionMsg] = useState<{ text: string; good: boolean } | null>(null)

  // reject flow state — when active the approve+reject buttons are hidden entirely
  // (mirrors app.js:276 mount(box, input, confirm, msg) which replaces both buttons)
  const [showRejectInput, setShowRejectInput] = useState(false)
  const [rejectReason, setRejectReason] = useState('')
  const [rejectDisabled, setRejectDisabled] = useState(false)

  async function handleApprove() {
    setApproveInFlight(true)
    setActionMsg(null)
    try {
      const r = await api.call<{ kind: string; id?: string }>('kb.approve', { proposal_id: pid })
      setActionMsg({ text: `approved → ${r.kind} ${r.id ?? ''}`, good: true })
      actions.refreshPending()
    } catch (e) {
      setActionMsg({ text: (e as Error).message, good: false })
      setApproveInFlight(false)
    }
  }

  function handleRejectClick() {
    setShowRejectInput(true)
    setActionMsg(null)
  }

  async function handleConfirmReject() {
    if (!rejectReason.trim()) return
    setRejectDisabled(true)
    setActionMsg(null)
    try {
      await api.call('kb.reject', { proposal_id: pid, reason: rejectReason.trim() })
      setActionMsg({ text: 'rejected', good: true })
      actions.refreshPending()
    } catch (e) {
      setActionMsg({ text: (e as Error).message, good: false })
      setRejectDisabled(false)
    }
  }

  return (
    <div className="card-actions">
      {!showRejectInput && (
        <>
          <button
            className="btn ok sm"
            disabled={!canApprove || approveInFlight}
            onClick={() => void handleApprove()}
          >
            Approve
          </button>
          <button
            className="btn danger sm ghost"
            disabled={!canReject}
            onClick={handleRejectClick}
          >
            Reject…
          </button>
        </>
      )}
      {showRejectInput && (
        <>
          <input
            className="input sm"
            placeholder="reason (required)"
            value={rejectReason}
            onChange={(e) => setRejectReason(e.target.value)}
          />
          <button
            className="btn danger sm"
            disabled={rejectDisabled}
            onClick={() => void handleConfirmReject()}
          >
            Confirm reject
          </button>
        </>
      )}
      {actionMsg && (
        <span className="act-msg">
          <span className={actionMsg.good ? 'good' : 'bad'}>{actionMsg.text}</span>
        </span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// ProposalActionsRenderer — adapts ProposalActions to the ReactNode-returning
// `actions` prop expected by ProposalCard
// ---------------------------------------------------------------------------

function makeProposalActions(onOpen: OnOpen) {
  return function ProposalActionsRenderer(p: Record<string, unknown>) {
    // onOpen is unused in the actions pane itself (it's threaded through
    // ProposalCard), but the closure keeps the signature consistent.
    void onOpen
    return <ProposalActions p={p} />
  }
}

// ---------------------------------------------------------------------------
// PendingQueue — mirrors the queue section of renderReview / loadQueue
// ---------------------------------------------------------------------------

interface PendingQueueProps {
  onOpen: OnOpen
}

function PendingQueue({ onOpen }: PendingQueueProps) {
  const [items, setItems] = useState<Record<string, unknown>[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  async function loadQueue() {
    setLoading(true)
    setErr(null)
    try {
      const result = await api.call<Record<string, unknown>[]>('kb.list_pending', {})
      setItems(result)
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadQueue()
  }, [])

  const actionsRenderer = makeProposalActions(onOpen)

  return (
    <div className="review-queue">
      <div className="section-head">
        <h2>Pending queue</h2>
        <button
          className="btn ghost sm"
          onClick={() => void loadQueue()}
        >
          Refresh
        </button>
      </div>
      <div className="queue">
        {loading && <div className="muted">loading…</div>}
        {!loading && err && <div className="errbox">{err}</div>}
        {!loading && !err && items && items.length === 0 && (
          <p className="muted">nothing pending — the queue is clear.</p>
        )}
        {!loading && !err && items && items.map((p, i) => (
          <ProposalCard
            key={(p.proposal_id ?? p.id ?? i) as string}
            p={p}
            onOpen={onOpen}
            actions={actionsRenderer}
          />
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Review — top-level view component
// ---------------------------------------------------------------------------

interface ReviewProps {
  onOpen: OnOpen
}

export default function Review({ onOpen }: ReviewProps) {
  const lifecycleMethods = methods.filter(
    (m) => m.view === 'review' && m.name !== 'kb.list_pending',
  )

  return (
    <>
      <PendingQueue onOpen={onOpen} />
      <div className="lifecycle">
        <div className="section-head">
          <h2>Lifecycle operations</h2>
        </div>
        {lifecycleMethods.map((m) => (
          <MethodCard key={m.name} method={m} onOpen={onOpen} />
        ))}
      </div>
    </>
  )
}
