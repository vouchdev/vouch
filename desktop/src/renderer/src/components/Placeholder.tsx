// Placeholder.tsx — temporary view-body stub used until the real view registry
// is wired in Phase 6. Renders a simple "coming soon" note so typecheck passes
// and the shell is visually complete. Must NOT use view-head — App.tsx already
// renders that as a sibling above view-body.
interface Props {
  name: string
}

export default function Placeholder({ name: _name }: Props) {
  return (
    <p className="muted">view coming soon</p>
  )
}
