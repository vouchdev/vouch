import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'
import { ConnectionProvider } from './ConnectionContext'
import { ConnectDialog } from './ConnectDialog'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth } from '../lib/rpc'

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
})

test('renders with the default endpoint and connects on submit', async () => {
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue({ name: 'vouch', level: 3, methods: [], review_gated: true })
  render(
    <ConnectionProvider>
      <ConnectDialog />
    </ConnectionProvider>,
  )
  const endpoint = screen.getByLabelText(/endpoint/i)
  expect(endpoint).toHaveValue('http://127.0.0.1:8731')
  await userEvent.click(screen.getByRole('button', { name: /connect/i }))
  await waitFor(() => expect(fetchCapabilities).toHaveBeenCalled())
})

test('shows the error when the endpoint is unreachable', async () => {
  vi.mocked(fetchHealth).mockResolvedValue(false)
  render(
    <ConnectionProvider>
      <ConnectDialog />
    </ConnectionProvider>,
  )
  await userEvent.click(screen.getByRole('button', { name: /connect/i }))
  expect(await screen.findByText(/no healthy vouch endpoint/i)).toBeInTheDocument()
})

test('sends the token when provided', async () => {
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue({ name: 'vouch', level: 3, methods: [], review_gated: true })
  render(
    <ConnectionProvider>
      <ConnectDialog />
    </ConnectionProvider>,
  )
  await userEvent.type(screen.getByLabelText(/token/i), 'sekrit')
  await userEvent.click(screen.getByRole('button', { name: /connect/i }))
  await waitFor(() =>
    expect(fetchHealth).toHaveBeenCalledWith(expect.objectContaining({ token: 'sekrit' })),
  )
})
