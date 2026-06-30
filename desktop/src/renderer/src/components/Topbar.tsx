// Topbar.tsx — KB id + cap pills. Faithful port of renderTopbar() in app.js:82-94.
// Class names: topbar kb-id kb-dot live topbar-actions cap-pill gate
import { useVouch } from '../lib/VouchContext'

function basename(p: string): string {
  return (p || '').replace(/\/+$/, '').split('/').pop() || p
}

export default function Topbar() {
  const { state } = useVouch()

  return (
    <header className="topbar">
      <div className="kb-id">
        {state.root ? (
          <span>
            <span className="kb-dot live" />
            <strong>{basename(state.root)}</strong>
            <span className="muted small path">{state.root}</span>
          </span>
        ) : (
          <span className="muted">no knowledge base open</span>
        )}
      </div>
      <div className="topbar-actions">
        {state.caps && (
          <span
            className="cap-pill"
            title={(state.caps.methods ?? []).join(', ')}
          >
            {(state.caps.methods ?? []).length} methods
          </span>
        )}
        {state.caps?.review_gated && (
          <span className="cap-pill gate">review-gated</span>
        )}
      </div>
    </header>
  )
}
