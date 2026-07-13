import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc } from '../lib/rpc'
import { renderWithProviders, seedConnection } from '../test/utils'
import { DashboardView } from './DashboardView'

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.status', 'kb.stats', 'kb.activity'],
  review_gated: true,
}

// Numbers chosen to be unique on the page — the hour axis renders
// 0,3,6,…,21 as text, so fixture counts must avoid those.
const STATUS = {
  kb_dir: '/tmp/demo/.vouch',
  claims: 13,
  pages: 3,
  sources: 5,
  entities: 4,
  relations: 2,
  evidence: 1,
  sessions: 6,
  pending_proposals: 2,
  audit_events: 40,
  index_present: true,
}

const STATS = {
  generated_at: '2026-07-04T02:18:21+00:00',
  counts: STATUS,
  pending: { total: 2, by_agent: { 'agent-a': 2 }, age_days: { median: 1, max: 3, oldest_id: 'x' } },
  review: {
    window_days: 30,
    decided_in_window: 10,
    approved: 8,
    rejected: 2,
    expired: 0,
    approval_rate: 0.8,
    by_agent: { 'agent-a': { approved: 8, rejected: 2, expired: 0, pending: 2 } },
  },
  citations: { claims_total: 12, claims_with_valid_citation: 11, broken_citation: 1, invalid_claim: 0, coverage_rate: 0.9167 },
}

/** Local "YYYY-MM-DD" for n days ago — must match the view's bucketing. */
function localKey(daysAgo: number): string {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  d.setDate(d.getDate() - daysAgo)
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

const BY_HOUR = Array.from({ length: 7 }, () => Array<number>(24).fill(0))
BY_HOUR[1][14] = 3

const ACTIVITY = {
  generated_at: '2026-07-10T08:00:00+00:00',
  window_days: 365,
  tz_offset_minutes: 0,
  total_events: 41,
  active_days: 2,
  first_event_day: localKey(7),
  last_event_day: localKey(0),
  by_day: {
    [localKey(7)]: { total: 15, proposals: 9, decisions: 2 },
    [localKey(0)]: { total: 26, proposals: 5, decisions: 4 },
  },
  by_hour: BY_HOUR,
  by_actor: { 'wiki-compiler': 23, a: 11, 'vouch-capture': 8 },
  by_event: { 'proposal.page.create': 14, 'proposal.page.approve': 5 },
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.status') return STATUS
    if (method === 'kb.stats') return STATS
    if (method === 'kb.activity') return ACTIVITY
    throw new Error(`unexpected ${method}`)
  })
  seedConnection()
})

test('renders tiles from kb.activity, kb.status and kb.stats', async () => {
  renderWithProviders(<DashboardView />)
  expect(await screen.findByText('41')).toBeInTheDocument() // events · 12 mo
  expect(screen.getByText('events · 12 mo')).toBeInTheDocument()
  expect(screen.getByText('active days')).toBeInTheDocument()
  expect(await screen.findByText('13')).toBeInTheDocument() // claims
  expect(await screen.findByText('80%')).toBeInTheDocument() // approval rate
})

test('renders the activity calendar with a metric toggle', async () => {
  renderWithProviders(<DashboardView />)
  expect(
    await screen.findByRole('img', {
      name: /activity calendar, last 12 months: 41 events across 2 active days/i,
    }),
  ).toBeInTheDocument()
  expect(vi.mocked(rpc)).toHaveBeenCalledWith(
    expect.anything(),
    'kb.activity',
    expect.objectContaining({ days: 371, tz: expect.any(String) }),
  )
  const proposals = screen.getByRole('button', { name: 'Proposals' })
  expect(proposals).toHaveAttribute('aria-pressed', 'false')
  await userEvent.click(proposals)
  expect(proposals).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('button', { name: 'All events' })).toHaveAttribute('aria-pressed', 'false')
})

test('renders hour-of-week heatmap and 30-day bars', async () => {
  renderWithProviders(<DashboardView />)
  expect(await screen.findByRole('img', { name: /events by hour of week/i })).toBeInTheDocument()
  expect(screen.getByRole('img', { name: /events per day, last 30 days/i })).toBeInTheDocument()
  expect(screen.getByText('today')).toBeInTheDocument()
})

test('renders top actors and event mix with counts', async () => {
  renderWithProviders(<DashboardView />)
  expect(await screen.findByText('wiki-compiler')).toBeInTheDocument()
  expect(screen.getByText('23')).toBeInTheDocument()
  expect(screen.getByText('proposal.page.create')).toBeInTheDocument()
  expect(screen.getByText('14')).toBeInTheDocument()
})

test('shows an upgrade hint when the endpoint lacks kb.activity', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: ['kb.status', 'kb.stats'] })
  renderWithProviders(<DashboardView />)
  expect(await screen.findByText(/doesn't advertise kb.activity/i)).toBeInTheDocument()
  expect(vi.mocked(rpc)).not.toHaveBeenCalledWith(expect.anything(), 'kb.activity', expect.anything())
})

test('shows an empty state when the audit log has no events', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.status') return STATUS
    if (method === 'kb.stats') return STATS
    if (method === 'kb.activity')
      return {
        ...ACTIVITY,
        total_events: 0,
        active_days: 0,
        first_event_day: null,
        last_event_day: null,
        by_day: {},
        by_hour: Array.from({ length: 7 }, () => Array<number>(24).fill(0)),
        by_actor: {},
        by_event: {},
      }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<DashboardView />)
  expect(await screen.findByText(/no audit activity yet/i)).toBeInTheDocument()
})
