import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { ALL_SCOPE, ConnectionProvider, STORAGE_KEY, STORAGE_KEY_V2, useConnection } from './ConnectionContext'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth } from '../lib/rpc'

const CAPS = { name: 'vouch', level: 3, methods: ['kb.status'], review_gated: true }
const A = 'http://127.0.0.1:8731'
const B = 'http://127.0.0.1:8732'

function Probe() {
  const c = useConnection()
  return (
    <div>
      <span data-testid="labels">{c.projects.map((p) => p.label).join(',')}</span>
      <span data-testid="scope">{c.scope}</span>
      <span data-testid="scoped">{c.scoped.length}</span>
      <span data-testid="aggregated">{String(c.aggregated)}</span>
      <button onClick={() => void c.connect({ endpoint: B, label: 'beta' })}>add-b</button>
      <button onClick={() => c.setScope(B)}>scope-b</button>
      <button onClick={() => c.removeProject(B)}>remove-b</button>
    </div>
  )
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
})

test('migrates a v1 single connection into one project', () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ endpoint: A }))
  render(
    <ConnectionProvider>
      <Probe />
    </ConnectionProvider>,
  )
  expect(screen.getByTestId('labels')).toHaveTextContent('127.0.0.1:8731')
  expect(screen.getByTestId('scope')).toHaveTextContent(ALL_SCOPE)
  expect(screen.getByTestId('aggregated')).toHaveTextContent('false')
})

test('adding a second project aggregates and persists to v2', async () => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ endpoint: A }))
  render(
    <ConnectionProvider>
      <Probe />
    </ConnectionProvider>,
  )
  await act(() => screen.getByText('add-b').click())
  await waitFor(() => expect(screen.getByTestId('labels')).toHaveTextContent('127.0.0.1:8731,beta'))
  expect(screen.getByTestId('aggregated')).toHaveTextContent('true')
  const stored = JSON.parse(localStorage.getItem(STORAGE_KEY_V2)!) as {
    projects: { endpoint: string }[]
  }
  expect(stored.projects.map((p) => p.endpoint)).toEqual([A, B])
})

test('scoping narrows to one project and removal degrades the scope to all', async () => {
  localStorage.setItem(
    STORAGE_KEY_V2,
    JSON.stringify({ projects: [{ endpoint: A }, { endpoint: B, label: 'beta' }], scope: ALL_SCOPE }),
  )
  render(
    <ConnectionProvider>
      <Probe />
    </ConnectionProvider>,
  )
  expect(screen.getByTestId('scoped')).toHaveTextContent('2')
  await act(() => screen.getByText('scope-b').click())
  expect(screen.getByTestId('scope')).toHaveTextContent(B)
  expect(screen.getByTestId('scoped')).toHaveTextContent('1')
  expect(screen.getByTestId('aggregated')).toHaveTextContent('false')
  await act(() => screen.getByText('remove-b').click())
  expect(screen.getByTestId('scope')).toHaveTextContent(ALL_SCOPE)
  expect(screen.getByTestId('labels')).toHaveTextContent('127.0.0.1:8731')
})
