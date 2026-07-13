import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { EmptyState } from '../components/EmptyState'
import { ErrorCard } from '../components/ErrorCard'
import { useErrorToast } from '../components/Toast'
import { useConnection } from '../connection/ConnectionContext'
import type { ProjectState } from '../connection/ConnectionContext'
import { rpc } from '../lib/rpc'
import type { KbActivity, KbStats, KbStatus } from '../lib/types'

type DayMetric = 'total' | 'proposals' | 'decisions'

const METRICS: { key: DayMetric; label: string; noun: string }[] = [
  { key: 'total', label: 'All events', noun: 'event' },
  { key: 'proposals', label: 'Proposals', noun: 'proposal' },
  { key: 'decisions', label: 'Decisions', noun: 'decision' },
]

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

// Steps of the ember ramp defined in styles.css — theme-aware via CSS vars.
const HEAT = [0, 1, 2, 3, 4].map((i) => `var(--heat-${i})`)

/** Quartile bucket into the 5-step ramp; 0 is reserved for "no events". */
function heatLevel(count: number, max: number): number {
  if (count <= 0 || max <= 0) return 0
  const t = max / 4
  if (count > 3 * t) return 4
  if (count > 2 * t) return 3
  if (count > t) return 2
  return 1
}

/** Local-calendar key matching the server's by_day bucketing. */
function dateKey(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

function daysAgo(base: Date, n: number): Date {
  const d = new Date(base)
  d.setDate(d.getDate() - n)
  return d
}

function localToday(): Date {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  return d
}

/** Monday-first weekday index, matching by_hour rows. */
function weekdayRow(d: Date): number {
  return (d.getDay() + 6) % 7
}

type Tip = { x: number; y: number; text: string } | null

function ChartTip({ tip }: { tip: Tip }) {
  if (!tip) return null
  return (
    <div
      className="pointer-events-none fixed z-50 rounded-lg border border-rule bg-paper-3 px-2 py-1 text-xs text-ink shadow-lg"
      style={{ left: tip.x + 12, top: tip.y + 14 }}
    >
      {tip.text}
    </div>
  )
}

function Tile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-xl border border-rule bg-paper-2 px-4 py-3">
      <p className="text-2xl font-semibold tabular-nums text-ink">{value}</p>
      <p className="mt-0.5 text-xs uppercase tracking-wide text-sepia">{label}</p>
    </div>
  )
}

function Card({
  title,
  right,
  children,
}: {
  title: string
  right?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="rounded-xl border border-rule bg-paper-2 p-5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-sepia">{title}</h3>
        {right}
      </div>
      {children}
    </section>
  )
}

const CELL = 11
const STEP = 13
const CAL_WEEKS = 53
const CAL_GUTTER_X = 30
const CAL_GUTTER_Y = 16

/** GitHub-style year calendar over by_day, colored with the ember ramp. */
function CalendarHeatmap({ byDay, metric }: { byDay: KbActivity['by_day']; metric: DayMetric }) {
  const [tip, setTip] = useState<Tip>(null)
  const today = localToday()
  const start = daysAgo(today, (CAL_WEEKS - 1) * 7 + weekdayRow(today))

  const cells: { w: number; r: number; date: Date; count: number }[] = []
  let max = 0
  for (let w = 0; w < CAL_WEEKS; w++) {
    for (let r = 0; r < 7; r++) {
      const date = new Date(start)
      date.setDate(start.getDate() + w * 7 + r)
      if (date.getTime() > today.getTime()) break
      const count = byDay[dateKey(date)]?.[metric] ?? 0
      if (count > max) max = count
      cells.push({ w, r, date, count })
    }
  }

  const boundaries: { w: number; label: string }[] = []
  let prevMonth = -1
  for (let w = 0; w < CAL_WEEKS; w++) {
    const monday = new Date(start)
    monday.setDate(start.getDate() + w * 7)
    if (monday.getTime() > today.getTime()) break
    if (monday.getMonth() !== prevMonth) {
      prevMonth = monday.getMonth()
      boundaries.push({ w, label: monday.toLocaleDateString(undefined, { month: 'short' }) })
    }
  }
  // The grid usually starts mid-month; labeling that sliver would put the
  // wrong month over the first real columns, so drop it (GitHub does too).
  if (boundaries.length > 1 && boundaries[1].w - boundaries[0].w < 3) boundaries.shift()
  const monthLabels = boundaries.map((b) => ({ x: CAL_GUTTER_X + b.w * STEP, label: b.label }))

  const active = cells.filter((c) => c.count > 0).length
  const shown = cells.reduce((sum, c) => sum + c.count, 0)
  const noun = METRICS.find((m) => m.key === metric)?.noun ?? 'event'
  const busiest = cells.reduce((top, c) => (c.count > top.count ? c : top), cells[0])
  const summary =
    `Activity calendar, last 12 months: ${shown} ${noun}${shown === 1 ? '' : 's'} ` +
    `across ${active} active day${active === 1 ? '' : 's'}` +
    (busiest && busiest.count > 0
      ? `, busiest ${busiest.date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} with ${busiest.count}`
      : '')
  const width = CAL_GUTTER_X + CAL_WEEKS * STEP
  const height = CAL_GUTTER_Y + 7 * STEP

  return (
    <div className="overflow-x-auto">
      <svg role="img" aria-label={summary} width={width} height={height} className="block">
        {monthLabels.map((m) => (
          <text key={m.x} x={m.x} y={10} className="fill-sepia" fontSize={9}>
            {m.label}
          </text>
        ))}
        {[0, 2, 4].map((r) => (
          <text
            key={r}
            x={0}
            y={CAL_GUTTER_Y + r * STEP + CELL - 2}
            className="fill-sepia"
            fontSize={9}
          >
            {WEEKDAYS[r]}
          </text>
        ))}
        {cells.map(({ w, r, date, count }) => (
          <rect
            key={`${w}-${r}`}
            x={CAL_GUTTER_X + w * STEP}
            y={CAL_GUTTER_Y + r * STEP}
            width={CELL}
            height={CELL}
            rx={2}
            fill={HEAT[heatLevel(count, max)]}
            onMouseMove={(e) =>
              setTip({
                x: e.clientX,
                y: e.clientY,
                text: `${count} ${noun}${count === 1 ? '' : 's'} — ${date.toLocaleDateString(undefined, {
                  weekday: 'short',
                  month: 'short',
                  day: 'numeric',
                })}`,
              })
            }
            onMouseLeave={() => setTip(null)}
          />
        ))}
      </svg>
      <ChartTip tip={tip} />
    </div>
  )
}

function RampLegend() {
  return (
    <div className="flex items-center gap-1 text-[10px] text-sepia">
      <span>less</span>
      {HEAT.map((c) => (
        <span key={c} className="inline-block h-2.5 w-2.5 rounded-[2px]" style={{ background: c }} />
      ))}
      <span>more</span>
    </div>
  )
}

const HOUR_CELL = 12
const HOUR_STEP = 14
const HOUR_GUTTER_X = 30
const HOUR_GUTTER_Y = 14

/** Hour-of-week matrix (Mon-first rows, 24 hour columns). */
function HourHeatmap({ byHour }: { byHour: number[][] }) {
  const [tip, setTip] = useState<Tip>(null)
  const max = Math.max(0, ...byHour.flat())
  let peak = ''
  if (max > 0) {
    const r = byHour.findIndex((row) => row.includes(max))
    peak = `; busiest ${WEEKDAYS[r]} ${String(byHour[r].indexOf(max)).padStart(2, '0')}:00 with ${max}`
  }
  const width = HOUR_GUTTER_X + 24 * HOUR_STEP
  const height = HOUR_GUTTER_Y + 7 * HOUR_STEP

  return (
    <div className="overflow-x-auto">
      <svg
        role="img"
        aria-label={`Events by hour of week${peak}`}
        width={width}
        height={height}
        className="block"
      >
        {[0, 3, 6, 9, 12, 15, 18, 21].map((h) => (
          <text key={h} x={HOUR_GUTTER_X + h * HOUR_STEP} y={9} className="fill-sepia" fontSize={9}>
            {h}
          </text>
        ))}
        {WEEKDAYS.map((d, r) => (
          <text
            key={d}
            x={0}
            y={HOUR_GUTTER_Y + r * HOUR_STEP + HOUR_CELL - 2}
            className="fill-sepia"
            fontSize={9}
          >
            {d}
          </text>
        ))}
        {byHour.slice(0, 7).map((row, r) =>
          row.slice(0, 24).map((count, h) => (
            <rect
              key={`${r}-${h}`}
              x={HOUR_GUTTER_X + h * HOUR_STEP}
              y={HOUR_GUTTER_Y + r * HOUR_STEP}
              width={HOUR_CELL}
              height={HOUR_CELL}
              rx={2}
              fill={HEAT[heatLevel(count, max)]}
              onMouseMove={(e) =>
                setTip({
                  x: e.clientX,
                  y: e.clientY,
                  text: `${count} event${count === 1 ? '' : 's'} — ${WEEKDAYS[r]} ${String(h).padStart(2, '0')}:00`,
                })
              }
              onMouseLeave={() => setTip(null)}
            />
          )),
        )}
      </svg>
      <ChartTip tip={tip} />
    </div>
  )
}

/** Last 30 days as thin baseline-anchored bars. */
function DailyBars({ byDay }: { byDay: KbActivity['by_day'] }) {
  const [tip, setTip] = useState<Tip>(null)
  const today = localToday()
  const days = Array.from({ length: 30 }, (_, i) => {
    const date = daysAgo(today, 29 - i)
    return { date, count: byDay[dateKey(date)]?.total ?? 0 }
  })
  const max = Math.max(0, ...days.map((d) => d.count))
  const total = days.reduce((sum, d) => sum + d.count, 0)

  return (
    <div>
      <div
        className="flex h-28 items-end gap-[2px]"
        role="img"
        aria-label={`Events per day, last 30 days: ${total} total`}
      >
        {days.map(({ date, count }) => (
          <div
            key={date.getTime()}
            className="min-w-[3px] flex-1 rounded-t-[3px]"
            style={{
              height: max > 0 && count > 0 ? `${Math.max(4, (count / max) * 100)}%` : '2px',
              background: count > 0 ? 'var(--accent)' : 'var(--paper-3)',
            }}
            onMouseMove={(e) =>
              setTip({
                x: e.clientX,
                y: e.clientY,
                text: `${count} event${count === 1 ? '' : 's'} — ${date.toLocaleDateString(undefined, {
                  month: 'short',
                  day: 'numeric',
                })}`,
              })
            }
            onMouseLeave={() => setTip(null)}
          />
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-sepia">
        <span>{daysAgo(today, 29).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</span>
        <span>today</span>
      </div>
      <ChartTip tip={tip} />
    </div>
  )
}

/** Ranked label + magnitude bar + count rows (top actors, event mix). */
function BarList({ items, empty }: { items: [string, number][]; empty: string }) {
  if (items.length === 0) return <p className="text-xs text-sepia">{empty}</p>
  const max = Math.max(...items.map(([, n]) => n))
  return (
    <ul className="space-y-2">
      {items.map(([name, count]) => (
        <li key={name} className="flex items-center gap-3">
          <span className="w-44 truncate font-mono text-xs text-ink-2" title={name}>
            {name}
          </span>
          <span className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-paper-3">
            <span
              className="block h-full rounded-full"
              style={{ width: `${(count / max) * 100}%`, background: 'var(--heat-3)' }}
            />
          </span>
          <span className="w-12 shrink-0 text-right text-xs tabular-nums text-ink">{count}</span>
        </li>
      ))}
    </ul>
  )
}

function pct(rate: number | null): string {
  return rate === null ? '—' : `${Math.round(rate * 100)}%`
}

/** One project's dashboard — the aggregated page stacks one per project. */
function ProjectDashboard({ project, titled }: { project: ProjectState; titled: boolean }) {
  const { conn, caps, label } = project
  const endpoint = conn.endpoint
  const [metric, setMetric] = useState<DayMetric>('total')
  // null = capabilities still loading; don't call or complain until known.
  const supportsActivity = caps === null ? null : caps.methods.includes('kb.activity')

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
  const activity = useQuery({
    queryKey: ['activity', endpoint],
    queryFn: () =>
      rpc<KbActivity>(conn, 'kb.activity', {
        // 53 weeks so the oldest drawn calendar column is always inside the window.
        days: 371,
        tz_offset_minutes: -new Date().getTimezoneOffset(),
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
      }),
    // kb.activity scans the whole audit log server-side — poll gently.
    refetchInterval: 120_000,
    enabled: supportsActivity === true,
  })
  useErrorToast(status.isError, status.error)
  useErrorToast(stats.isError, stats.error)
  useErrorToast(activity.isError, activity.error)

  if (status.isError)
    return (
      <ErrorCard
        code={(status.error as { code?: string }).code}
        message={status.error instanceof Error ? status.error.message : 'failed to load status'}
      />
    )

  const s = status.data
  const t = stats.data
  const a = activity.data

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

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Tile label="events · 12 mo" value={a ? a.total_events : '—'} />
        <Tile label="active days" value={a ? a.active_days : '—'} />
        {s && <Tile label="claims" value={s.claims} />}
        {s && <Tile label="pages" value={s.pages} />}
        {s && <Tile label="pending" value={s.pending_proposals} />}
        {t?.review && <Tile label="approval · 30d" value={pct(t.review.approval_rate ?? null)} />}
      </div>

      {supportsActivity === false && (
        <Card title="Activity">
          <EmptyState
            title="This endpoint doesn't advertise kb.activity"
            hint="Upgrade vouch on this project to see activity analytics."
          />
        </Card>
      )}

      {activity.isError && (
        <ErrorCard
          code={(activity.error as { code?: string }).code}
          message={activity.error instanceof Error ? activity.error.message : 'failed to load activity'}
        />
      )}

      {a && a.total_events === 0 && (
        <Card title="Activity — last 12 months">
          <EmptyState
            title="No audit activity yet"
            hint="Events appear here as agents propose knowledge and reviews are decided."
          />
        </Card>
      )}

      {a && a.total_events > 0 && (
        <>
          <Card
            title="Activity — last 12 months"
            right={
              <div className="flex items-center gap-3">
                <div className="flex rounded-lg border border-rule p-0.5">
                  {METRICS.map((m) => (
                    <button
                      key={m.key}
                      aria-pressed={metric === m.key}
                      onClick={() => setMetric(m.key)}
                      className={`rounded-md px-2 py-1 text-[11px] transition ${
                        metric === m.key ? 'bg-paper-3 text-ink' : 'text-sepia hover:text-ink'
                      }`}
                    >
                      {m.label}
                    </button>
                  ))}
                </div>
                <RampLegend />
              </div>
            }
          >
            <CalendarHeatmap byDay={a.by_day} metric={metric} />
          </Card>

          <div className="grid gap-6 lg:grid-cols-2">
            <div className="space-y-6">
              <Card title="Last 30 days">
                <DailyBars byDay={a.by_day} />
              </Card>
              <Card title="By hour of week">
                <HourHeatmap byHour={a.by_hour} />
              </Card>
            </div>
            <div className="space-y-6">
              <Card title="Top actors">
                <BarList items={Object.entries(a.by_actor).slice(0, 8)} empty="no actors yet" />
              </Card>
              <Card title="Event mix">
                <BarList items={Object.entries(a.by_event).slice(0, 8)} empty="no events yet" />
              </Card>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

export function DashboardView() {
  const { scoped, aggregated } = useConnection()
  return (
    <div className="mx-auto max-w-5xl space-y-10 p-6">
      {scoped.map((p) => (
        <ProjectDashboard key={p.conn.endpoint} project={p} titled={aggregated} />
      ))}
    </div>
  )
}
