import { render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('./lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('./lib/rpc')>('./lib/rpc')
  return {
    ...actual,
    rpc: vi.fn().mockResolvedValue([]),
    fetchHealth: vi.fn(),
    fetchCapabilities: vi.fn(),
  }
})
import App from './App'
import { fetchCapabilities, fetchHealth } from './lib/rpc'
import { seedConnection } from './test/utils'

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  window.history.replaceState(null, '', '/')
})

test('boots to the connect dialog when no endpoint is stored', () => {
  render(<App />)
  expect(screen.getByText(/connect to your knowledge base/i)).toBeInTheDocument()
})

test('boots to the dashboard when connected', async () => {
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue({
    name: 'vouch',
    level: 3,
    methods: ['kb.list_pending'],
    review_gated: true,
  })
  seedConnection()
  render(<App />)
  expect(
    await screen.findByRole('heading', { name: /dashboard — kb activity/i }),
  ).toBeInTheDocument()
})
