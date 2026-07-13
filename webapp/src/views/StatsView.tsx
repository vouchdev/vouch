import { useQuery } from '@tanstack/react-query'
import { ErrorCard } from '../components/ErrorCard'
import { useErrorToast } from '../components/Toast'
import { useConnection } from '../connection/ConnectionContext'
import type { ProjectState } from '../connection/ConnectionContext'
import { rpc } from '../lib/rpc'
import type { KbStats, KbStatus } from '../lib/types'

function pct(rate: number | null): string {
  return rate === null ? '—' : `${Math.round(rate * 100)}%`
}

function Tile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-xl border border-rule bg-paper-2 px-4 py-3">
      <p className="text-2xl font-semibold tabular-nums text-ink">{value}</p>
      <p className="mt-0.5 text-xs uppercase tracking-wide text-sepia">{label}</p>
    </div>
  )
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-rule bg-paper-2 p-5">
      <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-sepia">{title}</h3>
      {children}
    </section>
  )
}

/** One project's stats block — the aggregated page stacks one per project. */
function ProjectStats({ project, titled }: { project: ProjectState; titled: boolean }) {
  const { conn, caps, health, label } = project
  const endpoint = conn.endpoint

  const status = useQuery({
    queryKey: ['status', endpoint],
    queryFn: () => rpc<KbStatus>(conn, 'kb.status'),
    refetchInterval: 15_000,
  })
  const stats = useQuery({
    queryKey: ['stats', endpoint],
    queryFn: () => rpc<KbStats>(conn, 'kb.stats', { days: 30 }),
    refetchInterval: 30_000,
  })
  useErrorToast(status.isError, status.error)
  useErrorToast(stats.isError, stats.error)

  if (status.isError)
    return (
      <ErrorCard
        code={(status.error as { code?: string }).code}
        message={status.error instanceof Error ? status.error.message : 'failed to load status'}
      />
    )

  const s = status.data
  const t = stats.data

  return (
    <div className="space-y-6">
      {titled && (
        <h2 className="flex items-center gap-2 text-sm font-semibold text-ink">
          <span className="rounded bg-accent/15 px-2 py-0.5 font-mono text-[11px] text-accent-2">
            {label}
          </span>
          <span className="break-all font-mono text-[11px] font-normal text-sepia">{endpoint}</span>
        </h2>
      )}
      {s && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <Tile label="claims" value={s.claims} />
          <Tile label="pages" value={s.pages} />
          <Tile label="entities" value={s.entities} />
          <Tile label="relations" value={s.relations} />
          <Tile label="sources" value={s.sources} />
          <Tile label="evidence" value={s.evidence} />
          <Tile label="sessions" value={s.sessions} />
          <Tile label="pending" value={s.pending_proposals} />
          <Tile label="audit events" value={s.audit_events} />
          <Tile label="index" value={s.index_present ? 'present' : 'missing'} />
        </div>
      )}

      {stats.isError && (
        <ErrorCard
          code={(stats.error as { code?: string }).code}
          message={stats.error instanceof Error ? stats.error.message : 'failed to load stats'}
        />
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        {t && (
          <Card title={`Review — last ${t.review.window_days} days`}>
            <p className="text-3xl font-semibold text-ink">{pct(t.review.approval_rate)}</p>
            <p className="mb-3 text-xs text-sepia">approval rate</p>
            <p className="text-sm text-ink-2">
              {t.review.approved} approved · {t.review.rejected} rejected · {t.review.expired} expired
            </p>
            {Object.keys(t.review.by_agent).length > 0 && (
              <ul className="mt-3 space-y-1">
                {Object.entries(t.review.by_agent).map(([agent, r]) => (
                  <li key={agent} className="flex justify-between text-xs text-sepia">
                    <span className="font-mono">{agent}</span>
                    <span>
                      ✓{r.approved} ✗{r.rejected} ⏳{r.pending}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        )}

        {t && (
          <Card title="Citations">
            <p className="text-3xl font-semibold text-ink">{pct(t.citations.coverage_rate)}</p>
            <p className="mb-3 text-xs text-sepia">claims with a valid citation</p>
            <p className="text-sm text-ink-2">
              {t.citations.claims_with_valid_citation} of {t.citations.claims_total} cited · {t.citations.broken_citation} broken
            </p>
          </Card>
        )}

        <Card title="Endpoint">
          <p className="text-sm text-ink-2">
            <span className={`mr-2 inline-block h-2 w-2 rounded-full ${health === 'ok' ? 'bg-ok' : 'bg-accent'}`} />
            {health === 'ok' ? 'healthy' : health}
          </p>
          {caps && (
            <div className="mt-3 space-y-1 text-sm text-ink-2">
              <p>
                <span className="font-mono text-xs text-sepia">{caps.name}</span> · level {caps.level}
              </p>
              <p>{caps.methods.length} methods advertised</p>
              <p>{caps.review_gated ? 'review-gated' : 'not review-gated'}</p>
            </div>
          )}
          {s && <p className="mt-3 break-all font-mono text-[11px] text-sepia">{s.kb_dir}</p>}
        </Card>
      </div>
    </div>
  )
}

export function StatsView() {
  const { scoped, aggregated } = useConnection()
  return (
    <div className="mx-auto max-w-5xl space-y-10 p-6">
      {scoped.map((p) => (
        <ProjectStats key={p.conn.endpoint} project={p} titled={aggregated} />
      ))}
    </div>
  )
}
