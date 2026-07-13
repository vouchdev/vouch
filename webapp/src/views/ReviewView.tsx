import { useMutation, useQueryClient } from '@tanstack/react-query'
import { LoaderCircle, Sparkles } from 'lucide-react'
import { useState } from 'react'
import { EmptyState } from '../components/EmptyState'
import { ErrorCard } from '../components/ErrorCard'
import { useErrorToast, useToast } from '../components/Toast'
import { useConnection } from '../connection/ConnectionContext'
import type { ProjectState } from '../connection/ConnectionContext'
import { useFanout } from '../lib/fanout'
import { rpc, VouchRpcError } from '../lib/rpc'
import type { SessionEntry } from '../lib/types'
import { TranscriptView } from './TranscriptView'

const STAGE_LABEL: Record<SessionEntry['stage'], string> = {
  buffer: 'open buffer',
  pending: 'needs summary',
}

/** Badge text for a row: summarized wins over the raw capture stage. */
function stageLabel(s: SessionEntry): string {
  return s.summarized ? 'summarized' : STAGE_LABEL[s.stage]
}

/** Hints for the skip reasons kb.summarize_session can return instead of a summary. */
const SKIP_HINTS: Record<string, string> = {
  'not-configured': 'set capture.summary_llm_cmd in .vouch/config.yaml',
  'no-pending-summary-for-session': 'no filed summary proposal matches this session',
  'llm-failed': 'the summary LLM failed — check the server log',
}

interface Row {
  project: ProjectState
  s: SessionEntry
}

function rowKey(r: Row): string {
  const id = r.s.session_id ?? r.s.proposal_id ?? r.s.title ?? '(unknown)'
  return `${r.project.conn.endpoint} ${id}`
}

function rowTitle(s: SessionEntry): string {
  return s.title ?? s.session_id ?? '(untitled session)'
}

export function ReviewView() {
  const { aggregated, hasMethod } = useConnection()
  const { toast } = useToast()
  const qc = useQueryClient()
  const [selectedKey, setSelectedKey] = useState<string | null>(null)

  const sessions = useFanout<{ sessions: SessionEntry[] }>(['sessions'], 'kb.list_sessions', {}, {
    refetchInterval: 10_000,
  })
  useErrorToast(sessions.errors.length > 0, sessions.errors[0]?.error)

  const rows: Row[] = sessions.rows.flatMap((r) =>
    (r.data?.sessions ?? []).map((s) => ({ project: r.project, s })),
  )
  const selected = rows.find((r) => rowKey(r) === selectedKey) ?? null
  const canSummarize =
    !!selected &&
    !selected.s.summarized &&
    hasMethod('kb.summarize_session', selected.project.conn.endpoint)
  const canTranscript =
    !!selected &&
    !!selected.s.session_id &&
    hasMethod('kb.session_transcript', selected.project.conn.endpoint)

  const summarize = useMutation({
    mutationFn: (row: Row) =>
      rpc<{ session_id: string; summarized: boolean; proposal_id?: string | null; skipped?: string }>(
        row.project.conn, 'kb.summarize_session', { session_id: row.s.session_id },
      ),
    onError: (err) => {
      const code = err instanceof VouchRpcError ? err.code : undefined
      const message = err instanceof Error ? err.message : String(err)
      toast('error', code ? `${code}: ${message}` : message)
    },
    onSuccess: (res) => {
      if (res.summarized) {
        toast('success', 'Summary ready — moved to Pending')
        setSelectedKey(null)
        void qc.invalidateQueries({ queryKey: ['sessions'] })
        void qc.invalidateQueries({ queryKey: ['pending'] })
      } else {
        const skipped = res.skipped ?? 'unknown'
        const hint = SKIP_HINTS[skipped]
        toast('error', hint ? `${skipped}: ${hint}` : `summarization skipped: ${skipped}`)
      }
    },
  })

  if (sessions.unavailable) {
    return (
      <EmptyState
        title="Review is not available on this endpoint"
        hint="kb.list_sessions is not advertised in /capabilities."
      />
    )
  }
  if (sessions.isPending) return <p className="p-6 text-sm text-sepia">loading sessions…</p>
  if (sessions.isError)
    return (
      <div className="p-6">
        <ErrorCard
          code={(sessions.errors[0]?.error as { code?: string })?.code}
          message={
            sessions.errors[0]?.error instanceof Error
              ? sessions.errors[0].error.message
              : 'failed to load sessions'
          }
        />
      </div>
    )

  if (rows.length === 0) {
    return (
      <EmptyState
        title="No captured sessions"
        hint="Captured agent sessions appear here to read and summarize. A session that still needs a summary can be sent through review from its transcript."
      />
    )
  }

  return (
    <div className="flex h-full">
      <div className="flex w-96 shrink-0 flex-col border-r border-rule">
        <ul className="min-h-0 flex-1 overflow-y-auto">
          {rows.map((r) => (
            <li key={rowKey(r)} className="border-b border-rule/60">
              <button
                onClick={() => setSelectedKey(rowKey(r))}
                className={`block w-full min-w-0 px-4 py-4 text-left transition hover:bg-paper-2 ${
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
                    {stageLabel(r.s)}
                  </span>
                  {r.s.observations !== null && (
                    <span className="text-[11px] text-sepia">{r.s.observations} observations</span>
                  )}
                </div>
                <p className="line-clamp-2 text-sm text-ink-2">{rowTitle(r.s)}</p>
                <p className="mt-1 truncate font-mono text-[11px] text-sepia">
                  {r.s.session_id ?? '(no session id)'}
                  {r.s.last_activity && ` · ${r.s.last_activity.slice(0, 19).replace('T', ' ')}`}
                </p>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        {!selected ? (
          <div className="p-6">
            <EmptyState
              title="Select a session"
              hint="Pick a captured session to read its transcript — then Summarize to send it through review."
            />
          </div>
        ) : (
          <>
            <div className="shrink-0 border-b border-rule px-6 py-4">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                {aggregated && (
                  <span className="rounded bg-accent/15 px-2 py-0.5 font-mono text-[10px] text-accent-2">
                    {selected.project.label}
                  </span>
                )}
                <span className="rounded bg-paper-3 px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest text-accent">
                  {stageLabel(selected.s)}
                </span>
                <span className="break-all font-mono text-xs text-sepia">
                  {selected.s.session_id ?? '(no session id)'}
                </span>
                {selected.s.observations !== null && (
                  <span className="text-[11px] text-sepia">{selected.s.observations} observations</span>
                )}
                {selected.s.last_activity && (
                  <span className="text-[11px] text-sepia">
                    {selected.s.last_activity.slice(0, 19).replace('T', ' ')}
                  </span>
                )}
              </div>

              <p className="mb-3 text-[15px] leading-6 text-ink">{rowTitle(selected.s)}</p>

              {selected.s.summarized ? (
                <p className="text-xs text-sepia">
                  Already summarized — its proposal is in Pending for review.
                </p>
              ) : canSummarize ? (
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => summarize.mutate(selected)}
                    disabled={summarize.isPending || !selected.s.session_id}
                    title={selected.s.session_id ? undefined : 'no session id recorded'}
                    className="flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-40"
                  >
                    {summarize.isPending ? (
                      <>
                        <LoaderCircle size={15} className="animate-spin" /> Summarizing…
                      </>
                    ) : (
                      <>
                        <Sparkles size={15} /> Summarize
                      </>
                    )}
                  </button>
                  <span className="text-xs text-sepia">
                    {summarize.isPending
                      ? 'running the configured LLM — this can take a minute'
                      : selected.s.session_id
                        ? 'runs the configured LLM over the capture; the summary lands in Pending'
                        : 'no session id recorded — this capture cannot be summarized'}
                  </span>
                </div>
              ) : (
                <p className="text-xs text-sepia">
                  This endpoint cannot summarize — kb.summarize_session is not advertised.
                </p>
              )}
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto">
              {canTranscript ? (
                <TranscriptView
                  conn={selected.project.conn}
                  sessionId={selected.s.session_id as string}
                />
              ) : (
                <p className="p-6 text-sm text-sepia">
                  {selected.s.session_id
                    ? 'Transcript view is not available on this endpoint — kb.session_transcript is not advertised.'
                    : 'No transcript — this capture has no recorded session id.'}
                </p>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
