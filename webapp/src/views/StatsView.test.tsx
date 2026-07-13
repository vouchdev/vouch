import { screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc } from '../lib/rpc'
import { renderWithProviders, seedConnection } from '../test/utils'
import { StatsView } from './StatsView'

const CAPS = { name: 'vouch', level: 3, methods: ['kb.status', 'kb.stats'], review_gated: true }

const STATUS = {
  kb_dir: '/tmp/demo/.vouch',
  claims: 12,
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

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.status') return STATUS
    if (method === 'kb.stats') return STATS
    throw new Error(`unexpected ${method}`)
  })
  seedConnection()
})

test('renders artifact count tiles from kb.status', async () => {
  renderWithProviders(<StatsView />)
  expect(await screen.findByText('12')).toBeInTheDocument() // claims
  // exact match: /claims/i would also hit "claims with a valid citation"
  expect(screen.getByText('claims')).toBeInTheDocument()
  expect(screen.getByText('pending')).toBeInTheDocument()
})

test('renders review metrics with approval rate as a percentage', async () => {
  renderWithProviders(<StatsView />)
  expect(await screen.findByText('80%')).toBeInTheDocument()
  expect(screen.getByText(/8 approved/i)).toBeInTheDocument()
  expect(screen.getByText(/2 rejected/i)).toBeInTheDocument()
})

test('renders citation coverage', async () => {
  renderWithProviders(<StatsView />)
  expect(await screen.findByText('92%')).toBeInTheDocument()
  expect(screen.getByText(/1 broken/i)).toBeInTheDocument()
})

test('renders the capabilities card', async () => {
  renderWithProviders(<StatsView />)
  expect(await screen.findByText(/level 3/i)).toBeInTheDocument()
  expect(screen.getByText(/review-gated/i)).toBeInTheDocument()
})
