// registry.tsx — maps view ids to their React components.
//
// 7 generic views → GenericView (renders a MethodCard per method in that view).
// 3 bespoke views → Placeholder for now; Tasks 6.2-6.4 replace these entries.
//
// Usage:
//   const ViewComponent = registry[state.view] ?? registry['dashboard']
//   <ViewComponent onOpen={onOpen} />

import type { FC } from 'react'
import GenericView from './GenericView'
import Dashboard from './Dashboard'
import Review from './Review'
import DualSolve from './DualSolve'
import type { OnOpen } from '../components/MethodCard'

// Common props shared by all view components
export interface ViewProps {
  onOpen: OnOpen
}

// Wrap GenericView for a specific view id to satisfy the FC<ViewProps> signature
function makeGenericView(view: string): FC<ViewProps> {
  const Component: FC<ViewProps> = ({ onOpen }) => (
    <GenericView view={view} onOpen={onOpen} />
  )
  Component.displayName = `GenericView(${view})`
  return Component
}

// Placeholder wrapper so bespoke views match FC<ViewProps>
const DashboardView: FC<ViewProps> = () => <Dashboard />
DashboardView.displayName = 'DashboardView'

const ReviewView: FC<ViewProps> = ({ onOpen }) => <Review onOpen={onOpen} />
ReviewView.displayName = 'ReviewView'

const DualSolveView: FC<ViewProps> = () => <DualSolve />
DualSolveView.displayName = 'DualSolveView'

// The registry
export const registry: Record<string, FC<ViewProps>> = {
  // 7 generic views
  search:          makeGenericView('search'),
  browse:          makeGenericView('browse'),
  propose:         makeGenericView('propose'),
  sessions:        makeGenericView('sessions'),
  graph:           makeGenericView('graph'),
  maintenance:     makeGenericView('maintenance'),
  'export-import': makeGenericView('export-import'),
  audit:           makeGenericView('audit'),

  // 3 bespoke views — replaced by Tasks 6.2-6.4
  dashboard:       DashboardView,
  review:          ReviewView,
  'dual-solve':    DualSolveView,
}
