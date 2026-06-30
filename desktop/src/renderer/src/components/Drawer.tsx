// Drawer.tsx — controlled slide-in drawer with scrim + close button.
// Faithful port of openDrawer()/closeDrawer() in app.js:316-321.
// Class names: drawer-scrim open drawer drawer-close
import type { ReactNode } from 'react'

interface Props {
  open: boolean
  onClose: () => void
  children?: ReactNode
}

export default function Drawer({ open, onClose, children }: Props) {
  return (
    <>
      <div
        className={`drawer-scrim${open ? ' open' : ''}`}
        onClick={onClose}
      />
      <aside className={`drawer${open ? ' open' : ''}`}>
        <button className="drawer-close" onClick={onClose}>
          ×
        </button>
        {children}
      </aside>
    </>
  )
}
