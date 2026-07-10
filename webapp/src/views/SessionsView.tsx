import { useState } from 'react'
import { EmptyState } from '../components/EmptyState'
import { useConnection } from '../connection/ConnectionContext'
import { useFanout } from '../lib/fanout'
import type { SessionEntry, VouchConnectionInfo } from '../lib/types'
import { TranscriptView } from './TranscriptView'

interface Row {
  conn: VouchConnectionInfo
  label: string
  s: SessionEntry
}

export function SessionsView() {
  const { hasMethod } = useConnection()
  const sessions = useFanout<{ sessions: SessionEntry[] }>(['sessions'], 'kb.list_sessions', {}, {
    refetchInterval: 10_000,
  })
  const rows: Row[] = sessions.rows.flatMap((r) =>
    (r.data?.sessions ?? []).map((s) => ({ conn: r.project.conn, label: r.project.label, s })),
  )
  const [sel, setSel] = useState<Row | null>(null)

  return (
    <div className="flex h-full">
      <aside className="w-72 shrink-0 overflow-y-auto border-r border-rule">
        {rows.length === 0 ? (
          <div className="p-4">
            <EmptyState title="No sessions" hint="Captured agent sessions will appear here." />
          </div>
        ) : (
          <ul>
            {rows.map((row, i) => {
              const openable = !!row.s.session_id && hasMethod('kb.session_transcript', row.conn.endpoint)
              const active =
                sel?.s.session_id === row.s.session_id && sel?.conn.endpoint === row.conn.endpoint
              return (
                <li key={`${row.conn.endpoint}-${row.s.session_id ?? i}`}>
                  <button
                    disabled={!openable}
                    onClick={() => setSel(row)}
                    className={`block w-full border-b border-rule/60 px-4 py-2.5 text-left transition ${
                      active ? 'bg-paper-3' : 'hover:bg-paper-2'
                    } ${openable ? '' : 'cursor-not-allowed opacity-50'}`}
                  >
                    <div className="truncate text-sm text-ink">
                      {row.s.title ?? row.s.session_id ?? 'untitled session'}
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 font-mono text-[10px] text-sepia">
                      <span className="uppercase">{row.s.stage}</span>
                      {row.s.observations != null && <span>{row.s.observations} obs</span>}
                    </div>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </aside>
      <section className="min-w-0 flex-1 overflow-y-auto">
        {sel && sel.s.session_id ? (
          <TranscriptView conn={sel.conn} sessionId={sel.s.session_id} />
        ) : (
          <div className="p-6">
            <EmptyState title="Select a session" hint="Pick a session to view its full transcript." />
          </div>
        )}
      </section>
    </div>
  )
}
