import { useQuery } from '@tanstack/react-query'
import { Play } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ClaimLifecycleActions } from '../components/ClaimLifecycleActions'
import { DeleteArtifactButton } from '../components/DeleteArtifactButton'
import { EmptyState } from '../components/EmptyState'
import { ErrorCard } from '../components/ErrorCard'
import { useErrorToast } from '../components/Toast'
import { useConnection } from '../connection/ConnectionContext'
import type { ProjectState } from '../connection/ConnectionContext'
import { stashStartHere } from '../lib/claude'
import { useFanout } from '../lib/fanout'
import { rpc } from '../lib/rpc'
import type { Claim, WhyEdge, WhyResult } from '../lib/types'

/** First session id recorded anywhere in the provenance tree. */
export function provenanceSession(edges: WhyEdge[]): string | null {
  for (const e of edges) {
    if (e.session_id) return e.session_id
    const nested = provenanceSession(e.children)
    if (nested) return nested
  }
  return null
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )
  return (
    <button
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true)
          if (timer.current) clearTimeout(timer.current)
          timer.current = setTimeout(() => setCopied(false), 2000)
        })
      }}
      className="shrink-0 rounded-lg border border-rule px-3 py-1.5 text-xs font-semibold text-ink-2 transition hover:bg-paper-3"
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

function StartHereDialog({
  claimId,
  sessionId,
  onOpenChat,
  onClose,
}: {
  claimId: string
  sessionId: string | null
  onOpenChat: () => void
  onClose: () => void
}) {
  const startCmd = `claude "/vouch-start ${claimId}"`
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="start-here-title"
        onClick={(e) => e.stopPropagation()}
        className="w-[34rem] max-w-full rounded-2xl border border-rule bg-paper-2 p-6 shadow-2xl"
      >
        <h2 id="start-here-title" className="mb-1 text-lg font-semibold text-ink">
          Start a session from this claim
        </h2>
        <p className="mb-4 text-sm text-sepia">
          To start from this claim, open a terminal (or the vscode terminal) in your project and run:
        </p>
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-rule bg-paper px-3 py-2">
          <code className="min-w-0 flex-1 break-all font-mono text-sm text-ink">{startCmd}</code>
          <CopyButton text={startCmd} />
        </div>
        {sessionId && (
          <>
            <p className="mb-2 text-sm text-sepia">
              Provenance records an originating session — resume it directly:
            </p>
            <div className="mb-4 flex items-center gap-2 rounded-lg border border-rule bg-paper px-3 py-2">
              <code className="min-w-0 flex-1 break-all font-mono text-sm text-ink">
                claude --resume {sessionId}
              </code>
              <CopyButton text={`claude --resume ${sessionId}`} />
            </div>
          </>
        )}
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-lg border border-rule px-4 py-2 text-sm text-ink-2 transition hover:bg-paper-3"
          >
            Close
          </button>
          <button
            onClick={onOpenChat}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-paper transition hover:bg-accent-2"
          >
            Open in Chat
          </button>
        </div>
      </div>
    </div>
  )
}

interface Row {
  project: ProjectState
  claim: Claim
}

function rowKey(r: Row): string {
  return `${r.project.conn.endpoint} ${r.claim.id}`
}

export function ClaimsView() {
  const { aggregated, hasMethod } = useConnection()
  const navigate = useNavigate()
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [startHereOpen, setStartHereOpen] = useState(false)

  const claims = useFanout<Claim[]>(['list', 'claim'], 'kb.list_claims')
  useErrorToast(claims.errors.length > 0, claims.errors[0]?.error)

  // Archived claims are hidden from retrieval; keep them out of this curated
  // list too, so archiving a claim visibly removes it. (`kb.list_claims`
  // returns every status; there's no "all-but-archived" server filter.)
  const rows: Row[] = claims.rows
    .flatMap((r) => r.data.map((claim) => ({ project: r.project, claim })))
    .filter((r) => r.claim.status !== 'archived')
  const selected = rows.find((r) => rowKey(r) === selectedKey) ?? null

  const why = useQuery({
    queryKey: ['why', selected?.project.conn.endpoint, selected?.claim.id],
    queryFn: () =>
      rpc<WhyResult>(selected!.project.conn, 'kb.why', { claim_id: selected!.claim.id }),
    enabled: !!selected && hasMethod('kb.why', selected.project.conn.endpoint),
  })
  const sessionId = why.data ? provenanceSession(why.data.provenance) : null

  function startHere(claim: Claim) {
    stashStartHere({ claimId: claim.id, text: claim.text, sessionId })
    navigate('/chat?mode=claude')
  }

  if (claims.unavailable) {
    return (
      <EmptyState
        title="Claims are not available on this endpoint"
        hint="kb.list_claims is not advertised in /capabilities."
      />
    )
  }
  if (claims.isPending) return <p className="p-6 text-sm text-sepia">loading claims…</p>
  if (claims.isError)
    return (
      <div className="p-6">
        <ErrorCard
          code={(claims.errors[0]?.error as { code?: string })?.code}
          message={
            claims.errors[0]?.error instanceof Error
              ? claims.errors[0].error.message
              : 'failed to load claims'
          }
        />
      </div>
    )

  if (rows.length === 0) {
    return (
      <EmptyState
        title="No approved claims yet"
        hint="When a proposal passes review it becomes an approved claim and lands here — the knowledge your agents can build on."
      />
    )
  }

  return (
    <div className="flex h-full">
      <ul className="w-96 shrink-0 overflow-y-auto border-r border-rule">
        {rows.map((r) => (
          <li key={rowKey(r)}>
            <button
              onClick={() => setSelectedKey(rowKey(r))}
              className={`block w-full border-b border-rule/60 px-5 py-4 text-left transition hover:bg-paper-2 ${
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
                  {r.claim.type}
                </span>
                <span className="truncate font-mono text-[11px] text-sepia">{r.claim.id}</span>
              </div>
              <p className="line-clamp-3 text-sm text-ink-2">{r.claim.text}</p>
              <p className="mt-1 text-[11px] text-sepia">
                {r.claim.status} · {Math.round(r.claim.confidence * 100)}%
              </p>
            </button>
          </li>
        ))}
      </ul>

      <div className="min-w-0 flex-1 overflow-y-auto p-6">
        {!selected ? (
          <EmptyState title="Select a claim" hint="Pick an approved claim to see it in full — then Start Here to work from it." />
        ) : (
          <div className="mx-auto max-w-2xl">
            <div className="mb-4 flex items-center gap-2">
              {aggregated && (
                <span className="rounded bg-accent/15 px-2 py-0.5 font-mono text-[10px] text-accent-2">
                  {selected.project.label}
                </span>
              )}
              <span className="rounded bg-paper-3 px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest text-accent">
                {selected.claim.type}
              </span>
              <span className="font-mono text-xs text-sepia">{selected.claim.id}</span>
            </div>

            <p className="mb-6 whitespace-pre-wrap text-[15px] leading-7 text-ink">{selected.claim.text}</p>

            <dl className="mb-6 rounded-xl border border-rule bg-paper-2 p-5">
              <div className="flex gap-3 border-b border-rule/60 py-2 text-sm">
                <dt className="w-28 shrink-0 text-xs uppercase tracking-wide text-sepia">status</dt>
                <dd className="text-ink-2">{selected.claim.status}</dd>
              </div>
              <div className="flex gap-3 border-b border-rule/60 py-2 text-sm">
                <dt className="w-28 shrink-0 text-xs uppercase tracking-wide text-sepia">confidence</dt>
                <dd className="text-ink-2">{Math.round(selected.claim.confidence * 100)}%</dd>
              </div>
              {Array.isArray(selected.claim.tags) && selected.claim.tags.length > 0 && (
                <div className="flex gap-3 border-b border-rule/60 py-2 text-sm">
                  <dt className="w-28 shrink-0 text-xs uppercase tracking-wide text-sepia">tags</dt>
                  <dd className="flex flex-wrap gap-1 text-ink-2">
                    {(selected.claim.tags as string[]).map((t) => (
                      <span key={t} className="rounded bg-paper-3 px-1.5 py-0.5 font-mono text-[11px]">{t}</span>
                    ))}
                  </dd>
                </div>
              )}
              {typeof selected.claim.created_at === 'string' && (
                <div className="flex gap-3 py-2 text-sm">
                  <dt className="w-28 shrink-0 text-xs uppercase tracking-wide text-sepia">created</dt>
                  <dd className="text-ink-2">{selected.claim.created_at.slice(0, 19).replace('T', ' ')}</dd>
                </div>
              )}
            </dl>

            <button
              onClick={() => setStartHereOpen(true)}
              className="flex items-center gap-2 rounded-lg bg-accent px-5 py-2.5 text-sm font-semibold text-paper transition hover:bg-accent-2"
            >
              <Play size={15} /> Start Here
            </button>
            <p className="mt-2 text-xs text-sepia">
              {sessionId
                ? `shows the command to work from this claim, resuming session ${sessionId.slice(0, 8)}`
                : 'shows the command to work from this claim in your own terminal'}
            </p>

            <div className="mt-6 space-y-3 border-t border-rule pt-5">
              <ClaimLifecycleActions
                project={selected.project}
                claimId={selected.claim.id}
                onDone={() => setSelectedKey(null)}
              />
              <DeleteArtifactButton
                project={selected.project}
                kind="claim"
                id={selected.claim.id}
                onDone={() => setSelectedKey(null)}
              />
            </div>

            {startHereOpen && (
              <StartHereDialog
                claimId={selected.claim.id}
                sessionId={sessionId}
                onOpenChat={() => startHere(selected.claim)}
                onClose={() => setStartHereOpen(false)}
              />
            )}
          </div>
        )}
      </div>
    </div>
  )
}
