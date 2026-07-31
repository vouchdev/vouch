import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc } from '../lib/rpc'
import { renderWithProviders, seedConnection } from '../test/utils'
import { MemoryNetworkView } from './MemoryNetworkView'

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.graph_export', 'kb.read_claim', 'kb.read_page'],
  review_gated: true,
}

const GRAPH = {
  nodes: [
    { id: 'c-new', kind: 'claim', label: 'the newer fact', status: 'working' },
    { id: 'c-old', kind: 'claim', label: 'the older fact', status: 'superseded' },
    { id: 'page-alpha', kind: 'page', label: 'Alpha', status: 'active' },
    { id: 'src-1', kind: 'source', label: 'src-1', status: '' },
    {
      id: '20260731-120000-abcd1234',
      kind: 'proposal',
      label: 'an unreviewed fact',
      status: 'pending',
    },
  ],
  edges: [
    { src: 'page-alpha', dst: 'c-new', kind: 'embeds' },
    { src: 'c-new', dst: 'src-1', kind: 'cites' },
    { src: 'c-new', dst: 'c-old', kind: 'supersedes' },
    { src: '20260731-120000-abcd1234', dst: 'src-1', kind: 'cites' },
  ],
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.graph_export') return { format: 'json', graph: GRAPH }
    if (method === 'kb.read_claim') {
      return {
        id: 'c-new',
        text: 'the newer fact',
        type: 'observation',
        status: 'working',
        confidence: 0.9,
      }
    }
    throw new Error(`unexpected ${method}`)
  })
  seedConnection()
})

test('draws a node per artifact and summarises the graph for a screen reader', async () => {
  renderWithProviders(<MemoryNetworkView />)
  expect(await screen.findByText('the newer fact')).toBeInTheDocument()
  expect(screen.getByText('an unreviewed fact')).toBeInTheDocument()
  expect(screen.getByText(/5 nodes and 4 links, 1 pending review/)).toBeInTheDocument()
})

test('a node carries its kind and status in its accessible name', async () => {
  renderWithProviders(<MemoryNetworkView />)
  expect(await screen.findByRole('button', { name: 'claim the older fact (superseded)' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'source src-1' })).toBeInTheDocument()
})

test('clicking an approved node opens the artifact drawer', async () => {
  renderWithProviders(<MemoryNetworkView />)
  await userEvent.click(await screen.findByRole('button', { name: /claim the newer fact/ }))
  expect(await screen.findByTestId('drawer')).toBeInTheDocument()
})

test('a pending proposal is not a readable artifact, so it points at review instead', async () => {
  renderWithProviders(<MemoryNetworkView />)
  await userEvent.click(await screen.findByRole('button', { name: /proposal an unreviewed fact/ }))
  expect(await screen.findByText(/decide it under Review or Pending/)).toBeInTheDocument()
  expect(screen.queryByTestId('drawer')).not.toBeInTheDocument()
})

test('the pending-frontier filter narrows to proposals and what they touch', async () => {
  renderWithProviders(<MemoryNetworkView />)
  expect(await screen.findByText('the newer fact')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('checkbox', { name: /pending frontier only/i }))
  expect(screen.getByText('an unreviewed fact')).toBeInTheDocument()
  expect(screen.getByText('src-1')).toBeInTheDocument()
  expect(screen.queryByText('the newer fact')).not.toBeInTheDocument()
})

test('zoom controls scale the canvas and reset returns it', async () => {
  const { container } = renderWithProviders(<MemoryNetworkView />)
  await screen.findByText('the newer fact')
  const canvas = () => container.querySelector('svg g[transform]')
  expect(canvas()).toHaveAttribute('transform', 'translate(0 0) scale(1)')
  await userEvent.click(screen.getByRole('button', { name: 'zoom in' }))
  expect(canvas()).toHaveAttribute('transform', 'translate(0 0) scale(1.25)')
  await userEvent.click(screen.getByRole('button', { name: 'reset view' }))
  expect(canvas()).toHaveAttribute('transform', 'translate(0 0) scale(1)')
})

test('an endpoint that does not advertise the export renders a note, not endless loading', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: ['kb.list_claims'] })
  renderWithProviders(<MemoryNetworkView />)
  expect(await screen.findByText(/not available on this endpoint/i)).toBeInTheDocument()
})

test('an empty kb gets an instructive empty state', async () => {
  vi.mocked(rpc).mockResolvedValue({ format: 'json', graph: { nodes: [], edges: [] } })
  renderWithProviders(<MemoryNetworkView />)
  expect(await screen.findByText(/nothing to draw yet/i)).toBeInTheDocument()
})

test('a failed export surfaces the server error', async () => {
  vi.mocked(rpc).mockRejectedValue(new Error("unknown graph format: 'json'"))
  renderWithProviders(<MemoryNetworkView />)
  expect(await screen.findByText(/unknown graph format/)).toBeInTheDocument()
})
