// GenericView.tsx — generic view renderer: header + a MethodCard per method in
// the view. Faithful port of renderView()'s generic branch in app.js:130-133.
// Class names: view-head view-body (view-head is rendered by App.tsx above us;
// we render directly into view-body).

import { methods } from '../../../shared/methods.gen'
import MethodCard from '../components/MethodCard'
import type { OnOpen } from '../components/MethodCard'
import { viewBlurb } from './blurbs'
import { VIEWS } from '../components/Rail'

interface Props {
  view: string
  onOpen: OnOpen
}

export default function GenericView({ view, onOpen }: Props) {
  const ms = methods.filter((m) => m.view === view)

  if (!ms.length) {
    return <p className="muted">nothing here</p>
  }

  return (
    <>
      {ms.map((m) => (
        <MethodCard key={m.name} method={m} onOpen={onOpen} />
      ))}
    </>
  )
}

// Helper used by the view-head in App.tsx to get the label for the current view
export function viewLabel(id: string): string {
  return VIEWS.find((v) => v.id === id)?.label ?? id
}

export { viewBlurb }
