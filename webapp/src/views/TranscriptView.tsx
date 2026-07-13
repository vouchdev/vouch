import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { EmptyState } from '../components/EmptyState'
import { ErrorCard } from '../components/ErrorCard'
import { MessageBlock } from '../components/transcript/MessageBlock'
import { VouchRpcError } from '../lib/rpc'
import { fetchTranscript } from '../lib/transcript'
import type { Observation } from '../lib/transcript'
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
  const top = stack[stack.length - 1]
  const q = useQuery({
    queryKey: ['transcript', conn.endpoint, top.id],
    queryFn: () => fetchTranscript(conn, top.id, top.agent),
  })

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
  const t = q.data
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
          {t.truncated && (
            <div className="rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs text-accent-2">
              transcript truncated at {t.messages.length} messages
            </div>
          )}
          {t.messages.map((m, i) => (
            <MessageBlock
              key={i}
              message={m}
              onOpenSubagent={(id) => setStack((s) => [...s, { id, agent: t.source.agent }])}
            />
          ))}
        </>
      )}
    </div>
  )
}
