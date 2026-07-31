import { useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Minus, Plus, RotateCcw } from 'lucide-react'
import { ArtifactDrawer } from '../components/ArtifactDrawer'
import type { DrawerTarget } from '../components/ArtifactDrawer'
import { EmptyState } from '../components/EmptyState'
import { ErrorCard } from '../components/ErrorCard'
import { useErrorToast, useToast } from '../components/Toast'
import { useConnection } from '../connection/ConnectionContext'
import type { ProjectState } from '../connection/ConnectionContext'
import { canRead } from '../lib/resolveArtifact'
import { rpc } from '../lib/rpc'
import type { GraphEdge, GraphExport, GraphNode } from '../lib/types'

const METHOD = 'kb.graph_export'

/**
 * Left-to-right reading order, the same flow `vouch graph` renders with
 * rankdir=LR: what is proposed or written, then the claims it rests on, then
 * the evidence and sources under those, with sessions and audit events last.
 */
const COLUMNS: GraphNode['kind'][] = [
  'proposal',
  'page',
  'claim',
  'evidence',
  'source',
  'session',
  'event',
  'unknown',
]

const COL_W = 210
const ROW_H = 30
const PAD = 26
const NODE_R = 6
const MIN_ZOOM = 0.25
const MAX_ZOOM = 3

/** Statuses that mean "no longer the live answer" — drawn muted. */
const RETIRED = new Set(['superseded', 'archived', 'redacted'])

function fillFor(status: string): string {
  if (status === 'pending') return 'var(--accent)'
  if (RETIRED.has(status)) return 'var(--sepia)'
  if (status) return 'var(--ok)'
  return 'var(--ink-2)'
}

const LEGEND: { label: string; fill: string }[] = [
  { label: 'pending review', fill: 'var(--accent)' },
  { label: 'approved', fill: 'var(--ok)' },
  { label: 'superseded / archived', fill: 'var(--sepia)' },
  { label: 'source, session, audit event', fill: 'var(--ink-2)' },
]

interface Placed extends GraphNode {
  x: number
  y: number
}

interface Layout {
  nodes: Placed[]
  edges: { edge: GraphEdge; x1: number; y1: number; x2: number; y2: number }[]
  width: number
  height: number
}

/**
 * A deterministic layered layout: one column per node kind, nodes stacked in
 * the order the server returned them (which is sorted by id). No simulation —
 * the same KB draws the same picture every time, which is what makes a graph
 * screenshot worth comparing against the last one.
 */
function layout(graph: GraphExport): Layout {
  const columns = COLUMNS.filter((kind) => graph.nodes.some((n) => n.kind === kind))
  const rows = new Map<string, number>()
  const nodes: Placed[] = []
  for (const node of graph.nodes) {
    const col = Math.max(columns.indexOf(node.kind), 0)
    const row = rows.get(node.kind) ?? 0
    rows.set(node.kind, row + 1)
    nodes.push({ ...node, x: PAD + col * COL_W, y: PAD + row * ROW_H })
  }
  const at = new Map(nodes.map((n) => [n.id, n]))
  const edges = graph.edges.flatMap((edge) => {
    const from = at.get(edge.src)
    const to = at.get(edge.dst)
    return from && to ? [{ edge, x1: from.x, y1: from.y, x2: to.x, y2: to.y }] : []
  })
  const tallest = Math.max(1, ...rows.values())
  return {
    nodes,
    edges,
    width: PAD * 2 + Math.max(1, columns.length) * COL_W,
    height: PAD * 2 + tallest * ROW_H,
  }
}

function truncate(text: string, max = 38): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function ZoomButton({
  label,
  onClick,
  children,
}: {
  label: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      aria-label={label}
      onClick={onClick}
      className="rounded-lg border border-rule bg-paper-2 p-1.5 text-ink-2 transition hover:bg-paper-3 hover:text-ink"
    >
      {children}
    </button>
  )
}

function Network({ graph, project }: { graph: GraphExport; project: ProjectState }) {
  const { toast } = useToast()
  const [drawer, setDrawer] = useState<DrawerTarget>(null)
  const [pendingOnly, setPendingOnly] = useState(false)
  const [view, setView] = useState({ x: 0, y: 0, k: 1 })
  const grab = useRef<{ x: number; y: number } | null>(null)

  const shown = useMemo<GraphExport>(() => {
    if (!pendingOnly) return graph
    // The frontier plus what it touches — a pending claim is only legible
    // next to the source it cites and the claim it would replace.
    const seeds = new Set(graph.nodes.filter((n) => n.kind === 'proposal').map((n) => n.id))
    const keep = new Set(seeds)
    for (const e of graph.edges) {
      if (seeds.has(e.src)) keep.add(e.dst)
      if (seeds.has(e.dst)) keep.add(e.src)
    }
    return {
      nodes: graph.nodes.filter((n) => keep.has(n.id)),
      edges: graph.edges.filter((e) => keep.has(e.src) && keep.has(e.dst)),
    }
  }, [graph, pendingOnly])

  const placed = useMemo(() => layout(shown), [shown])
  const pending = graph.nodes.filter((n) => n.status === 'pending').length
  const summary =
    `Memory network: ${shown.nodes.length} node${shown.nodes.length === 1 ? '' : 's'} ` +
    `and ${shown.edges.length} link${shown.edges.length === 1 ? '' : 's'}, ` +
    `${pending} pending review.`

  const open = (node: GraphNode) => {
    if (node.kind === 'proposal') {
      toast('info', 'pending proposal — decide it under Review or Pending')
      return
    }
    if (!canRead(project, node.kind)) {
      toast('error', `${node.kind} ${truncate(node.id, 24)} is not readable here`)
      return
    }
    setDrawer({ kind: node.kind, id: node.id })
  }

  const zoom = (factor: number) =>
    setView((v) => ({ ...v, k: Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.k * factor)) }))

  if (graph.nodes.length === 0) {
    return (
      <EmptyState
        title="Nothing to draw yet"
        hint="Claims, pages and pending proposals appear here as soon as an agent writes one."
      />
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-4 border-b border-rule px-6 py-3">
        {LEGEND.map(({ label, fill }) => (
          <span key={label} className="flex items-center gap-1.5 text-xs text-sepia">
            <span className="h-2.5 w-2.5 rounded-full" style={{ background: fill }} />
            {label}
          </span>
        ))}
        <label className="ml-auto flex items-center gap-2 text-xs text-ink-2">
          <input
            type="checkbox"
            checked={pendingOnly}
            onChange={(e) => setPendingOnly(e.target.checked)}
          />
          pending frontier only ({pending})
        </label>
        <div className="flex items-center gap-1">
          <ZoomButton label="zoom out" onClick={() => zoom(1 / 1.25)}>
            <Minus size={14} strokeWidth={1.75} />
          </ZoomButton>
          <ZoomButton label="zoom in" onClick={() => zoom(1.25)}>
            <Plus size={14} strokeWidth={1.75} />
          </ZoomButton>
          <ZoomButton label="reset view" onClick={() => setView({ x: 0, y: 0, k: 1 })}>
            <RotateCcw size={14} strokeWidth={1.75} />
          </ZoomButton>
        </div>
      </div>

      <p className="sr-only">{summary}</p>

      <div className="min-h-0 flex-1 overflow-hidden">
        <svg
          data-testid="memory-network"
          className="h-full w-full cursor-grab touch-none select-none"
          onPointerDown={(e) => {
            grab.current = { x: e.clientX - view.x, y: e.clientY - view.y }
          }}
          onPointerMove={(e) => {
            const from = grab.current
            if (from) setView((v) => ({ ...v, x: e.clientX - from.x, y: e.clientY - from.y }))
          }}
          onPointerUp={() => {
            grab.current = null
          }}
          onPointerLeave={() => {
            grab.current = null
          }}
          onWheel={(e) => zoom(e.deltaY < 0 ? 1.1 : 1 / 1.1)}
        >
          <defs>
            <marker
              id="memory-network-arrow"
              viewBox="0 0 8 8"
              refX={7}
              refY={4}
              markerWidth={5}
              markerHeight={5}
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 8 4 L 0 8 z" fill="var(--rule)" />
            </marker>
          </defs>
          <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
            {placed.edges.map(({ edge, x1, y1, x2, y2 }) => (
              <line
                key={`${edge.src} ${edge.kind} ${edge.dst}`}
                x1={x1}
                y1={y1}
                x2={x2}
                y2={y2}
                stroke="var(--rule)"
                strokeWidth={1}
                strokeDasharray={edge.kind === 'targets' ? '3 3' : undefined}
                markerEnd="url(#memory-network-arrow)"
              />
            ))}
            {placed.nodes.map((node) => (
              <g
                key={node.id}
                role="button"
                tabIndex={0}
                aria-label={`${node.kind} ${node.label}${node.status ? ` (${node.status})` : ''}`}
                className="cursor-pointer"
                onClick={() => open(node)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') open(node)
                }}
              >
                <circle cx={node.x} cy={node.y} r={NODE_R} fill={fillFor(node.status)} />
                <text
                  x={node.x + NODE_R + 5}
                  y={node.y + 3}
                  className="fill-ink-2"
                  fontSize={9}
                >
                  {truncate(node.label)}
                </text>
              </g>
            ))}
          </g>
        </svg>
      </div>

      <ArtifactDrawer
        target={drawer}
        project={project}
        onClose={() => setDrawer(null)}
        onOpen={(kind, id) => setDrawer({ kind, id })}
      />
    </div>
  )
}

function ProjectNetwork({ project, titled }: { project: ProjectState; titled: boolean }) {
  const supported = project.caps === null || project.caps.methods.includes(METHOD)
  const q = useQuery({
    queryKey: ['graph-export', project.conn.endpoint],
    queryFn: () =>
      rpc<{ format: string; graph: GraphExport }>(project.conn, METHOD, { format: 'json' }),
    enabled: supported,
  })
  useErrorToast(q.isError, q.error)

  return (
    <section className="flex h-full min-h-0 flex-col">
      {titled && (
        <h2 className="border-b border-rule px-6 py-2 text-xs font-semibold uppercase tracking-wide text-sepia">
          {project.label}
        </h2>
      )}
      {!supported && (
        <EmptyState
          title="The memory network is not available on this endpoint"
          hint={`${METHOD} is not advertised in /capabilities.`}
        />
      )}
      {supported && q.isPending && <p className="p-6 text-sm text-sepia">loading…</p>}
      {supported && q.isError && (
        <div className="p-6">
          <ErrorCard
            code={(q.error as { code?: string })?.code}
            message={q.error instanceof Error ? q.error.message : 'failed to load the graph'}
          />
        </div>
      )}
      {supported && q.data && <Network graph={q.data.graph} project={project} />}
    </section>
  )
}

export function MemoryNetworkView() {
  const { scoped, aggregated } = useConnection()
  return (
    <div className="flex h-full flex-col divide-y divide-rule">
      {scoped.map((p) => (
        <ProjectNetwork key={p.conn.endpoint} project={p} titled={aggregated} />
      ))}
    </div>
  )
}
