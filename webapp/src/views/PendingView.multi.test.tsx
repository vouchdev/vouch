import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc } from '../lib/rpc'
import { renderWithProviders, seedProjects, TEST_ENDPOINT, TEST_ENDPOINT_B } from '../test/utils'
import { PendingView } from './PendingView'

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.list_pending', 'kb.approve', 'kb.reject'],
  review_gated: true,
}

const PROPOSAL_A = {
  id: 'prop-from-a',
  kind: 'claim',
  proposed_by: 'agent-a',
  session_id: null,
  payload: { text: 'claim living in project a' },
  status: 'pending',
  proposed_at: '2026-07-06T02:17:28+00:00',
}
const PROPOSAL_B = {
  id: 'prop-from-b',
  kind: 'claim',
  proposed_by: 'agent-b',
  session_id: null,
  payload: { text: 'claim living in project b' },
  status: 'pending',
  proposed_at: '2026-07-06T03:17:28+00:00',
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  vi.mocked(rpc).mockImplementation(async (conn, method) => {
    if (method === 'kb.list_pending') return conn.endpoint === TEST_ENDPOINT ? [PROPOSAL_A] : [PROPOSAL_B]
    if (method === 'kb.approve') return { kind: 'claim', id: 'approved-id' }
    return []
  })
  seedProjects([
    { endpoint: TEST_ENDPOINT, label: 'proj-a' },
    { endpoint: TEST_ENDPOINT_B, label: 'proj-b' },
  ])
})

test('aggregates the queues of every project and badges rows with the owner', async () => {
  renderWithProviders(<PendingView />)
  expect(await screen.findByText('claim living in project a')).toBeInTheDocument()
  expect(await screen.findByText('claim living in project b')).toBeInTheDocument()
  expect(screen.getAllByText('proj-a').length).toBeGreaterThan(0)
  expect(screen.getAllByText('proj-b').length).toBeGreaterThan(0)
})

test('approve routes to the project the proposal belongs to', async () => {
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByText('claim living in project b'))
  await userEvent.click(await screen.findByRole('button', { name: /approve/i }))

  await waitFor(() => {
    const call = vi
      .mocked(rpc)
      .mock.calls.find(([, method]) => method === 'kb.approve')
    expect(call).toBeDefined()
    expect(call![0].endpoint).toBe(TEST_ENDPOINT_B)
    expect(call![2]).toEqual({ proposal_id: 'prop-from-b' })
  })
  // The other project's queue was never touched by a decision.
  const approveCalls = vi.mocked(rpc).mock.calls.filter(([, m]) => m === 'kb.approve')
  expect(approveCalls).toHaveLength(1)
})
