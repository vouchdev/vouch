import { Trash2 } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { useConnection } from './ConnectionContext'

const DOT: Record<string, string> = {
  ok: 'bg-ok',
  down: 'bg-accent',
  connecting: 'bg-sepia animate-pulse',
}

/**
 * Connect / manage-projects dialog. With no projects (or after a 401) it is
 * the blocking connect gate; opened from the Shell pill it manages the list —
 * every connected endpoint with health and a remove button, plus the add form.
 */
export function ConnectDialog({ onClose }: { onClose?: () => void }) {
  const { projects, connect, removeProject, needsAuth } = useConnection()
  const [endpoint, setEndpoint] = useState('http://127.0.0.1:8731')
  const [label, setLabel] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await connect({
        endpoint: endpoint.trim().replace(/\/+$/, ''),
        token: token || undefined,
        label: label.trim() || undefined,
      })
      setLabel('')
      setToken('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-[28rem] max-w-full rounded-2xl border border-rule bg-paper-2 p-6 shadow-2xl">
        <div className="mb-1 font-mono text-xs tracking-widest text-accent">VOUCH</div>
        <h1 className="mb-1 text-lg font-semibold text-ink">
          {projects.length === 0 ? 'Connect to your knowledge base' : 'Projects'}
        </h1>
        <p className="mb-5 text-sm text-sepia">
          {needsAuth
            ? 'An endpoint rejected the credential — re-add it with a valid bearer token.'
            : projects.length === 0
              ? 'Point the console at a running `vouch serve --transport http` endpoint.'
              : 'Each project is one vouch endpoint. Add several to review them all in one place.'}
        </p>

        {projects.length > 0 && (
          <ul className="mb-5 space-y-2">
            {projects.map((p) => (
              <li
                key={p.conn.endpoint}
                className="flex items-center gap-3 rounded-lg border border-rule bg-paper px-3 py-2"
              >
                <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[p.health]}`} />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-ink">{p.label}</p>
                  <p className="truncate font-mono text-[11px] text-sepia">{p.conn.endpoint}</p>
                </div>
                <button
                  type="button"
                  aria-label={`remove ${p.label}`}
                  onClick={() => removeProject(p.conn.endpoint)}
                  className="rounded-lg p-1.5 text-sepia transition hover:bg-paper-3 hover:text-accent-2"
                >
                  <Trash2 size={14} />
                </button>
              </li>
            ))}
          </ul>
        )}

        <form onSubmit={onSubmit}>
          <label className="mb-1 block text-xs font-medium text-ink-2" htmlFor="endpoint">
            Endpoint
          </label>
          <input
            id="endpoint"
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder="http://127.0.0.1:8731"
            className="mb-4 w-full rounded-lg border border-rule bg-paper px-3 py-2 font-mono text-sm text-ink outline-none focus:border-accent"
          />

          <label className="mb-1 block text-xs font-medium text-ink-2" htmlFor="label">
            Project name <span className="text-sepia">(optional)</span>
          </label>
          <input
            id="label"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="my-project"
            className="mb-4 w-full rounded-lg border border-rule bg-paper px-3 py-2 text-sm text-ink outline-none focus:border-accent"
          />

          <label className="mb-1 block text-xs font-medium text-ink-2" htmlFor="token">
            Bearer token <span className="text-sepia">(optional)</span>
          </label>
          <input
            id="token"
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="mb-4 w-full rounded-lg border border-rule bg-paper px-3 py-2 font-mono text-sm text-ink outline-none focus:border-accent"
          />

          {error && (
            <p role="alert" className="mb-4 rounded-lg border border-accent/40 bg-accent/10 px-3 py-2 text-sm text-accent-2">
              {error}
            </p>
          )}

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy || endpoint.trim() === ''}
              className="flex-1 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-paper transition hover:bg-accent-2 disabled:opacity-50"
            >
              {busy ? 'Connecting…' : projects.length === 0 ? 'Connect' : 'Add project'}
            </button>
            {onClose && (
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg border border-rule px-4 py-2 text-sm text-ink-2 transition hover:bg-paper-3"
              >
                Done
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  )
}
