// EmptyState.tsx — "Open a knowledge base" card with Open/Initialize buttons and recents.
// Faithful port of emptyState()/paintRecents() in app.js:326-345.
// Class names: empty empty-card empty-actions recents recent btn ghost muted small
import { useEffect, useState } from 'react'
import type { RecentRoot } from '../../../shared/ipc'
import * as api from '../lib/client'

function basename(p: string): string {
  return (p || '').replace(/\/+$/, '').split('/').pop() || p
}

interface Props {
  onOpen: (root: string) => void
  onInitKb: () => void
}

export default function EmptyState({ onOpen, onInitKb }: Props) {
  const [recents, setRecents] = useState<RecentRoot[]>([])

  useEffect(() => {
    api.recentRoots().then((rs) => setRecents(rs)).catch(() => {/* noop */})
  }, [])

  async function handleOpen() {
    try {
      const dir = await api.pickKb()
      if (dir) onOpen(dir)
    } catch {
      // noop — dialog was cancelled or error
    }
  }

  return (
    <div className="empty">
      <div className="empty-card">
        <h1>Open a knowledge base</h1>
        <p className="muted">
          choose a folder that contains a <code>.vouch/</code> directory, or
          initialize a new one.
        </p>
        <div className="empty-actions">
          <button className="btn" onClick={() => void handleOpen()}>
            Open existing…
          </button>
          <button className="btn ghost" onClick={onInitKb}>
            Initialize new…
          </button>
        </div>
        {recents.length > 0 && (
          <div className="recents">
            <h3 className="muted small">recent</h3>
            {recents.map((r) => (
              <button
                key={r.root}
                className="recent"
                onClick={() => onOpen(r.root)}
              >
                <strong>{basename(r.root)}</strong>
                <span className="muted small">{r.root}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
