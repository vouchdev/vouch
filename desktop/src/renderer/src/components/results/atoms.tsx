// atoms.tsx — small result-render helper components.
//
// Ported from src/renderer/lib/result-render.js:56-60,223-241
// (pill, idLink, confidenceBar, metaRow, linkChips, idOf).
//
// KB content goes through React text children (JSX escapes it) — never
// dangerouslySetInnerHTML. Behavior is an exact faithful port of the JS source.

import { truncate, timeAgo } from '../../lib/format'

// ---------------------------------------------------------------------------
// OnOpen — the callback type used throughout the result renderers
// ---------------------------------------------------------------------------
export type OnOpen = (kind: string, id: string) => void

// ---------------------------------------------------------------------------
// idOf — mirrors result-render.js:241
// ---------------------------------------------------------------------------
export function idOf(x: unknown): string {
  if (typeof x === 'string') return x
  if (x && typeof x === 'object') {
    const o = x as Record<string, unknown>
    const v = o.id || o.source_id || o.locator
    if (v != null) return String(v)
  }
  return JSON.stringify(x)
}

// ---------------------------------------------------------------------------
// Pill — mirrors result-render.js:56
// ---------------------------------------------------------------------------
interface PillProps {
  text: string
  cls?: string
}

export function Pill({ text, cls = '' }: PillProps): JSX.Element {
  return <span className={`pill ${cls}`.trimEnd()}>{text}</span>
}

// ---------------------------------------------------------------------------
// IdLink — mirrors result-render.js:57-60
// ---------------------------------------------------------------------------
interface IdLinkProps {
  kind: string
  id: string | null | undefined
  onOpen: OnOpen
}

export function IdLink({ kind, id, onOpen }: IdLinkProps): JSX.Element | null {
  if (!id) return null
  return (
    <a
      className="idlink"
      title={`open ${kind} ${id}`}
      onClick={() => onOpen(kind, id)}
    >
      {id}
    </a>
  )
}

// ---------------------------------------------------------------------------
// ConfidenceBar — mirrors result-render.js:223-226
// ---------------------------------------------------------------------------
interface ConfidenceBarProps {
  v: number
}

export function ConfidenceBar({ v }: ConfidenceBarProps): JSX.Element {
  return (
    <div className="confbar" title={`confidence ${v}`}>
      <div className="confbar-fill" style={{ width: `${Math.round(v * 100)}%` }} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// MetaRow — mirrors result-render.js:227-235
// ---------------------------------------------------------------------------
interface MetaRowProps {
  o: Record<string, unknown>
}

export function MetaRow({ o }: MetaRowProps): JSX.Element | null {
  const parts: string[] = []
  if (o.approved_by) parts.push('approved by ' + String(o.approved_by))
  if (o.created_at) parts.push('created ' + timeAgo(o.created_at as string))
  if (o.updated_at && o.updated_at !== o.created_at)
    parts.push('updated ' + timeAgo(o.updated_at as string))
  if (o.last_confirmed_at)
    parts.push('confirmed ' + timeAgo(o.last_confirmed_at as string))
  if (!parts.length) return null
  return (
    <div className="card-meta">
      {parts.map((p, i) => (
        <span key={i}>{p}</span>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// LinkChips — mirrors result-render.js:236-240
// ---------------------------------------------------------------------------
interface LinkChipsProps {
  label: string
  ids: unknown[] | null | undefined
  kind: string
  onOpen: OnOpen
  cls?: string
  truncateAt?: number
}

export function LinkChips({ label, ids, kind, onOpen, cls = 'chip', truncateAt = 36 }: LinkChipsProps): JSX.Element | null {
  if (!ids || !ids.length) return null
  return (
    <div className="chips">
      <span className="chips-label">{label}</span>
      {ids.map((id, i) => (
        <span
          key={i}
          className={cls}
          onClick={() => onOpen(kind, idOf(id))}
        >
          {truncate(idOf(id), truncateAt)}
        </span>
      ))}
    </div>
  )
}
