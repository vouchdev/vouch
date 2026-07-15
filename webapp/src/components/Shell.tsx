import { Activity, BadgeCheck, FileClock, Inbox, LayoutDashboard, Library, MessageSquare, Plug, SunMoon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { ConnectDialog } from '../connection/ConnectDialog'
import { ALL_SCOPE, useConnection } from '../connection/ConnectionContext'
import { useFanout } from '../lib/fanout'
import type { Proposal, SessionEntry } from '../lib/types'

const THEME_KEY = 'vouch-ui.theme'

const NAV = [
  { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/chat', label: 'Chat', icon: MessageSquare },
  { to: '/review', label: 'Review', icon: FileClock },
  { to: '/pending', label: 'Pending', icon: Inbox },
  { to: '/claims', label: 'Claims', icon: BadgeCheck },
  { to: '/browse', label: 'Browse', icon: Library },
  { to: '/stats', label: 'Stats', icon: Activity },
]

const TITLES: Record<string, string> = {
  '/chat': 'Chat',
  '/review': 'Review — session transcripts & summaries',
  '/pending': 'Pending review',
  '/claims': 'Approved claims',
  '/browse': 'Knowledge',
  '/dashboard': 'Dashboard — KB activity',
  '/stats': 'Stats & health',
}

export function Shell() {
  const { projects, scoped, scope, setScope, health, needsAuth } = useConnection()
  const location = useLocation()
  const [theme, setTheme] = useState(() => localStorage.getItem(THEME_KEY) ?? 'dark')
  const [manageOpen, setManageOpen] = useState(false)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem(THEME_KEY, theme)
  }, [theme])

  const pending = useFanout<Proposal[]>(['pending'], 'kb.list_pending', {}, { refetchInterval: 10_000 })
  const sessions = useFanout<{ sessions: SessionEntry[] }>(['sessions'], 'kb.list_sessions', {}, {
    refetchInterval: 10_000,
  })
  const sessionRows = sessions.rows.flatMap((r) =>
    (r.data?.sessions ?? []).map((s) => ({ endpoint: r.project.conn.endpoint, s })),
  )
  const reviewCount = sessionRows.filter(({ s }) => !s.summarized).length
  // Keep the badge in step with PendingView: proposals whose session is still
  // awaiting a summary are counted under Review, not Pending. Proposal ids are
  // only unique per project, so key the set by endpoint too.
  const awaitingSummary = new Set(
    sessionRows
      .filter(({ s }) => s.stage === 'pending' && !s.summarized && s.proposal_id)
      .map(({ endpoint, s }) => `${endpoint} ${s.proposal_id}`),
  )
  const pendingCount = pending.rows
    .flatMap((r) => r.data.map((p) => `${r.project.conn.endpoint} ${p.id}`))
    .filter((k) => !awaitingSummary.has(k)).length

  const down = projects.filter((p) => p.health === 'down')
  const dot = health === 'ok' ? 'bg-ok' : health === 'down' ? 'bg-accent' : 'bg-sepia animate-pulse'
  const pillText =
    scope === ALL_SCOPE
      ? projects.length === 1
        ? projects[0].label
        : `${projects.length} projects`
      : (scoped[0]?.label ?? scope)

  return (
    <div className="flex h-screen bg-paper text-ink">
      <aside className="flex w-52 shrink-0 flex-col border-r border-rule bg-paper-2">
        <div className="flex items-center gap-2 px-5 py-5">
          <span className="font-mono text-sm font-bold tracking-widest text-accent">VOUCH</span>
          <span className="text-xs text-sepia">console</span>
        </div>
        <nav className="flex flex-col gap-1 px-3">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
                  isActive ? 'bg-paper-3 text-ink' : 'text-ink-2 hover:bg-paper-3 hover:text-ink'
                }`
              }
            >
              <Icon size={16} strokeWidth={1.75} />
              <span>{label}</span>
              {to === '/review' && reviewCount > 0 && (
                <span className="ml-auto rounded-full bg-accent px-2 py-0.5 text-[10px] font-bold text-paper">
                  {reviewCount}
                </span>
              )}
              {to === '/pending' && pendingCount > 0 && (
                <span className="ml-auto rounded-full bg-accent px-2 py-0.5 text-[10px] font-bold text-paper">
                  {pendingCount}
                </span>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto px-5 py-4 text-[10px] text-sepia">
          review-gated knowledge, made visible
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-rule px-6">
          <h1 className="text-sm font-semibold text-ink">
            {Object.entries(TITLES).find(([p]) => location.pathname.startsWith(p))?.[1] ?? 'vouch console'}
          </h1>
          <div className="flex items-center gap-3">
            {projects.length > 1 && (
              <select
                aria-label="project scope"
                value={scope}
                onChange={(e) => setScope(e.target.value)}
                className="rounded-lg border border-rule bg-paper-2 px-2 py-1.5 text-xs text-ink-2 outline-none focus:border-accent"
              >
                <option value={ALL_SCOPE}>All projects</option>
                {projects.map((p) => (
                  <option key={p.conn.endpoint} value={p.conn.endpoint}>
                    {p.label}
                  </option>
                ))}
              </select>
            )}
            <button
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
              title="Toggle theme"
              className="rounded-lg p-2 text-sepia transition hover:bg-paper-3 hover:text-ink"
            >
              <SunMoon size={16} />
            </button>
            {projects.length > 0 && (
              <button
                onClick={() => setManageOpen(true)}
                title="Manage projects"
                className="flex items-center gap-2 rounded-full border border-rule bg-paper-2 px-3 py-1.5 font-mono text-xs text-ink-2 transition hover:border-accent/50"
              >
                <span className={`h-2 w-2 rounded-full ${dot}`} />
                {pillText}
                <Plug size={12} className="text-sepia" />
              </button>
            )}
          </div>
        </header>
        {down.length > 0 && (
          <div role="alert" className="flex items-center justify-between border-b border-accent/40 bg-accent/10 px-6 py-2 text-sm text-accent-2">
            <span>
              {down.map((p) => p.label).join(', ')} unreachable — is `vouch serve --transport http`
              still running?
            </span>
            <button
              onClick={() => setManageOpen(true)}
              className="rounded-lg border border-accent/50 px-3 py-1 text-xs font-semibold hover:bg-accent/20"
            >
              Manage projects
            </button>
          </div>
        )}
        <main className="min-h-0 flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>

      {(projects.length === 0 || needsAuth || manageOpen) && (
        <ConnectDialog
          onClose={projects.length > 0 && !needsAuth ? () => setManageOpen(false) : undefined}
        />
      )}
    </div>
  )
}
