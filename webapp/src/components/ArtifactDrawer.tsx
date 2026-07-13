import { useQuery } from '@tanstack/react-query'
import { X } from 'lucide-react'
import type { ProjectState } from '../connection/ConnectionContext'
import { rpc } from '../lib/rpc'
import type { Citation, Claim, Entity, Page, Relation, WhyEdge, WhyResult } from '../lib/types'
import { ClaimLifecycleActions } from './ClaimLifecycleActions'
import { DeleteArtifactButton } from './DeleteArtifactButton'
import { ErrorCard } from './ErrorCard'
import { Markdown } from './Markdown'
import { useErrorToast } from './Toast'

export type DrawerTarget = { kind: 'claim' | 'page' | 'entity' | 'relation'; id: string } | null

const READ_METHOD: Record<string, { method: string; param: string }> = {
  claim: { method: 'kb.read_claim', param: 'claim_id' },
  page: { method: 'kb.read_page', param: 'page_id' },
  entity: { method: 'kb.read_entity', param: 'entity_id' },
  relation: { method: 'kb.read_relation', param: 'relation_id' },
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 border-b border-rule/60 py-2 text-sm">
      <span className="w-24 shrink-0 text-xs uppercase tracking-wide text-sepia">{label}</span>
      <span className="min-w-0 break-words text-ink-2">{children}</span>
    </div>
  )
}

function WhyTree({ edges, depth = 0 }: { edges: WhyEdge[]; depth?: number }) {
  return (
    <ul className="space-y-1">
      {edges.map((e, i) => (
        <li key={`${e.kind}-${e.target}-${i}`} style={{ paddingLeft: depth * 12 }}>
          <span className="font-mono text-xs text-accent-2">{e.kind}</span>{' '}
          <span className="font-mono text-xs text-ink-2">{e.target.slice(0, 24)}</span>{' '}
          <span className="text-xs text-sepia">({e.target_kind})</span>
          {e.children.length > 0 && <WhyTree edges={e.children} depth={depth + 1} />}
        </li>
      ))}
    </ul>
  )
}

export function ArtifactDrawer({
  target,
  project,
  onClose,
}: {
  target: DrawerTarget
  /** The project the artifact lives in — reads route to this endpoint. */
  project: ProjectState | null
  onClose: () => void
}) {
  const conn = project?.conn ?? null
  const endpoint = conn?.endpoint
  const has = (m: string) => project?.caps?.methods.includes(m) ?? false

  const read = READ_METHOD[target?.kind ?? 'claim']
  const artifact = useQuery({
    queryKey: ['artifact', endpoint, target?.kind, target?.id],
    queryFn: () => rpc<Record<string, unknown>>(conn!, read.method, { [read.param]: target!.id }),
    enabled: !!conn && !!target,
  })

  const isClaim = target?.kind === 'claim'
  const cite = useQuery({
    queryKey: ['cite', endpoint, target?.id],
    queryFn: () => rpc<Citation[]>(conn!, 'kb.cite', { claim_id: target!.id }),
    enabled: !!conn && isClaim && has('kb.cite'),
  })
  const why = useQuery({
    queryKey: ['why', endpoint, target?.id],
    queryFn: () => rpc<WhyResult>(conn!, 'kb.why', { claim_id: target!.id }),
    enabled: !!conn && isClaim && has('kb.why'),
  })
  useErrorToast(artifact.isError, artifact.error)

  if (!target || !project) return null

  const a = artifact.data

  return (
    <div data-testid="drawer" className="fixed inset-y-0 right-0 z-40 flex w-[28rem] max-w-full flex-col border-l border-rule bg-paper-2 shadow-2xl">
      <header className="flex items-start justify-between gap-3 border-b border-rule px-5 py-4">
        <div className="min-w-0">
          <span className="mr-2 rounded bg-accent/15 px-2 py-0.5 font-mono text-[10px] text-accent-2">
            {project.label}
          </span>
          <span className="rounded bg-paper-3 px-2 py-0.5 font-mono text-[10px] uppercase tracking-widest text-accent">
            {target.kind}
          </span>
          <p className="mt-1 break-all font-mono text-xs text-sepia">{target.id}</p>
        </div>
        <button aria-label="close" onClick={onClose} className="rounded-lg p-1.5 text-sepia hover:bg-paper-3 hover:text-ink">
          <X size={16} />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {artifact.isPending && <p className="text-sm text-sepia">loading…</p>}
        {artifact.isError && (
          <ErrorCard
            code={(artifact.error as { code?: string }).code}
            message={artifact.error instanceof Error ? artifact.error.message : 'failed to load'}
          />
        )}

        {a && target.kind === 'claim' && (
          <>
            <p className="mb-3 text-sm leading-relaxed text-ink">{(a as unknown as Claim).text}</p>
            <Row label="type">{String(a.type)}</Row>
            <Row label="status">{String(a.status)}</Row>
            <Row label="confidence">{Math.round(Number(a.confidence) * 100)}%</Row>
            {typeof a.created_at === 'string' && <Row label="created">{a.created_at.slice(0, 19).replace('T', ' ')}</Row>}
          </>
        )}
        {a && target.kind === 'page' && (
          <>
            <p className="mb-3 text-base font-semibold text-ink">{(a as unknown as Page).title}</p>
            <Row label="type">{String(a.type)}</Row>
            <Row label="status">{String(a.status)}</Row>
            <div className="mt-3">
              <Markdown>{(a as unknown as Page).body}</Markdown>
            </div>
          </>
        )}
        {a && target.kind === 'entity' && (
          <>
            <p className="mb-3 text-base font-semibold text-ink">{(a as unknown as Entity).name}</p>
            <Row label="type">{String(a.type)}</Row>
          </>
        )}
        {a && target.kind === 'relation' && (
          <>
            <Row label="source">
              <span className="font-mono text-xs">{(a as unknown as Relation).source}</span>
            </Row>
            <Row label="relation">{(a as unknown as Relation).relation}</Row>
            <Row label="target">
              <span className="font-mono text-xs">{(a as unknown as Relation).target}</span>
            </Row>
            <Row label="confidence">{Math.round(Number(a.confidence) * 100)}%</Row>
          </>
        )}

        {isClaim && cite.data && cite.data.length > 0 && (
          <section className="mt-5">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-sepia">Citations</h3>
            <ul className="space-y-1">
              {cite.data.map((c, i) => (
                <li key={i} className="rounded-lg border border-rule bg-paper px-3 py-2 text-xs text-ink-2">
                  {typeof c.title === 'string' && c.title !== '' ? c.title : null}{' '}
                  <span className="break-all font-mono text-sepia">
                    {typeof c.id === 'string' ? c.id.slice(0, 16) : JSON.stringify(c).slice(0, 60)}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}

        {isClaim && why.data && why.data.provenance.length > 0 && (
          <section className="mt-5">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-sepia">Why this claim exists</h3>
            <WhyTree edges={why.data.provenance} />
          </section>
        )}
      </div>

      {a && (
        <footer className="space-y-3 border-t border-rule px-5 py-4">
          {target.kind === 'claim' && (
            <ClaimLifecycleActions project={project} claimId={target.id} onDone={onClose} />
          )}
          <DeleteArtifactButton project={project} kind={target.kind} id={target.id} onDone={onClose} />
        </footer>
      )}
    </div>
  )
}
