import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc } from '../lib/rpc'
import { renderWithProviders, seedProjects, TEST_ENDPOINT, TEST_ENDPOINT_B } from '../test/utils'
import { Shell } from './Shell'

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.list_pending', 'kb.list_sessions'],
  review_gated: true,
}

function proposal(id: string) {
  return {
    id,
    kind: 'claim',
    proposed_by: 'agent',
    session_id: null,
    payload: { text: id },
    status: 'pending',
    proposed_at: '2026-07-06T02:17:28+00:00',
  }
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  vi.mocked(rpc).mockImplementation(async (conn, method) => {
    if (method === 'kb.list_pending')
      return conn.endpoint === TEST_ENDPOINT ? [proposal('a-1'), proposal('a-2')] : [proposal('b-1')]
    if (method === 'kb.list_sessions') return { sessions: [] }
    return []
  })
  seedProjects([
    { endpoint: TEST_ENDPOINT, label: 'proj-a' },
    { endpoint: TEST_ENDPOINT_B, label: 'proj-b' },
  ])
})

test('pending badge sums the queues of every project in scope', async () => {
  renderWithProviders(<Shell />)
  await waitFor(() => expect(screen.getByText('3')).toBeInTheDocument())
})

test('the scope switcher narrows every fan-out to one project', async () => {
  renderWithProviders(<Shell />)
  const picker = await screen.findByRole('combobox', { name: /project scope/i })
  expect(picker).toHaveValue('all')
  await userEvent.selectOptions(picker, TEST_ENDPOINT_B)
  await waitFor(() => expect(screen.getByText('1')).toBeInTheDocument())
  expect(screen.queryByText('3')).not.toBeInTheDocument()
})

test('the connection pill names the project count, not one endpoint', async () => {
  renderWithProviders(<Shell />)
  expect(await screen.findByText('2 projects')).toBeInTheDocument()
})
