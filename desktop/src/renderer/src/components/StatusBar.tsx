// StatusBar.tsx — process up/down dot, pending badge, error, diagnostics link.
// Faithful port of renderStatus() in app.js:96-105.
// Class names: statusbar proc up down proc-dot pending-badge status-err spacer link
import * as api from '../lib/client'
import { useVouch } from '../lib/VouchContext'

interface Props {
  onShowDiagnostics: (info: Record<string, unknown>) => void
}

export default function StatusBar({ onShowDiagnostics }: Props) {
  const { state } = useVouch()

  async function handleDiagnostics() {
    try {
      const info = await api.procInfo()
      onShowDiagnostics(info)
    } catch {
      // noop
    }
  }

  return (
    <footer className="statusbar">
      <span className={`proc${state.jsonl ? ' up' : ' down'}`}>
        <span className="proc-dot" />
        {`vouch ${state.jsonl ? 'running' : 'down'}`}
      </span>
      {typeof state.pending === 'number' && (
        <span className="pending-badge">{state.pending} pending</span>
      )}
      {state.statusError && (
        <span className="status-err">{state.statusError}</span>
      )}
      <span className="spacer" />
      <button className="link sm" onClick={() => void handleDiagnostics()}>
        diagnostics
      </button>
    </footer>
  )
}
