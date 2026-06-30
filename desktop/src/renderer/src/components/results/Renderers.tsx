// Renderers.tsx — search/context/synthesis/audit/findings/graph result components.
//
// Ported 1:1 from src/renderer/lib/result-render.js:156-220
// (searchResults, contextPack, synthesis, auditTimeline, findings, graphCode).
// Also exports kindOf (result-render.js:28-36) and RenderArray (result-render.js:38-43).
//
// KB content goes through React text children (JSX escapes it) — never
// dangerouslySetInnerHTML. Behavior is an exact faithful port of the JS source.

import { cloneElement } from 'react'
import { truncate, timeAgo } from '../../lib/format'
import { Pill, type OnOpen } from './atoms'
import { card } from './Cards'
import { JsonTree } from './JsonTree'

// ---------------------------------------------------------------------------
// kindOf — mirrors result-render.js:28-36
// ---------------------------------------------------------------------------
export function kindOf(o: Record<string, unknown>): string | null {
  if (o.source && o.target && o.relation) return 'relation'
  if (o.id && (o.text !== undefined || o.claim_type !== undefined) && o.title === undefined)
    return 'claim'
  if (o.id && o.title !== undefined && o.body !== undefined) return 'page'
  if (o.id && o.type !== undefined && (o.aliases !== undefined || o.name !== undefined))
    return 'entity'
  if (
    o.id &&
    (o.locator !== undefined || o.media_type !== undefined || o.source_type !== undefined)
  )
    return 'source'
  if (o.proposal_id !== undefined && o.payload !== undefined) return 'proposal'
  return null
}

// ---------------------------------------------------------------------------
// RenderArray — mirrors result-render.js:38-43
// ---------------------------------------------------------------------------
export function RenderArray({
  arr,
  onOpen,
}: {
  arr: unknown[]
  onOpen: OnOpen
}): JSX.Element {
  if (arr.length === 0) return <p className="muted">no results</p>
  const first = arr[0]
  const k = typeof first === 'object' && first !== null ? kindOf(first as Record<string, unknown>) : null
  if (!k) return <JsonTree value={arr} />
  return (
    <div className="cards">
      {arr.map((o, i) =>
        cloneElement(card(k, o as Record<string, unknown>, onOpen), { key: i })
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// SearchResults — mirrors result-render.js:157-167
// ---------------------------------------------------------------------------
export interface SearchResultShape {
  hits: Array<{ kind?: string; id?: string; snippet?: string; score?: number }>
  backend?: string
  viewer?: { project?: string; agent?: string }
}

export function SearchResults({
  res,
  onOpen,
}: {
  res: SearchResultShape
  onOpen: OnOpen
}): JSX.Element {
  return (
    <div>
      <div className="result-head">
        <span>{res.hits.length} hits</span>
        <Pill text={`backend: ${res.backend ?? ''}`} cls="info" />
        {res.viewer ? (
          <span className="muted small">{`scope: ${res.viewer.project || '—'} / ${res.viewer.agent || '—'}`}</span>
        ) : null}
      </div>
      <div className="hits">
        {res.hits.map((hwit, i) => (
          <div
            key={i}
            className="hit"
            onClick={() => onOpen(hwit.kind ?? '', hwit.id ?? '')}
          >
            <Pill text={hwit.kind ?? ''} cls={`k-${hwit.kind ?? ''}`} />
            <code className="hit-id">{hwit.id ?? ''}</code>
            <span className="hit-snip">{hwit.snippet ?? ''}</span>
            {typeof hwit.score === 'number' ? (
              <span className="hit-score">{hwit.score.toFixed(3)}</span>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// ContextPackView — mirrors result-render.js:169-180
// ---------------------------------------------------------------------------
interface ContextPackItem {
  type?: string
  backend?: string
  score?: number
  summary?: string
  text?: string
  id?: string
}

export interface ContextPackShape {
  items: ContextPackItem[]
  quality?: { ok?: boolean }
  warnings?: string[]
}

export function ContextPackView({ pack }: { pack: ContextPackShape }): JSX.Element {
  const q = pack.quality || {}
  return (
    <div>
      <div className="result-head">
        <span>{pack.items.length} context items</span>
        <Pill text={q.ok ? 'quality ok' : 'quality warn'} cls={q.ok ? 'good' : 'warn'} />
      </div>
      {pack.warnings && pack.warnings.length ? (
        <ul className="warn">
          {pack.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      ) : null}
      <div className="cards">
        {pack.items.map((it, i) => (
          <div key={i} className="card ctx">
            <div className="card-head">
              <Pill text={it.type || 'item'} cls="t" />
              <span className="spacer" />
              {it.backend ? <Pill text={it.backend} cls="info" /> : null}
              {typeof it.score === 'number' ? (
                <Pill text={it.score.toFixed(3)} cls="conf" />
              ) : null}
            </div>
            <p>{it.summary || it.text || ''}</p>
            <code className="cid">{it.id || ''}</code>
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// SynthesisView — mirrors result-render.js:182-189
// ---------------------------------------------------------------------------
export interface SynthesisShape {
  answer?: string
  prose?: string
  text?: string
  synthesis_confidence?: number | string
  gaps?: unknown
}

export function SynthesisView({ s }: { s: SynthesisShape }): JSX.Element {
  const prose = s.answer || s.prose || s.text || ''
  return (
    <div className="synthesis">
      {s.synthesis_confidence != null ? (
        <div className="result-head">
          <Pill text={`confidence: ${s.synthesis_confidence}`} cls="info" />
        </div>
      ) : null}
      <div className="synth-prose">{prose}</div>
      {s.gaps ? (
        <div className="gaps">
          <h4>gaps</h4>
          {Array.isArray(s.gaps) ? (
            <ul>
              {(s.gaps as unknown[]).map((g, i) => (
                <li key={i}>{typeof g === 'string' ? g : JSON.stringify(g)}</li>
              ))}
            </ul>
          ) : (
            <p>{String(s.gaps)}</p>
          )}
        </div>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// AuditTimeline — mirrors result-render.js:192-201
// ---------------------------------------------------------------------------
interface AuditEvent {
  event?: string
  actor?: string
  object_ids?: string[]
  reason?: string
  at?: string
  ts?: string
}

export function AuditTimeline({ res }: { res: { events?: AuditEvent[] } }): JSX.Element {
  const events = res.events || []
  if (!events.length) return <p className="muted">no audit events</p>
  return (
    <div className="timeline">
      {events
        .slice()
        .reverse()
        .map((e, i) => (
          <div key={i} className="tl-row">
            <span className="tl-event">{e.event || ''}</span>
            <span className="tl-actor">{e.actor || ''}</span>
            <span className="tl-objs">{(e.object_ids || []).join(', ')}</span>
            {e.reason ? <span className="tl-reason muted">{e.reason}</span> : null}
            <span className="tl-at muted">{timeAgo(e.at || e.ts || '')}</span>
          </div>
        ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// FindingsView — mirrors result-render.js:203-213
// ---------------------------------------------------------------------------
interface Finding {
  severity?: string
  code?: string
  message?: string
  object_ids?: string[]
}

export interface FindingsShape {
  ok?: boolean
  counts?: Record<string, unknown>
  findings?: Finding[]
}

export function FindingsView({ res }: { res: FindingsShape }): JSX.Element {
  const f = res.findings || []
  return (
    <div>
      <div className="result-head">
        <Pill text={res.ok ? 'ok' : 'issues found'} cls={res.ok ? 'good' : 'warn'} />
        {Object.entries(res.counts || {}).map(([k, v]) => (
          <span key={k} className="muted small">{`${k}: ${v}`}</span>
        ))}
      </div>
      {f.length ? (
        <table className="tbl">
          <thead>
            <tr>
              <th>severity</th>
              <th>code</th>
              <th>message</th>
              <th>objects</th>
            </tr>
          </thead>
          <tbody>
            {f.map((x, i) => (
              <tr key={i} className={`sev-${x.severity ?? ''}`}>
                <td>{x.severity ?? ''}</td>
                <td>{x.code ?? ''}</td>
                <td>{x.message ?? ''}</td>
                <td className="mono">{(x.object_ids || []).join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">no findings</p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// GraphCodeView — mirrors result-render.js:215-220
// ---------------------------------------------------------------------------
export interface GraphCodeShape {
  format?: string
  graph?: string
}

export function GraphCodeView({ res }: { res: GraphCodeShape }): JSX.Element {
  function handleCopy() {
    if (navigator.clipboard && res.graph) {
      void navigator.clipboard.writeText(res.graph)
    }
  }
  return (
    <div>
      <div className="result-head">
        <Pill text={res.format ?? ''} cls="info" />
        <button className="btn ghost sm" onClick={handleCopy}>
          copy
        </button>
      </div>
      <pre className="graph-code mono">{res.graph ?? ''}</pre>
    </div>
  )
}
