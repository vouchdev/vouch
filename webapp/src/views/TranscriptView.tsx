import { useQuery } from '@tanstack/react-query'
import { EyeOff, MessagesSquare, ScrollText, Sparkles } from 'lucide-react'
import { useMemo, useState } from 'react'
import { EmptyState } from '../components/EmptyState'
import { ErrorCard } from '../components/ErrorCard'
import { MessageBlock } from '../components/transcript/MessageBlock'
import { VouchRpcError } from '../lib/rpc'
import { fetchTranscript } from '../lib/transcript'
import type { Observation, TranscriptMessage } from '../lib/transcript'
import type { VouchConnectionInfo } from '../lib/types'

function Degraded({ reason, observations }: { reason: string; observations: Observation[] }) {
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-rule/60 bg-paper-2 px-3 py-2 text-xs text-sepia">
        original transcript unavailable — {reason}. Showing captured activity.
      </div>
      {observations.length === 0 ? (
        <EmptyState title="No captured activity" />
      ) : (
        <ol className="space-y-1">
          {observations.map((o, i) => (
            <li
              key={i}
              className="flex items-center gap-2 rounded-lg border border-rule bg-paper-2 px-3 py-1.5 text-xs"
            >
              <span className="font-mono text-[11px] font-semibold text-accent">{o.tool}</span>
              <span className="text-ink-2">{o.summary}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/** A message the dialog view shows: it has at least one visible text block. */
function hasDialogText(m: TranscriptMessage): boolean {
  if (m.noise) return false
  return m.blocks.some((b) => b.type === 'text' && !b.noise && b.text.trim() !== '')
}

type RenderItem =
  | { kind: 'message'; index: number }
  /** Consecutive messages the current view hides, folded into one stub row. */
  | { kind: 'stub'; start: number; indices: number[]; tools: number; thinking: number; injected: number }

function groupMessages(messages: TranscriptMessage[], dialog: boolean): RenderItem[] {
  const items: RenderItem[] = []
  for (let i = 0; i < messages.length; i++) {
    const m = messages[i]
    const hidden = dialog ? !hasDialogText(m) : !!m.noise
    if (!hidden) {
      items.push({ kind: 'message', index: i })
      continue
    }
    let stub = items[items.length - 1]
    if (!stub || stub.kind !== 'stub') {
      stub = { kind: 'stub', start: i, indices: [], tools: 0, thinking: 0, injected: 0 }
      items.push(stub)
    }
    stub.indices.push(i)
    if (m.noise) stub.injected += 1
    for (const b of m.blocks) {
      if (b.type === 'tool_use') stub.tools += 1
      else if (b.type === 'thinking') stub.thinking += 1
    }
  }
  return items
}

function stubLabel(s: Extract<RenderItem, { kind: 'stub' }>, dialog: boolean): string {
  if (!dialog) {
    return `${s.indices.length} injected/system ${s.indices.length === 1 ? 'message' : 'messages'} hidden`
  }
  const parts: string[] = []
  if (s.tools > 0) parts.push(`${s.tools} tool ${s.tools === 1 ? 'run' : 'runs'}`)
  if (s.thinking > 0) parts.push(`${s.thinking} thinking`)
  if (s.injected > 0) parts.push(`${s.injected} injected`)
  const detail = parts.length > 0 ? ` (${parts.join(', ')})` : ''
  return `${s.indices.length} working ${s.indices.length === 1 ? 'step' : 'steps'} hidden${detail}`
}

export function TranscriptView({
  conn,
  sessionId,
  agent,
}: {
  conn: VouchConnectionInfo
  sessionId: string
  agent?: string
}) {
  // Subagent drill-down replaces the shown transcript with the child's,
  // keeping a back stack to the parent session.
  const [stack, setStack] = useState<{ id: string; agent?: string }[]>([{ id: sessionId, agent }])
  // The reviewer's default is the conversation itself — prompts and replies.
  // "full transcript" brings back the working steps (tools, thinking), with
  // injected/system content still behind stubs.
  const [dialog, setDialog] = useState(true)
  const [showHidden, setShowHidden] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  // grade.n keys the query so "grade" and "re-grade" each fetch exactly once.
  const [grade, setGrade] = useState<{ on: boolean; fresh: boolean; n: number }>({
    on: false,
    fresh: false,
    n: 0,
  })
  const top = stack[stack.length - 1]
  const q = useQuery({
    queryKey: ['transcript', conn.endpoint, top.id, grade.n],
    queryFn: () =>
      fetchTranscript(conn, top.id, top.agent, {
        grade: grade.on,
        regrade: grade.fresh,
      }),
    placeholderData: (prev) => prev,
  })

  const t = q.data
  const items = useMemo(
    () => (t?.available ? groupMessages(t.messages, dialog) : []),
    [t, dialog],
  )

  if (q.isPending) return <div className="p-6 text-sm text-sepia">Loading transcript…</div>
  if (q.isError) {
    const e = q.error
    return (
      <div className="p-6">
        <ErrorCard
          code={e instanceof VouchRpcError ? e.code : undefined}
          message={e instanceof Error ? e.message : String(e)}
        />
      </div>
    )
  }
  if (!t) return null

  const noiseCount = t.available ? t.messages.filter((m) => m.noise).length : 0

  return (
    <div className="space-y-3 p-4">
      {stack.length > 1 && (
        <button
          onClick={() => setStack((s) => s.slice(0, -1))}
          className="text-xs text-accent-2 hover:underline"
        >
          ← back to parent session
        </button>
      )}
      {!t.available ? (
        <Degraded reason={t.reason} observations={t.observations} />
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-rule bg-paper-2 px-4 py-2 font-mono text-[11px] text-sepia">
            {t.session.model && <span className="text-ink-2">{t.session.model}</span>}
            {t.session.cwd && <span>{t.session.cwd}</span>}
            {t.session.git_branch && <span>⎇ {t.session.git_branch}</span>}
            <span>{t.session.tokens.input + t.session.tokens.output} tokens</span>
            <span className="uppercase tracking-widest">{t.source.agent}</span>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <div className="flex overflow-hidden rounded-lg border border-rule">
              <button
                onClick={() => setDialog(true)}
                className={`flex items-center gap-1.5 px-2.5 py-1 ${
                  dialog ? 'bg-accent/15 text-accent-2' : 'bg-paper-2 text-sepia hover:text-ink'
                }`}
              >
                <MessagesSquare size={12} /> dialog
              </button>
              <button
                onClick={() => setDialog(false)}
                className={`flex items-center gap-1.5 px-2.5 py-1 ${
                  !dialog ? 'bg-accent/15 text-accent-2' : 'bg-paper-2 text-sepia hover:text-ink'
                }`}
              >
                <ScrollText size={12} /> full transcript
              </button>
            </div>
            {!dialog && noiseCount > 0 && (
              <button
                onClick={() => setShowHidden((v) => !v)}
                className="flex items-center gap-1.5 rounded-lg border border-rule bg-paper-2 px-2.5 py-1 text-sepia hover:text-ink"
              >
                <EyeOff size={12} />
                {showHidden
                  ? 'hide injected/system messages'
                  : `${noiseCount} injected/system messages hidden — show`}
              </button>
            )}
            {t.grading_available && !t.grading && (
              <button
                onClick={() => setGrade((g) => ({ on: true, fresh: false, n: g.n + 1 }))}
                disabled={q.isFetching}
                className="flex items-center gap-1.5 rounded-lg border border-accent/40 bg-accent/10 px-2.5 py-1 text-accent-2 hover:bg-accent/20 disabled:opacity-50"
              >
                <Sparkles size={12} />
                {q.isFetching && grade.on ? 'grading…' : 'grade relevance with llm'}
              </button>
            )}
            {t.grading && !t.grading.error && (
              <span className="flex items-center gap-2 text-sepia">
                <span>
                  ✓ graded{t.grading.cached ? ' (cached)' : ''} — {t.grading.graded_messages}{' '}
                  marked
                </span>
                <button
                  onClick={() => setGrade((g) => ({ on: true, fresh: true, n: g.n + 1 }))}
                  disabled={q.isFetching}
                  className="text-accent-2 hover:underline disabled:opacity-50"
                >
                  {q.isFetching ? 'grading…' : 're-grade'}
                </button>
              </span>
            )}
            {t.grading?.error && (
              <span className="text-red-800/80">grading failed: {t.grading.error}</span>
            )}
          </div>
          {t.truncated && (
            <div className="rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs text-accent-2">
              transcript truncated at {t.messages.length} messages
            </div>
          )}
          {items.map((it) => {
            if (it.kind === 'message') {
              return (
                <MessageBlock
                  key={it.index}
                  message={t.messages[it.index]}
                  dialog={dialog}
                  showHidden={showHidden}
                  onOpenSubagent={(id) => setStack((s) => [...s, { id, agent: t.source.agent }])}
                />
              )
            }
            const stubKey = `${dialog ? 'd' : 'f'}-${it.start}`
            const isOpen = (!dialog && showHidden) || expanded.has(stubKey)
            if (!isOpen) {
              return (
                <button
                  key={stubKey}
                  onClick={() => setExpanded((s) => new Set(s).add(stubKey))}
                  className="block w-full rounded-lg border border-dashed border-rule/70 px-3 py-1.5 text-left font-mono text-[11px] text-sepia/80 hover:bg-paper-2"
                >
                  ▸ {stubLabel(it, dialog)}
                </button>
              )
            }
            return it.indices.map((mi) => (
              <div key={`${stubKey}-${mi}`} className="opacity-60">
                <MessageBlock
                  message={t.messages[mi]}
                  showHidden
                  onOpenSubagent={(id) => setStack((s) => [...s, { id, agent: t.source.agent }])}
                />
              </div>
            ))
          })}
        </>
      )}
    </div>
  )
}
