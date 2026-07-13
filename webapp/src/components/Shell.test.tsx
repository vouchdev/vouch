import { screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn().mockResolvedValue([]), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth } from '../lib/rpc'
import { renderWithProviders, seedConnection } from '../test/utils'
import { Shell } from './Shell'

const CAPS = { name: 'vouch', level: 3, methods: ['kb.list_pending'], review_gated: true }

function app() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route path="/" element={<div>home content</div>} />
      </Route>
    </Routes>
  )
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
})

test('shows the connect dialog when disconnected', () => {
  renderWithProviders(app())
  expect(screen.getByText(/connect to your knowledge base/i)).toBeInTheDocument()
})

test('shows nav, endpoint pill, and outlet content when connected', async () => {
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  seedConnection()
  renderWithProviders(app())
  expect(screen.getByRole('link', { name: /chat/i })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /review/i })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /browse/i })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /stats/i })).toBeInTheDocument()
  expect(screen.getByText('home content')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByText('127.0.0.1:8731')).toBeInTheDocument())
  expect(screen.queryByText(/connect to your knowledge base/i)).not.toBeInTheDocument()
})
