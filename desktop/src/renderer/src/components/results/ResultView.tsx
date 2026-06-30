// ResultView.tsx — top-level result dispatcher.
//
// Reproduces renderResult's shape detection exactly (result-render.js:6-25):
//   array → cards or JsonTree
//   hits+backend → SearchResults
//   items+quality → ContextPackView
//   gaps|synthesis_confidence|answer → SynthesisView
//   events → AuditTimeline
//   findings+counts → FindingsView
//   format+graph → GraphCodeView
//   volunteers → JsonTree
//   else kindOf → card
//   else proposal shape → ProposeResult
//   else → JsonTree
//
// KB content goes through React text children (JSX escapes it) — never
// dangerouslySetInnerHTML. Behavior is an exact faithful port of the JS source.

import { type OnOpen } from './atoms'
import { card } from './Cards'
import { ProposeResult } from './Cards'
import { JsonTree } from './JsonTree'
import {
  kindOf,
  RenderArray,
  SearchResults,
  ContextPackView,
  SynthesisView,
  AuditTimeline,
  FindingsView,
  GraphCodeView,
  type SearchResultShape,
  type ContextPackShape,
  type SynthesisShape,
  type FindingsShape,
  type GraphCodeShape,
} from './Renderers'

// ---------------------------------------------------------------------------
// ResultView — top-level dispatcher mirrors result-render.js:6-24
// ---------------------------------------------------------------------------
interface ResultViewProps {
  result: unknown
  onOpen: OnOpen
}

export function ResultView({ result, onOpen }: ResultViewProps): JSX.Element {
  if (result == null) {
    return <p className="muted">ok (no content)</p>
  }

  if (Array.isArray(result)) {
    return <RenderArray arr={result} onOpen={onOpen} />
  }

  if (typeof result === 'object') {
    const o = result as Record<string, unknown>

    // searchResults: hits array + backend key
    if (Array.isArray(o.hits) && 'backend' in o) {
      return (
        <SearchResults
          res={o as unknown as SearchResultShape}
          onOpen={onOpen}
        />
      )
    }

    // contextPack: items array + quality key
    if (Array.isArray(o.items) && o.quality) {
      return <ContextPackView pack={o as unknown as ContextPackShape} />
    }

    // synthesis: gaps, synthesis_confidence, or answer present
    if (o.gaps !== undefined || o.synthesis_confidence !== undefined || o.answer !== undefined) {
      return <SynthesisView s={o as SynthesisShape} />
    }

    // auditTimeline: events array
    if (Array.isArray(o.events)) {
      return <AuditTimeline res={o as { events?: Array<Record<string, unknown>> }} />
    }

    // findings: findings array + counts
    if (Array.isArray(o.findings) && o.counts) {
      return <FindingsView res={o as FindingsShape} />
    }

    // graphCode: format + graph
    if (o.format && o.graph) {
      return <GraphCodeView res={o as GraphCodeShape} />
    }

    // volunteers — json fallback (result-render.js:19)
    if (o.volunteers) {
      return <JsonTree value={o} />
    }

    // typed card dispatch
    const k = kindOf(o)
    if (k) return card(k, o, onOpen)

    // proposeResult shape (result-render.js:22)
    if (o.proposal_id || (o.kind && o.status)) {
      return <ProposeResult r={o} onOpen={onOpen} />
    }
  }

  return <JsonTree value={result} />
}

export default ResultView

// Alias used by MethodCard (mirrors the named export from the JS source)
export { ResultView as renderResult }
