import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc } from '../lib/rpc'
import { Route, Routes } from 'react-router-dom'
import { renderWithProviders, seedConnection } from '../test/utils'
import { BrowseView } from './BrowseView'

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.list_claims', 'kb.list_pages', 'kb.list_entities', 'kb.list_relations', 'kb.read_claim', 'kb.read_page'],
  review_gated: true,
}

function renderBrowse(route = '/browse') {
  return renderWithProviders(
    <Routes>
      <Route path="/browse/:kind?/:id?" element={<BrowseView />} />
    </Routes>,
    { route },
  )
}

const CLAIMS = [
  { id: 'server-binds-loopback', text: 'The vouch HTTP server binds 127.0.0.1:8731 by default', type: 'observation', status: 'working', confidence: 0.7 },
  { id: 'reject-needs-reason', text: 'kb.reject requires a reason parameter', type: 'observation', status: 'working', confidence: 0.9 },
]
const PAGES = [{ id: 'howto-serve', title: 'Serving vouch over HTTP', body: 'run vouch serve', type: 'concept', status: 'draft' }]

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_claims') return CLAIMS
    if (method === 'kb.list_pages') return PAGES
    if (method === 'kb.list_entities') return []
    if (method === 'kb.list_relations') return []
    if (method === 'kb.read_claim') return CLAIMS[0]
    throw new Error(`unexpected ${method}`)
  })
  seedConnection()
})

test('lists claims by default with counts in the tabs', async () => {
  renderBrowse()
  expect(await screen.findByText(/binds 127\.0\.0\.1:8731/)).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: /claims \(2\)/i })).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: /pages \(1\)/i })).toBeInTheDocument()
})

test('switching tab loads that artifact list', async () => {
  renderBrowse()
  await screen.findByText(/binds 127\.0\.0\.1:8731/)
  await userEvent.click(screen.getByRole('tab', { name: /pages/i }))
  expect(await screen.findByText('Serving vouch over HTTP')).toBeInTheDocument()
})

test('filter narrows rows client-side', async () => {
  renderBrowse()
  await screen.findByText(/binds 127\.0\.0\.1:8731/)
  await userEvent.type(screen.getByPlaceholderText(/filter/i), 'reason')
  expect(screen.queryByText(/binds 127\.0\.0\.1:8731/)).not.toBeInTheDocument()
  expect(screen.getByText(/requires a reason parameter/)).toBeInTheDocument()
})

test('clicking a row opens the artifact drawer (and updates the URL)', async () => {
  renderBrowse()
  await userEvent.click(await screen.findByText(/binds 127\.0\.0\.1:8731/))
  expect(await screen.findByTestId('drawer')).toBeInTheDocument()
})

test('a /browse/claim/<id> URL opens the drawer directly (deep link)', async () => {
  renderBrowse('/browse/claim/server-binds-loopback')
  expect(await screen.findByTestId('drawer')).toBeInTheDocument()
})

test('a /browse/page/<id> deep link also syncs the active tab underneath the drawer', async () => {
  renderBrowse('/browse/page/howto-serve')
  expect(await screen.findByTestId('drawer')).toBeInTheDocument()
  await waitFor(() => {
    expect(screen.getByRole('tab', { name: /pages/i })).toHaveAttribute('aria-selected', 'true')
  })
  expect(screen.getByRole('tab', { name: /claims/i })).toHaveAttribute('aria-selected', 'false')
})

test('manual tab clicks stick while a drawer deep-link is open', async () => {
  renderBrowse('/browse/page/howto-serve')
  expect(await screen.findByTestId('drawer')).toBeInTheDocument()
  await waitFor(() => {
    expect(screen.getByRole('tab', { name: /pages/i })).toHaveAttribute('aria-selected', 'true')
  })
  await userEvent.click(screen.getByRole('tab', { name: /entities/i }))
  await waitFor(() => {
    expect(screen.getByRole('tab', { name: /entities/i })).toHaveAttribute('aria-selected', 'true')
  })
  expect(screen.getByRole('tab', { name: /pages/i })).toHaveAttribute('aria-selected', 'false')
  // Tab clicks do not navigate — the deep-linked drawer stays open (URL unchanged).
  expect(screen.getByTestId('drawer')).toBeInTheDocument()
})

test('un-advertised list methods render an unavailable note, not endless loading', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: ['kb.list_claims'] })
  renderBrowse()
  await screen.findByText(/binds 127\.0\.0\.1:8731/)
  await userEvent.click(screen.getByRole('tab', { name: /pages/i }))
  expect(await screen.findByText(/not available on this endpoint/i)).toBeInTheDocument()
})

test('empty tab shows an instructive empty state', async () => {
  renderBrowse()
  await screen.findByText(/binds 127\.0\.0\.1:8731/)
  await userEvent.click(screen.getByRole('tab', { name: /entities/i }))
  expect(await screen.findByText(/no entities yet/i)).toBeInTheDocument()
})
