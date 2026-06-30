// Cards.tsx — claim/page/entity/relation/source/proposal cards + dispatcher.
//
// Ported 1:1 from src/renderer/lib/result-render.js:46-154
// (card, claimCard, pageCard, entityCard, relationRow, sourceCard, proposalCard,
// proposeResult).
//
// KB content goes through React text children (JSX escapes it) — never
// dangerouslySetInnerHTML. Behavior is an exact faithful port of the JS source.

import { type ReactNode } from 'react'
import { truncate, timeAgo } from '../../lib/format'
import {
  Pill,
  IdLink,
  ConfidenceBar,
  MetaRow,
  LinkChips,
  idOf,
  type OnOpen,
} from './atoms'
import { JsonTree } from './JsonTree'

// ---------------------------------------------------------------------------
// card — dispatcher mirroring result-render.js:46-54
// ---------------------------------------------------------------------------
export function card(kind: string, o: Record<string, unknown>, onOpen: OnOpen): JSX.Element {
  if (kind === 'claim')    return <ClaimCard    c={o} onOpen={onOpen} />
  if (kind === 'page')     return <PageCard     p={o} onOpen={onOpen} />
  if (kind === 'entity')   return <EntityCard   e={o} onOpen={onOpen} />
  if (kind === 'relation') return <RelationRow  r={o} onOpen={onOpen} />
  if (kind === 'source')   return <SourceCard   s={o} onOpen={onOpen} />
  if (kind === 'proposal') return <ProposalCard p={o} onOpen={onOpen} />
  return <JsonTree value={o} />
}

// ---------------------------------------------------------------------------
// ClaimCard — mirrors result-render.js:62-79
// ---------------------------------------------------------------------------
interface ClaimCardProps {
  c: Record<string, unknown>
  onOpen: OnOpen
}

export function ClaimCard({ c, onOpen }: ClaimCardProps): JSX.Element {
  const ev = (c.evidence as unknown[] | undefined) || (c.citations as unknown[] | undefined) || []
  const entities = (c.entities as unknown[] | undefined) || []
  return (
    <div className="card claim">
      <div className="card-head">
        <Pill text="claim" cls="k-claim" />
        {c.claim_type ? <Pill text={String(c.claim_type)} cls="t" /> : null}
        {c.status ? <Pill text={String(c.status)} cls={`s-${c.status}`} /> : null}
        {c.scope ? <Pill text={String(c.scope)} cls="scope" /> : null}
        <span className="spacer" />
        <code className="cid">{String(c.id ?? '')}</code>
      </div>
      <p className="claim-text">{String(c.text ?? '')}</p>
      {typeof c.confidence === 'number' ? <ConfidenceBar v={c.confidence} /> : null}
      {ev.length ? (
        <div className="chips">
          <span className="chips-label">evidence</span>
          {ev.map((e, i) => (
            <span
              key={i}
              className="chip mono"
              onClick={() => onOpen('source', idOf(e))}
            >
              {truncate(idOf(e), 40)}
            </span>
          ))}
        </div>
      ) : null}
      {entities.length ? (
        <div className="chips">
          <span className="chips-label">entities</span>
          {entities.map((e, i) => (
            <span
              key={i}
              className="chip"
              onClick={() => onOpen('entity', idOf(e))}
            >
              {idOf(e)}
            </span>
          ))}
        </div>
      ) : null}
      <MetaRow o={c} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// PageCard — mirrors result-render.js:81-91
// ---------------------------------------------------------------------------
interface PageCardProps {
  p: Record<string, unknown>
  onOpen: OnOpen
}

export function PageCard({ p, onOpen }: PageCardProps): JSX.Element {
  return (
    <div className="card page">
      <div className="card-head">
        <Pill text="page" cls="k-page" />
        {p.type ? <Pill text={String(p.type)} cls="t" /> : null}
        {p.status ? <Pill text={String(p.status)} cls={`s-${p.status}`} /> : null}
        <span className="spacer" />
        <code className="cid">{String(p.id ?? '')}</code>
      </div>
      <h3 className="page-title">{String(p.title ?? p.id ?? '')}</h3>
      {p.body ? (
        <pre className="page-body">{truncate(String(p.body), 2000)}</pre>
      ) : null}
      <LinkChips
        label="claims"
        ids={p.claims as unknown[] | undefined}
        kind="claim"
        onOpen={onOpen}
      />
      <LinkChips
        label="entities"
        ids={p.entities as unknown[] | undefined}
        kind="entity"
        onOpen={onOpen}
      />
      <LinkChips
        label="sources"
        ids={p.sources as unknown[] | undefined}
        kind="source"
        onOpen={onOpen}
      />
      <MetaRow o={p} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// EntityCard — mirrors result-render.js:93-103
// ---------------------------------------------------------------------------
interface EntityCardProps {
  e: Record<string, unknown>
  onOpen: OnOpen
}

export function EntityCard({ e, onOpen }: EntityCardProps): JSX.Element {
  const aliases = e.aliases as string[] | undefined
  return (
    <div className="card entity">
      <div className="card-head">
        <Pill text="entity" cls="k-entity" />
        {e.type ? <Pill text={String(e.type)} cls="t" /> : null}
        <span className="spacer" />
        <code className="cid">{String(e.id ?? '')}</code>
      </div>
      <h3>{String(e.name ?? e.id ?? '')}</h3>
      {aliases && aliases.length ? (
        <div className="muted small">{'aka ' + aliases.join(', ')}</div>
      ) : null}
      {e.description ? <p>{String(e.description)}</p> : null}
      <div className="card-actions">
        <button
          className="btn ghost sm"
          onClick={() => onOpen('__neighbors', String(e.id ?? ''))}
        >
          show neighbors
        </button>
      </div>
      <MetaRow o={e} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// RelationRow — mirrors result-render.js:105-111
// ---------------------------------------------------------------------------
interface RelationRowProps {
  r: Record<string, unknown>
  onOpen: OnOpen
}

export function RelationRow({ r, onOpen }: RelationRowProps): JSX.Element {
  return (
    <div className="card relation">
      <div className="rel-line">
        <IdLink kind="node" id={r.source as string | null | undefined} onOpen={onOpen} />
        <span className="rel-arrow">{`—[${r.relation}]→`}</span>
        <IdLink kind="node" id={r.target as string | null | undefined} onOpen={onOpen} />
        {typeof r.confidence === 'number' ? (
          <Pill text={(r.confidence as number).toFixed(2)} cls="conf" />
        ) : null}
      </div>
      <MetaRow o={r} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// SourceCard — mirrors result-render.js:113-123
// ---------------------------------------------------------------------------
interface SourceCardProps {
  s: Record<string, unknown>
  onOpen?: OnOpen
}

export function SourceCard({ s, onOpen: _onOpen }: SourceCardProps): JSX.Element {
  const parts: string[] = [
    s.media_type != null ? String(s.media_type) : '',
    s.size != null ? `${s.size} bytes` : '',
    s.hash ? `sha:${truncate(String(s.hash), 12)}` : '',
  ].filter(Boolean)

  return (
    <div className="card source">
      <div className="card-head">
        <Pill text="source" cls="k-source" />
        {s.source_type ? <Pill text={String(s.source_type)} cls="t" /> : null}
        <span className="spacer" />
        <code className="cid">{String(s.id ?? '')}</code>
      </div>
      <h3>{String(s.title ?? s.locator ?? s.id ?? '')}</h3>
      {s.locator ? (
        <div className="muted small mono">{String(s.locator)}</div>
      ) : null}
      <div className="muted small">{parts.join(' · ')}</div>
      <MetaRow o={s} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// ProposalCard — mirrors result-render.js:125-144
// ---------------------------------------------------------------------------
interface ProposalCardProps {
  p: Record<string, unknown>
  onOpen: OnOpen
  actions?: (p: Record<string, unknown>) => ReactNode
}

export function ProposalCard({ p, onOpen: _onOpen, actions }: ProposalCardProps): JSX.Element {
  const payload = (p.payload as Record<string, unknown>) || {}
  return (
    <div className="card proposal">
      <div className="card-head">
        <Pill text="proposal" cls="k-prop" />
        <Pill text={String(p.kind || payload.kind || '?')} cls="t" />
        {p.status ? <Pill text={String(p.status)} cls={`s-${p.status}`} /> : null}
        <span className="spacer" />
        <code className="cid">{String(p.proposal_id ?? p.id ?? '')}</code>
      </div>
      <div className="prop-body">
        {payload.text ? <p>{String(payload.text)}</p> : null}
        {payload.title ? <h3>{String(payload.title)}</h3> : null}
        {payload.name ? <h3>{String(payload.name)}</h3> : null}
        {p.rationale ? (
          <div className="muted small">{'rationale: ' + String(p.rationale)}</div>
        ) : null}
      </div>
      <div className="card-meta">
        {p.proposed_by ? <span>{'by ' + String(p.proposed_by)}</span> : null}
        {p.session_id ? <span>{'session ' + truncate(String(p.session_id), 18)}</span> : null}
        {p.proposed_at ? <span>{timeAgo(p.proposed_at as string)}</span> : null}
      </div>
      {actions ? actions(p) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// ProposeResult — mirrors result-render.js:146-154
// ---------------------------------------------------------------------------
interface ProposeResultProps {
  r: Record<string, unknown>
  onOpen: OnOpen
}

export function ProposeResult({ r, onOpen }: ProposeResultProps): JSX.Element {
  const warnings = r.warnings as string[] | undefined
  return (
    <div className="card good">
      <div className="card-head">
        <Pill text="proposed" cls="k-prop" />
        <Pill text={String(r.kind || '?')} cls="t" />
        {r.status ? <Pill text={String(r.status)} cls={`s-${r.status}`} /> : null}
      </div>
      <p>
        {'proposal created — it is '}
        <strong>pending review</strong>
        {'.'}
      </p>
      {r.proposal_id ? <div className="mono">{String(r.proposal_id)}</div> : null}
      {warnings && warnings.length ? (
        <ul className="warn">
          {warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      ) : null}
      <div className="card-actions">
        <button
          className="btn sm"
          onClick={() => onOpen('__view', 'review')}
        >
          go to review queue
        </button>
      </div>
    </div>
  )
}
