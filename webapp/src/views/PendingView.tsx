import { useMutation, useQueryClient } from '@tanstack/react-query'
import { BookOpen, Check, Eraser, LoaderCircle, Merge, X } from 'lucide-react'
import { useState } from 'react'
import { EmptyState } from '../components/EmptyState'
import { ErrorCard } from '../components/ErrorCard'
import { Markdown } from '../components/Markdown'
import { useErrorToast, useToast } from '../components/Toast'
import { useConnection } from '../connection/ConnectionContext'
import type { ProjectState } from '../connection/ConnectionContext'
import { useFanout } from '../lib/fanout'
import { rpc, VouchRpcError } from '../lib/rpc'
import type { Proposal, SessionEntry } from '../lib/types'

function payloadPreview(p: Proposal): string {
  const pl = p.payload
  if (p.kind === 'delete') {
    const tk = typeof pl.target_kind === 'string' ? pl.target_kind : 'artifact'
    const id = typeof pl.id === 'string' ? pl.id : ''
    return `delete ${tk} ${id}`.trim()
  }
  if (typeof pl.text === 'string') return pl.text
  if (typeof pl.title === 'string') return pl.title
  if (typeof pl.name === 'string') return pl.name
  return JSON.stringify(pl).slice(0, 120)
}

/** Readable detail for a `delete` proposal: what it removes + a snapshot line. */
function DeletePayload({ payload }: { payload: Record<string, unknown> }) {
  const tk = typeof payload.target_kind === 'string' ? payload.target_kind : 'artifact'
  const id = typeof payload.id === 'string' ? payload.id : ''
  const snap = (payload.snapshot ?? {}) as Record<string, unknown>
  const summary =
    (typeof snap.text === 'string' && snap.text) ||
    (typeof snap.title === 'string' && snap.title) ||
    (typeof snap.name === 'string' && snap.name) ||
    ''
  const rows: [string, string][] = [
    ['action', `delete ${tk}`],
    ['id', id],
  ]
  if (summary) rows.push(['snapshot', summary])
  return (
    <>
      {rows.map(([k, v]) => (
        <div key={k} className="flex gap-3 border-b border-rule/60 py-2 text-sm last:border-b-0">
          <dt className="w-28 shrink-0 text-xs uppercase tracking-wide text-sepia">{k}</dt>
          <dd className="min-w-0 whitespace-pre-wrap break-words text-ink-2">{v}</dd>
        </div>
      ))}
    </>
  )
}

// Shape of the kb.compile report envelope (compile.CompileReport.to_dict()).
type CompileReport = {
  proposed: { title: string; proposal_id: string; page_id?: string }[]
  dropped: { title: string; reason: string }[]
  draft_count: number
  dry_run: boolean
}

// Shape of the kb.wipe_dead_refs envelope (lifecycle.DeadRefsWipeResult).
type DeadRefsReport = {
  pages: Record<string, string[]>
  proposals: Record<string, string[]>
  dropped: number
  dry_run: boolean
}

/** One queue row: a pending proposal plus the project it belongs to. */
interface Row {
  project: ProjectState
  proposal: Proposal
}

/** Proposal ids are only unique within one KB — key rows by endpoint too. */
function rowKey(r: Row): string {
  return `${r.project.conn.endpoint} ${r.proposal.id}`
}

export function PendingView() {
  const { conn, aggregated, hasMethod } = useConnection()
  const { toast } = useToast()
  const qc = useQueryClient()
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const [decisionError, setDecisionError] = useState<{ code?: string; message: string } | null>(null)
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [clearing, setClearing] = useState(false)
  const [clearReason, setClearReason] = useState('')
  const [deadRefRows, setDeadRefRows] = useState<Row[]>([])
  const [wipePreview, setWipePreview] = useState<DeadRefsReport | null>(null)

  const pending = useFanout<Proposal[]>(['pending'], 'kb.list_pending', {}, { refetchInterval: 10_000 })
  useErrorToast(pending.errors.length > 0, pending.errors[0]?.error)

  // Captured sessions without an LLM narrative wait on the Review page for a
  // summary; their raw proposals stay out of this queue until it exists.
  const sessions = useFanout<{ sessions: SessionEntry[] }>(['sessions'], 'kb.list_sessions', {}, {
    refetchInterval: 10_000,
  })
  const awaitingSummary = new Set(
    sessions.rows.flatMap((r) =>
      (r.data?.sessions ?? [])
        .filter((s) => s.stage === 'pending' && !s.summarized && s.proposal_id)
        .map((s) => `${r.project.conn.endpoint} ${s.proposal_id}`),
    ),
  )
  const rows: Row[] = pending.rows
    .flatMap((r) => r.data.map((proposal) => ({ project: r.project, proposal })))
    .filter((r) => !awaitingSummary.has(rowKey(r)))
  // Derived: an optimistic cache removal hides the detail pane in flight;
  // a rollback (onError restoring the cache) brings it back, error card intact.
  const selected = rows.find((r) => rowKey(r) === selectedKey) ?? null
  const canDecide =
    !!selected &&
    hasMethod('kb.approve', selected.project.conn.endpoint) &&
    hasMethod('kb.reject', selected.project.conn.endpoint)
  const canMerge = hasMethod('kb.merge_pending')
  // "Clear queue" rejects every currently-listed proposal at once. Target only
  // rows whose endpoint advertises kb.reject (aggregated mode spans projects).
  const clearTargets = rows.filter((r) => hasMethod('kb.reject', r.project.conn.endpoint))
  const canClear = clearTargets.length > 0
  // Intersect with the live queue: a proposal decided elsewhere mid-selection
  // must not be acted on. Merge combines PAGES inside ONE KB; batch-approve can
  // target any approvable row, across projects.
  const checkedRows = rows.filter((r) => checked.has(rowKey(r)))
  const mergeRows = checkedRows.filter((r) => r.proposal.kind === 'page')
  const mergeProject = mergeRows[0]?.project ?? null
  const mergeSameProject = mergeRows.every((r) => r.project === mergeProject)
  const mergeIds = mergeSameProject ? mergeRows.map((r) => r.proposal.id) : []
  // Batch approve: every row whose endpoint advertises kb.approve is selectable;
  // the action targets the checked subset.
  const approvableRows = rows.filter((r) => hasMethod('kb.approve', r.project.conn.endpoint))
  const approveTargets = approvableRows.filter((r) => checked.has(rowKey(r)))
  const allApprovableChecked =
    approvableRows.length > 0 && approveTargets.length === approvableRows.length
  const canCheck = (r: Row) =>
    hasMethod('kb.approve', r.project.conn.endpoint) || (canMerge && r.proposal.kind === 'page')

  function toggleChecked(key: string) {
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function toggleAllApprovable() {
    setChecked((prev) => {
      const next = new Set(prev)
      const keys = approvableRows.map(rowKey)
      const allOn = keys.length > 0 && keys.every((k) => next.has(k))
      for (const k of keys) {
        if (allOn) next.delete(k)
        else next.add(k)
      }
      return next
    })
  }

  function afterDecision() {
    setSelectedKey(null)
    setRejecting(false)
    setReason('')
    void qc.invalidateQueries({ queryKey: ['pending'] })
    void qc.invalidateQueries({ queryKey: ['status'] })
    void qc.invalidateQueries({ queryKey: ['stats'] })
    void qc.invalidateQueries({ queryKey: ['list'] })
    void qc.invalidateQueries({ queryKey: ['audit'] })
  }

  function decisionFailed(err: unknown) {
    const code = err instanceof VouchRpcError ? err.code : undefined
    const message = err instanceof Error ? err.message : String(err)
    // dead_claim_refs renders as a decision card (strip & approve?), not a
    // failure toast — the reviewer has a next step, nothing went wrong.
    if (code !== 'dead_claim_refs') toast('error', code ? `${code}: ${message}` : message)
    setDecisionError({ code, message })
  }

  // Optimistic updates target the owning project's slice of the fan-out cache.
  async function removeOptimistically(row: Row) {
    const key = ['pending', row.project.conn.endpoint]
    await qc.cancelQueries({ queryKey: key })
    const previous = qc.getQueryData<Proposal[]>(key)
    qc.setQueryData<Proposal[]>(key, (old) => (old ?? []).filter((p) => p.id !== row.proposal.id))
    return { previous, key }
  }

  function rollback(ctx: { previous?: Proposal[]; key?: unknown[] } | undefined) {
    if (ctx?.previous && ctx.key) qc.setQueryData(ctx.key, ctx.previous)
  }

  const approve = useMutation({
    mutationFn: (vars: { row: Row; dropMissingClaims?: boolean }) =>
      rpc<{ kind: string; id: string }>(vars.row.project.conn, 'kb.approve', {
        proposal_id: vars.row.proposal.id,
        ...(vars.dropMissingClaims ? { drop_missing_claims: true } : {}),
      }),
    onMutate: (vars) => removeOptimistically(vars.row),
    onError: (err, _vars, ctx) => {
      rollback(ctx)
      decisionFailed(err)
    },
    onSuccess: (res) => {
      toast('success', `Approved → ${res.kind}/${res.id}`)
      afterDecision()
    },
  })

  const reject = useMutation({
    mutationFn: (vars: { row: Row; reason: string }) =>
      rpc<{ proposal_id: string; status: string }>(vars.row.project.conn, 'kb.reject', {
        proposal_id: vars.row.proposal.id,
        reason: vars.reason,
      }),
    onMutate: (vars) => removeOptimistically(vars.row),
    onError: (err, _vars, ctx) => {
      rollback(ctx)
      decisionFailed(err)
    },
    onSuccess: (res) => {
      toast('info', `Rejected ${res.proposal_id}`)
      afterDecision()
    },
  })

  // Clear the queue: reject every listed row via kb.reject with one shared
  // reason. No bulk-reject endpoint exists — loop client-side, per project.
  const clear = useMutation({
    mutationFn: async (vars: { reason: string }) => {
      const results = await Promise.allSettled(
        clearTargets.map((r) =>
          rpc(r.project.conn, 'kb.reject', { proposal_id: r.proposal.id, reason: vars.reason }),
        ),
      )
      const ok = results.filter((x) => x.status === 'fulfilled').length
      return { ok, failed: results.length - ok }
    },
    onError: decisionFailed,
    onSuccess: ({ ok, failed }) => {
      toast(failed ? 'info' : 'success', `Cleared ${ok} pending${failed ? ` (${failed} failed)` : ''}`)
      setClearing(false)
      setClearReason('')
      afterDecision()
    },
  })

  const merge = useMutation({
    mutationFn: (vars: { project: ProjectState; ids: string[] }) =>
      rpc<{ proposal_id: string; merged_from: string[]; status: string }>(
        vars.project.conn, 'kb.merge_pending', { proposal_ids: vars.ids },
      ),
    onError: decisionFailed,
    onSuccess: (res, vars) => {
      toast('success', `Merged ${res.merged_from.length} → ${res.proposal_id}`)
      setChecked(new Set())
      setSelectedKey(`${vars.project.conn.endpoint} ${res.proposal_id}`)
      void qc.invalidateQueries({ queryKey: ['pending'] })
      void qc.invalidateQueries({ queryKey: ['stats'] })
    },
  })

  // Batch approve: approve every checked approvable row. No bulk endpoint exists —
  // loop client-side per project, the same shape as clear (reject-all).
  // Rows refused with dead_claim_refs are collected for a one-click
  // strip-and-retry pass instead of counting as plain failures.
  const approveSelected = useMutation({
    mutationFn: async (vars: { rows: Row[]; dropMissingClaims?: boolean }) => {
      const results = await Promise.allSettled(
        vars.rows.map((r) =>
          rpc(r.project.conn, 'kb.approve', {
            proposal_id: r.proposal.id,
            ...(vars.dropMissingClaims ? { drop_missing_claims: true } : {}),
          }),
        ),
      )
      const deadRefs: Row[] = []
      let ok = 0
      let failed = 0
      results.forEach((res, i) => {
        if (res.status === 'fulfilled') ok += 1
        else if (res.reason instanceof VouchRpcError && res.reason.code === 'dead_claim_refs')
          deadRefs.push(vars.rows[i])
        else failed += 1
      })
      return { ok, failed, deadRefs }
    },
    onError: decisionFailed,
    onSuccess: ({ ok, failed, deadRefs }) => {
      setDeadRefRows(deadRefs)
      const parts = [`Approved ${ok}`]
      if (deadRefs.length) parts.push(`${deadRefs.length} cite missing claims`)
      if (failed) parts.push(`${failed} failed`)
      toast(failed ? 'info' : 'success', parts.join(' — '))
      setChecked(new Set())
      afterDecision()
    },
  })

  // Wipe dead refs is KB-wide (pages + pending page proposals), so it is
  // scoped to a single project like compile. Two-step: a dry-run previews
  // what would be stripped, the confirm click applies it.
  const canWipe = !aggregated && !!conn && hasMethod('kb.wipe_dead_refs')
  const wipeDeadRefs = useMutation({
    mutationFn: (vars: { dryRun: boolean }) =>
      rpc<DeadRefsReport>(conn!, 'kb.wipe_dead_refs', { dry_run: vars.dryRun }),
    onError: decisionFailed,
    onSuccess: (res) => {
      if (res.dry_run) {
        if (res.dropped === 0) toast('info', 'No dead claim references found')
        else setWipePreview(res)
        return
      }
      setWipePreview(null)
      toast(
        'success',
        `Stripped ${res.dropped} dead reference(s) from ` +
          `${Object.keys(res.pages).length} page(s) and ` +
          `${Object.keys(res.proposals).length} pending proposal(s)`,
      )
      afterDecision()
    },
  })

  // Compile ingests ONE project's approved claims — it is offered when the
  // scope names a single project (use the scope switcher to pick one).
  const canCompile = !aggregated && !!conn && hasMethod('kb.compile')
  const compile = useMutation({
    mutationFn: () => rpc<CompileReport>(conn!, 'kb.compile'),
    onError: decisionFailed,
    onSuccess: (res) => {
      const dropped = res.dropped.length
      toast(
        'success',
        `Compiled ${res.proposed.length} page draft(s) into the queue` +
          (dropped ? ` — ${dropped} dropped by citation checks` : ''),
      )
      void qc.invalidateQueries({ queryKey: ['pending'] })
      void qc.invalidateQueries({ queryKey: ['status'] })
      void qc.invalidateQueries({ queryKey: ['stats'] })
      void qc.invalidateQueries({ queryKey: ['audit'] })
    },
  })

  // Nudge: count claim approvals that landed after the most recent
  // compile.run audit event — fresh compile input the wiki hasn't seen.
  // Computed client-side from the audit tail; no extra server surface.
  const audit = useFanout<{ events: { event: string }[] }>(['audit'], 'kb.audit', { tail: 200 }, {
    refetchInterval: 15_000,
    enabled: canCompile,
  })
  let freshApprovals = 0
  for (const e of audit.rows[0]?.data.events ?? []) {
    if (e.event === 'compile.run') freshApprovals = 0
    else if (e.event === 'proposal.claim.approve') freshApprovals += 1
  }

  // Rendered on the empty queue too: dead references live in durable pages,
  // so the cleanup is offered even when nothing is pending.
  const wipeBar = canWipe ? (
    <div className="flex items-center gap-3 border-b border-rule bg-paper-2 px-4 py-2.5">
      {wipePreview ? (
        <>
          <span className="text-xs font-medium text-accent-2">
            {wipePreview.dropped} dead claim ref(s) in {Object.keys(wipePreview.pages).length} page(s),{' '}
            {Object.keys(wipePreview.proposals).length} pending proposal(s)
          </span>
          <button
            onClick={() => wipeDeadRefs.mutate({ dryRun: false })}
            disabled={wipeDeadRefs.isPending}
            className="rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-40"
          >
            Wipe {wipePreview.dropped} dead ref(s)
          </button>
          <button
            onClick={() => setWipePreview(null)}
            className="rounded-lg border border-rule px-3 py-1.5 text-xs text-ink-2 transition hover:bg-paper-3"
          >
            Cancel
          </button>
        </>
      ) : (
        <>
          <button
            onClick={() => wipeDeadRefs.mutate({ dryRun: true })}
            disabled={wipeDeadRefs.isPending}
            className="flex items-center gap-2 rounded-lg border border-accent/40 px-3 py-1.5 text-xs font-semibold text-accent-2 transition hover:bg-accent/10"
          >
            {wipeDeadRefs.isPending ? (
              <LoaderCircle size={13} className="animate-spin" />
            ) : (
              <Eraser size={13} />
            )}
            Wipe dead claim refs
          </button>
          <span className="text-xs text-sepia">
            strip references to claims that no longer exist (audited)
          </span>
        </>
      )}
    </div>
  ) : null

  // The compile bar renders on the empty queue too — that is the natural
  // starting state for an ingest pass over already-approved claims.
  const compileBar = canCompile ? (
    <div className="flex items-center gap-3 border-b border-rule bg-paper-2 px-4 py-2.5">
      <button
        onClick={() => compile.mutate()}
        disabled={compile.isPending}
        className="flex items-center gap-2 rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-40"
      >
        {compile.isPending ? (
          <LoaderCircle size={13} className="animate-spin" />
        ) : (
          <BookOpen size={13} />
        )}
        {compile.isPending ? 'Compiling…' : 'Compile wiki'}
      </button>
      {compile.isPending ? (
        <span className="text-xs text-sepia">llm drafting topic pages — this can take a minute</span>
      ) : freshApprovals > 0 ? (
        <span className="text-xs font-medium text-accent-2">
          {freshApprovals} claim{freshApprovals === 1 ? '' : 's'} approved since the last compile —
          fresh material for the wiki
        </span>
      ) : (
        <span className="text-xs text-sepia">
          distill approved claims into topic pages; drafts land here for review
        </span>
      )}
    </div>
  ) : null

  if (pending.unavailable) {
    return (
      <EmptyState
        title="Pending review is not available on this endpoint"
        hint="kb.list_pending is not advertised in /capabilities."
      />
    )
  }
  if (pending.isPending) return <p className="p-6 text-sm text-sepia">loading queue…</p>
  if (pending.isError)
    return (
      <div className="p-6">
        <ErrorCard
          code={(pending.errors[0]?.error as { code?: string })?.code}
          message={
            pending.errors[0]?.error instanceof Error
              ? pending.errors[0].error.message
              : 'failed to load queue'
          }
        />
      </div>
    )

  if (rows.length === 0) {
    return (
      <div className="flex h-full flex-col">
        {compileBar}
        {wipeBar}
        <div className="min-h-0 flex-1">
          <EmptyState
            title="The queue is clear"
            hint="When an agent proposes a claim, page, entity, or relation, it lands here for review. Nothing enters the KB without a decision."
          />
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full">
      <div className="flex w-96 shrink-0 flex-col border-r border-rule">
        {compileBar}
        {wipeBar}
        {canClear &&
          (clearing ? (
            <div className="border-b border-rule bg-paper-2 px-4 py-2.5">
              <label className="mb-1 block text-xs font-medium text-ink-2" htmlFor="clear-reason">
                Reject all {clearTargets.length} pending — reason (audited)
              </label>
              <textarea
                id="clear-reason"
                value={clearReason}
                onChange={(e) => setClearReason(e.target.value)}
                placeholder="why clear the queue?"
                rows={2}
                className="mb-2 w-full rounded-lg border border-rule bg-paper px-3 py-2 text-sm text-ink outline-none focus:border-accent"
              />
              <div className="flex gap-2">
                <button
                  onClick={() => clear.mutate({ reason: clearReason.trim() })}
                  disabled={clearReason.trim() === '' || clear.isPending}
                  className="rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-40"
                >
                  Reject all {clearTargets.length}
                </button>
                <button
                  onClick={() => {
                    setClearing(false)
                    setClearReason('')
                  }}
                  className="rounded-lg border border-rule px-3 py-1.5 text-xs text-ink-2 transition hover:bg-paper-3"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-3 border-b border-rule bg-paper-2 px-4 py-2.5">
              <button
                onClick={() => setClearing(true)}
                className="flex items-center gap-2 rounded-lg border border-accent/40 px-3 py-1.5 text-xs font-semibold text-accent-2 transition hover:bg-accent/10"
              >
                <X size={13} /> Clear queue
              </button>
              <span className="text-xs text-sepia">reject all {clearTargets.length} pending at once</span>
            </div>
          ))}
        {approvableRows.length > 0 && (
          <div className="flex items-center gap-3 border-b border-rule bg-paper-2 px-4 py-2.5">
            <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-2">
              <input
                type="checkbox"
                aria-label="select all pending"
                checked={allApprovableChecked}
                onChange={toggleAllApprovable}
                className="accent-accent"
              />
              select all
            </label>
            {approveTargets.length >= 1 && (
              <>
                <button
                  onClick={() => approveSelected.mutate({ rows: approveTargets })}
                  disabled={approveSelected.isPending}
                  className="flex items-center gap-2 rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-40"
                >
                  {approveSelected.isPending ? (
                    <LoaderCircle size={13} className="animate-spin" />
                  ) : (
                    <Check size={13} />
                  )}
                  Approve {approveTargets.length} selected
                </button>
                <button
                  onClick={() => setChecked(new Set())}
                  className="text-xs text-sepia transition hover:text-ink"
                >
                  clear
                </button>
              </>
            )}
          </div>
        )}
        {deadRefRows.length > 0 && (
          <div className="flex items-center gap-3 border-b border-rule bg-paper-2 px-4 py-2.5">
            <span className="text-xs font-medium text-accent-2">
              {deadRefRows.length} proposal(s) cite claims that no longer exist
            </span>
            <button
              onClick={() => {
                const rows = deadRefRows
                setDeadRefRows([])
                approveSelected.mutate({ rows, dropMissingClaims: true })
              }}
              disabled={approveSelected.isPending}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-40"
            >
              Strip dead refs & approve
            </button>
            <button
              onClick={() => setDeadRefRows([])}
              className="text-xs text-sepia transition hover:text-ink"
            >
              dismiss
            </button>
          </div>
        )}
        {canMerge && mergeRows.length >= 2 && (
          <div className="flex items-center gap-3 border-b border-rule bg-paper-2 px-4 py-2.5">
            <button
              onClick={() => merge.mutate({ project: mergeProject!, ids: mergeIds })}
              disabled={merge.isPending || mergeIds.length < 2}
              title={mergeSameProject ? undefined : 'merge combines proposals within one project'}
              className="flex items-center gap-2 rounded-lg bg-accent px-3 py-1.5 text-xs font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-40"
            >
              <Merge size={13} /> Merge {mergeRows.length} into one
            </button>
            {!mergeSameProject && (
              <span className="text-xs text-accent-2">pick pages from one project</span>
            )}
            <button
              onClick={() => setChecked(new Set())}
              className="text-xs text-sepia transition hover:text-ink"
            >
              clear
            </button>
          </div>
        )}
        <ul className="min-h-0 flex-1 overflow-y-auto">
          {rows.map((r) => (
            <li key={rowKey(r)} className="flex items-stretch border-b border-rule/60">
              {canCheck(r) && (
                <label className="flex shrink-0 cursor-pointer items-start py-5 pl-4">
                  <input
                    type="checkbox"
                    aria-label={`select ${r.proposal.id}`}
                    checked={checked.has(rowKey(r))}
                    onChange={() => toggleChecked(rowKey(r))}
                    className="accent-accent"
                  />
                </label>
              )}
              <button
                onClick={() => {
                  setSelectedKey(rowKey(r))
                  setRejecting(false)
                  setDecisionError(null)
                }}
                className={`block min-w-0 flex-1 px-4 py-4 text-left transition hover:bg-paper-2 ${
                  rowKey(r) === selectedKey ? 'bg-paper-2' : ''
                }`}
              >
                <div className="mb-1 flex items-center gap-2">
                  {aggregated && (
                    <span className="rounded bg-accent/15 px-1.5 py-0.5 font-mono text-[10px] text-accent-2">
                      {r.project.label}
                    </span>
                  )}
                  <span className="rounded bg-paper-3 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-widest text-accent">
                    {r.proposal.kind}
                  </span>
                  <span className="truncate font-mono text-[11px] text-sepia">{r.proposal.id}</span>
                </div>
                <p className="line-clamp-2 text-sm text-ink-2">{payloadPreview(r.proposal)}</p>
                <p className="mt-1 text-[11px] text-sepia">by {r.proposal.proposed_by}</p>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto p-6">
        {!selected ? (
          <EmptyState title="Select a proposal" hint="Pick an item from the queue to see its full payload." />
        ) : (
          <div className="mx-auto max-w-2xl">
            <div className="mb-4 flex items-center gap-2">
              {aggregated && (
                <span className="rounded bg-accent/15 px-2 py-0.5 font-mono text-[10px] text-accent-2">
                  {selected.project.label}
                </span>
              )}
              <span className="rounded bg-paper-3 px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest text-accent">
                {selected.proposal.kind}
              </span>
              <span className="font-mono text-xs text-sepia">{selected.proposal.id}</span>
            </div>

            <dl className="mb-6 rounded-xl border border-rule bg-paper-2 p-5">
              {selected.proposal.kind === 'delete' ? (
                <DeletePayload payload={selected.proposal.payload} />
              ) : (
                Object.entries(selected.proposal.payload).map(([k, v]) =>
                k === 'body' && typeof v === 'string' ? (
                  <div key={k} className="border-b border-rule/60 py-2 text-sm last:border-b-0">
                    <dt className="mb-2 text-xs uppercase tracking-wide text-sepia">{k}</dt>
                    <dd className="min-w-0">
                      <Markdown>{v}</Markdown>
                    </dd>
                  </div>
                ) : (
                  <div key={k} className="flex gap-3 border-b border-rule/60 py-2 text-sm last:border-b-0">
                    <dt className="w-28 shrink-0 text-xs uppercase tracking-wide text-sepia">{k}</dt>
                    <dd className="min-w-0 whitespace-pre-wrap break-words text-ink-2">
                      {typeof v === 'string' ? v : JSON.stringify(v)}
                    </dd>
                  </div>
                ),
                )
              )}
              <div className="flex gap-3 py-2 text-sm">
                <dt className="w-28 shrink-0 text-xs uppercase tracking-wide text-sepia">proposed by</dt>
                <dd className="text-ink-2">{selected.proposal.proposed_by}</dd>
              </div>
            </dl>

            {decisionError?.code === 'dead_claim_refs' ? (
              <div className="mb-4 rounded-xl border border-accent/50 bg-paper-2 p-4">
                <p className="mb-2 text-sm text-ink-2">
                  This page cites claim(s) that no longer exist. Remove the dead
                  references and approve what remains? The dropped ids are
                  recorded in the audit log.
                </p>
                <p className="mb-3 break-words font-mono text-xs text-sepia">
                  {decisionError.message}
                </p>
                <div className="flex gap-2">
                  <button
                    onClick={() => {
                      setDecisionError(null)
                      approve.mutate({ row: selected, dropMissingClaims: true })
                    }}
                    disabled={approve.isPending}
                    className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-paper hover:bg-accent-2 disabled:opacity-40"
                  >
                    Strip dead refs & approve
                  </button>
                  <button
                    onClick={() => setDecisionError(null)}
                    className="rounded-lg border border-rule px-4 py-2 text-sm text-ink-2 hover:bg-paper-3"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : decisionError ? (
              <div className="mb-4">
                <ErrorCard code={decisionError.code} message={decisionError.message} />
              </div>
            ) : null}

            {!canDecide ? (
              <p className="text-sm text-sepia">
                This endpoint is read-only for you — kb.approve / kb.reject are not advertised.
              </p>
            ) : rejecting ? (
              <div className="rounded-xl border border-rule bg-paper-2 p-4">
                <label className="mb-1 block text-xs font-medium text-ink-2" htmlFor="reject-reason">
                  Rejection reason (recorded in the audit log)
                </label>
                <textarea
                  id="reject-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="why is this rejected?"
                  rows={2}
                  className="mb-3 w-full rounded-lg border border-rule bg-paper px-3 py-2 text-sm text-ink outline-none focus:border-accent"
                />
                <div className="flex gap-2">
                  <button
                    onClick={() => reject.mutate({ row: selected, reason: reason.trim() })}
                    disabled={reason.trim() === '' || reject.isPending}
                    className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-paper hover:bg-accent-2 disabled:opacity-40"
                  >
                    Confirm reject
                  </button>
                  <button
                    onClick={() => setRejecting(false)}
                    className="rounded-lg border border-rule px-4 py-2 text-sm text-ink-2 hover:bg-paper-3"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="flex gap-3">
                <button
                  onClick={() => approve.mutate({ row: selected })}
                  disabled={approve.isPending}
                  className="flex items-center gap-2 rounded-lg bg-ok/90 px-5 py-2.5 text-sm font-semibold text-paper transition hover:bg-ok disabled:opacity-40"
                >
                  <Check size={15} /> Approve
                </button>
                <button
                  onClick={() => {
                    setRejecting(true)
                    setDecisionError(null)
                  }}
                  className="flex items-center gap-2 rounded-lg border border-accent/50 px-5 py-2.5 text-sm font-semibold text-accent-2 transition hover:bg-accent/10"
                >
                  <X size={15} /> Reject
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
