import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ConnectionProvider, STORAGE_KEY, STORAGE_KEY_V2, useConnection } from './ConnectionContext'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return {
    ...actual,
    fetchHealth: vi.fn(),
    fetchCapabilities: vi.fn(),
  }
})
import { fetchCapabilities, fetchHealth } from '../lib/rpc'

const CAPS = { name: 'vouch', level: 3, methods: ['kb.status', 'kb.approve'], review_gated: true }

function Probe() {
  const c = useConnection()
  return (
    <div>
      <span data-testid="endpoint">{c.conn?.endpoint ?? 'none'}</span>
      <span data-testid="health">{c.health}</span>
      <span data-testid="can-approve">{String(c.hasMethod('kb.approve'))}</span>
      <button onClick={() => c.connect({ endpoint: 'http://127.0.0.1:9999' })}>go</button>
      <button onClick={() => c.disconnect()}>bye</button>
    </div>
  )
}

beforeEach(() => localStorage.clear())
afterEach(() => vi.clearAllMocks())

test('starts disconnected with empty storage', () => {
  render(
    <ConnectionProvider>
      <Probe />
    </ConnectionProvider>,
  )
  expect(screen.getByTestId('endpoint')).toHaveTextContent('none')
})

test('connect() validates, persists, and exposes capabilities', async () => {
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  render(
    <ConnectionProvider>
      <Probe />
    </ConnectionProvider>,
  )
  await act(() => screen.getByText('go').click())
  await waitFor(() => expect(screen.getByTestId('endpoint')).toHaveTextContent('http://127.0.0.1:9999'))
  expect(screen.getByTestId('health')).toHaveTextContent('ok')
  expect(screen.getByTestId('can-approve')).toHaveTextContent('true')
  const stored = JSON.parse(localStorage.getItem(STORAGE_KEY_V2)!) as {
    projects: { endpoint: string }[]
  }
  expect(stored.projects.map((p) => p.endpoint)).toEqual(['http://127.0.0.1:9999'])
})

test('connect() does not re-validate the conn it just validated', async () => {
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  render(
    <ConnectionProvider>
      <Probe />
    </ConnectionProvider>,
  )
  await act(() => screen.getByText('go').click())
  await waitFor(() => expect(screen.getByTestId('endpoint')).toHaveTextContent('http://127.0.0.1:9999'))
  expect(screen.getByTestId('health')).toHaveTextContent('ok')
  expect(fetchHealth).toHaveBeenCalledTimes(1)
  expect(fetchCapabilities).toHaveBeenCalledTimes(1)
})

test('connect() rejects when health check fails and stays disconnected', async () => {
  vi.mocked(fetchHealth).mockResolvedValue(false)
  let caught: unknown
  function Trier() {
    const c = useConnection()
    return (
      <button onClick={() => c.connect({ endpoint: 'http://127.0.0.1:1' }).catch((e) => (caught = e))}>
        try
      </button>
    )
  }
  render(
    <ConnectionProvider>
      <Trier />
      <Probe />
    </ConnectionProvider>,
  )
  await act(() => screen.getByText('try').click())
  await waitFor(() => expect(caught).toBeInstanceOf(Error))
  expect(screen.getByTestId('endpoint')).toHaveTextContent('none')
  expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
})

test('restores a stored connection on mount and validates it', async () => {
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ endpoint: 'http://127.0.0.1:8731' }))
  render(
    <ConnectionProvider>
      <Probe />
    </ConnectionProvider>,
  )
  expect(screen.getByTestId('endpoint')).toHaveTextContent('http://127.0.0.1:8731')
  await waitFor(() => expect(screen.getByTestId('health')).toHaveTextContent('ok'))
})

test('disconnect clears state and storage', async () => {
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ endpoint: 'http://127.0.0.1:8731' }))
  render(
    <ConnectionProvider>
      <Probe />
    </ConnectionProvider>,
  )
  await act(() => screen.getByText('bye').click())
  expect(screen.getByTestId('endpoint')).toHaveTextContent('none')
  expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
})
