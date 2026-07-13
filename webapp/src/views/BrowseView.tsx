import { useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArtifactDrawer } from '../components/ArtifactDrawer'
import type { DrawerTarget } from '../components/ArtifactDrawer'
import { EmptyState } from '../components/EmptyState'
import { ErrorCard } from '../components/ErrorCard'
import { useErrorToast } from '../components/Toast'
import { useConnection } from '../connection/ConnectionContext'
import type { ProjectState } from '../connection/ConnectionContext'
import { useFanout } from '../lib/fanout'
import type { Claim, Entity, Page, Relation } from '../lib/types'

type Kind = 'claim' | 'page' | 'entity' | 'relation'
const KINDS: readonly Kind[] = ['claim', 'page', 'entity', 'relation']
type Artifact = Claim | Page | Entity | Relation

const TABS: { kind: Kind; label: string; method: string }[] = [
  { kind: 'claim', label: 'Claims', method: 'kb.list_claims' },
  { kind: 'page', label: 'Pages', method: 'kb.list_pages' },
  { kind: 'entity', label: 'Entities', method: 'kb.list_entities' },
  { kind: 'relation', label: 'Relations', method: 'kb.list_relations' },
]

function rowText(kind: Kind, a: Artifact): string {
  if (kind === 'claim') return (a as Claim).text
  if (kind === 'page') return (a as Page).title
  if (kind === 'entity') return (a as Entity).name
  const r = a as Relation
  return `${r.source} —${r.relation}→ ${r.target}`
}

interface Row {
  project: ProjectState
  artifact: Artifact
}

export function BrowseView() {
  const { scoped, aggregated } = useConnection()
  const params = useParams<{ kind?: string; id?: string }>()
  const [search] = useSearchParams()
  const navigate = useNavigate()
  // In-app tab clicks intentionally don't navigate; the URL only drives the
  // deep-linked drawer. Initialize the tab from the URL's kind once.
  const [active, setActive] = useState<Kind>(() =>
    params.kind && KINDS.includes(params.kind as Kind) ? (params.kind as Kind) : 'claim',
  )
  const [filter, setFilter] = useState('')

  // The drawer is URL-driven: /browse/claim/<id>?p=<endpoint> deep-links to an
  // open drawer on that project (the ?p defaults to the only scoped project).
  const drawerProject =
    scoped.find((p) => p.conn.endpoint === search.get('p')) ?? (aggregated ? null : (scoped[0] ?? null))
  const drawer: DrawerTarget =
    params.kind && params.id && KINDS.includes(params.kind as Kind) && drawerProject
      ? { kind: params.kind as Kind, id: params.id }
      : null
  const openDrawer = (kind: Kind, id: string, project: ProjectState) => {
    const p = aggregated ? `?p=${encodeURIComponent(project.conn.endpoint)}` : ''
    navigate(`/browse/${kind}/${encodeURIComponent(id)}${p}`)
  }
  const closeDrawer = () => navigate('/browse')

  const claims = useFanout<Artifact[]>(['list', 'claim'], 'kb.list_claims')
  const pages = useFanout<Artifact[]>(['list', 'page'], 'kb.list_pages')
  const entities = useFanout<Artifact[]>(['list', 'entity'], 'kb.list_entities')
  const relations = useFanout<Artifact[]>(['list', 'relation'], 'kb.list_relations')
  const byKind = { claim: claims, page: pages, entity: entities, relation: relations }

  const current = byKind[active]
  useErrorToast(current.errors.length > 0, current.errors[0]?.error)
  const activeMethod = TABS.find((t) => t.kind === active)!.method
  const needle = filter.trim().toLowerCase()
  const all: Row[] = current.rows.flatMap((r) => r.data.map((artifact) => ({ project: r.project, artifact })))
  const rows = all.filter(
    ({ artifact }) =>
      needle === '' ||
      artifact.id.toLowerCase().includes(needle) ||
      rowText(active, artifact).toLowerCase().includes(needle),
  )
  const counts = Object.fromEntries(
    KINDS.map((k) => [k, byKind[k].rows.reduce((n, r) => n + r.data.length, 0)]),
  ) as Record<Kind, number>

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-4 border-b border-rule px-6 py-3" role="tablist">
        {TABS.map(({ kind, label }) => (
          <button
            key={kind}
            role="tab"
            aria-selected={active === kind}
            onClick={() => setActive(kind)}
            className={`rounded-lg px-3 py-1.5 text-sm transition ${
              active === kind ? 'bg-paper-3 text-ink' : 'text-ink-2 hover:text-ink'
            }`}
          >
            {label} ({counts[kind]})
          </button>
        ))}
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="filter…"
          className="ml-auto w-56 rounded-lg border border-rule bg-paper-2 px-3 py-1.5 text-sm text-ink outline-none placeholder:text-sepia focus:border-accent"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {current.unavailable && (
          <EmptyState
            title={`${TABS.find((t) => t.kind === active)!.label} are not available on this endpoint`}
            hint={`${activeMethod} is not advertised in /capabilities.`}
          />
        )}
        {!current.unavailable && current.isPending && <p className="p-6 text-sm text-sepia">loading…</p>}
        {current.isError && (
          <div className="p-6">
            <ErrorCard
              code={(current.errors[0]?.error as { code?: string })?.code}
              message={
                current.errors[0]?.error instanceof Error
                  ? current.errors[0].error.message
                  : 'failed to load'
              }
            />
          </div>
        )}
        {!current.unavailable && !current.isPending && !current.isError && rows.length === 0 && (
          <EmptyState
            title={needle ? 'No matches' : `No ${active === 'entity' ? 'entities' : `${active}s`} yet`}
            hint={
              needle
                ? 'Try a different filter.'
                : 'Approved artifacts appear here once proposals pass review.'
            }
          />
        )}
        <ul>
          {rows.map(({ project, artifact }) => (
            <li key={`${project.conn.endpoint} ${artifact.id}`}>
              <button
                onClick={() => openDrawer(active, artifact.id, project)}
                className="block w-full border-b border-rule/60 px-6 py-3.5 text-left transition hover:bg-paper-2"
              >
                <p className="truncate text-sm text-ink">{rowText(active, artifact)}</p>
                <p className="mt-0.5 flex items-center gap-2 truncate font-mono text-[11px] text-sepia">
                  {aggregated && (
                    <span className="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] text-accent-2">
                      {project.label}
                    </span>
                  )}
                  {artifact.id}
                </p>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <ArtifactDrawer target={drawer} project={drawerProject} onClose={closeDrawer} />
    </div>
  )
}
