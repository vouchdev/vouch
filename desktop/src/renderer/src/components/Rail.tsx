// Rail.tsx — left navigation rail. Faithful port of shell()/renderNav() in app.js:52-80.
// Class names: rail brand brand-v brand-dot nav nav-item active dim nav-icon rail-foot
import { methods } from '../../../shared/methods.gen'
import { useVouch, isAvailable } from '../lib/VouchContext'

const VIEWS = [
  { id: 'dashboard',     label: 'Dashboard',         icon: '◈' },
  { id: 'search',        label: 'Search & Ask',       icon: '⌕' },
  { id: 'browse',        label: 'Browse',             icon: '▤' },
  { id: 'propose',       label: 'Propose',            icon: '✚' },
  { id: 'review',        label: 'Review & Lifecycle', icon: '⚖' },
  { id: 'sessions',      label: 'Sessions',           icon: '◷' },
  { id: 'graph',         label: 'Graph',              icon: '⌗' },
  { id: 'maintenance',   label: 'Maintenance',        icon: '✦' },
  { id: 'export-import', label: 'Export / Import',    icon: '⇄' },
  { id: 'audit',         label: 'Audit',              icon: '❰❱' },
  { id: 'dual-solve',    label: 'Dual-Solve',         icon: '⚔' },
]

export { VIEWS }

interface Props {
  onSwitchKb: () => void
}

export default function Rail({ onSwitchKb }: Props) {
  const { state, actions } = useVouch()

  return (
    <aside className="rail">
      <div className="brand">
        <span className="brand-v">V</span>
        <span className="brand-dot">·</span>
        ouch
      </div>

      <nav className="nav">
        {VIEWS.map((v) => {
          const ms = methods.filter((m) => m.view === v.id)
          // dual-solve is always enabled; otherwise dim when no method is advertised
          const avail =
            v.id === 'dual-solve' ? true : ms.some((m) => isAvailable(state, m.name))
          const active = state.view === v.id
          return (
            <button
              key={v.id}
              className={`nav-item${active ? ' active' : ''}${avail ? '' : ' dim'}`}
              onClick={() => actions.navigate(v.id)}
            >
              <span className="nav-icon">{v.icon}</span>
              <span>{v.label}</span>
            </button>
          )
        })}
      </nav>

      <div className="rail-foot">
        <button className="btn ghost sm" onClick={onSwitchKb}>
          Switch KB…
        </button>
      </div>
    </aside>
  )
}
