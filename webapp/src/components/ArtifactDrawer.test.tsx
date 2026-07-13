import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc, VouchRpcError } from '../lib/rpc'
import { makeProject, renderWithProviders, seedConnection } from '../test/utils'
import { ArtifactDrawer } from './ArtifactDrawer'

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.read_claim', 'kb.cite', 'kb.why', 'kb.read_page'],
  review_gated: true,
}

const CLAIM = {
  id: 'the-vouch-http-server-binds-127-0-0-1-8731-by-default',
  text: 'The vouch HTTP server binds 127.0.0.1:8731 by default',
  type: 'observation',
  status: 'working',
  confidence: 0.7,
  created_at: '2026-07-04T02:17:50+00:00',
}

const WHY = {
  root: CLAIM.id,
  node_kind: 'claim',
  depth: 3,
  provenance: [
    { kind: 'approvedBy', target: '03f7', target_kind: 'event', event_ts: null, session_id: null, cycle: false, children: [] },
    { kind: 'cites', target: 'ea1cc580', target_kind: 'source', event_ts: null, session_id: null, cycle: false, children: [] },
  ],
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  seedConnection()
})

test('renders nothing for a null target', () => {
  const { container } = renderWithProviders(<ArtifactDrawer target={null} project={makeProject(CAPS)} onClose={() => {}} />)
  expect(container.querySelector('[data-testid="drawer"]')).toBeNull()
})

test('shows a Delete button when kb.propose_delete is advertised', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.read_claim') return CLAIM
    if (method === 'kb.cite') return []
    if (method === 'kb.why') return WHY
    throw new Error(`unexpected ${method}`)
  })
  const caps = { ...CAPS, methods: [...CAPS.methods, 'kb.propose_delete'] }
  renderWithProviders(
    <ArtifactDrawer target={{ kind: 'claim', id: CLAIM.id }} project={makeProject(caps)} onClose={() => {}} />,
  )
  expect(await screen.findByRole('button', { name: /delete/i })).toBeInTheDocument()
})

test('shows Archive and Supersede for a claim when advertised', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.read_claim') return CLAIM
    if (method === 'kb.cite') return []
    if (method === 'kb.why') return WHY
    throw new Error(`unexpected ${method}`)
  })
  const caps = { ...CAPS, methods: [...CAPS.methods, 'kb.archive', 'kb.supersede'] }
  renderWithProviders(
    <ArtifactDrawer target={{ kind: 'claim', id: CLAIM.id }} project={makeProject(caps)} onClose={() => {}} />,
  )
  expect(await screen.findByRole('button', { name: /archive/i })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /supersede/i })).toBeInTheDocument()
})

test('loads and renders a claim with citations and provenance', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.read_claim') return CLAIM
    if (method === 'kb.cite') return [{ id: 'ea1cc580', title: 'note.txt' }]
    if (method === 'kb.why') return WHY
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ArtifactDrawer target={{ kind: 'claim', id: CLAIM.id }} project={makeProject(CAPS)} onClose={() => {}} />)
  expect(await screen.findByText(CLAIM.text)).toBeInTheDocument()
  expect(screen.getByText(/observation/)).toBeInTheDocument()
  await waitFor(() => expect(screen.getByText(/approvedBy/)).toBeInTheDocument())
  expect(screen.getByText(/cites/)).toBeInTheDocument()
})

test('close button fires onClose', async () => {
  // Per-method mock: a blanket mockResolvedValue(CLAIM) would make kb.why
  // return a Claim and crash the provenance render on `.provenance.length`.
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.read_claim') return CLAIM
    if (method === 'kb.cite') return []
    if (method === 'kb.why') return { root: CLAIM.id, node_kind: 'claim', depth: 3, provenance: [] }
    throw new Error(`unexpected ${method}`)
  })
  const onClose = vi.fn()
  renderWithProviders(<ArtifactDrawer target={{ kind: 'claim', id: CLAIM.id }} project={makeProject(CAPS)} onClose={onClose} />)
  await userEvent.click(await screen.findByRole('button', { name: /close/i }))
  expect(onClose).toHaveBeenCalled()
})

test('shows an ErrorCard when the artifact cannot be read', async () => {
  vi.mocked(rpc).mockRejectedValue(new VouchRpcError('not_found', 'claim missing-id not found'))
  renderWithProviders(<ArtifactDrawer target={{ kind: 'claim', id: 'missing-id' }} project={makeProject(CAPS)} onClose={() => {}} />)
  expect(await screen.findByText(/claim missing-id not found/)).toBeInTheDocument()
})
