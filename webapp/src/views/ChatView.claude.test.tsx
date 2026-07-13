import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { stashStartHere } from '../lib/claude'
import { fetchCapabilities, fetchHealth } from '../lib/rpc'
import { renderWithProviders, seedConnection } from '../test/utils'
import { ChatView } from './ChatView'

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.synthesize', 'kb.search'],
  review_gated: true,
}

function ndjsonResponse(lines: object[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      for (const l of lines) c.enqueue(new TextEncoder().encode(JSON.stringify(l) + '\n'))
      c.close()
    },
  })
  return { ok: true, body: stream } as unknown as Response
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  seedConnection()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

test('claude mode toggle changes the composer and sends orders to the bridge', async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    ndjsonResponse([
      { type: 'system', subtype: 'init', session_id: 'new-session-1' },
      { type: 'result', subtype: 'success', is_error: false, result: 'ran the tests — all green', session_id: 'new-session-1' },
    ]),
  )
  vi.stubGlobal('fetch', fetchMock)

  renderWithProviders(<ChatView />)
  await userEvent.click(screen.getByRole('button', { name: /claude mode/i }))
  expect(screen.getByPlaceholderText(/order claude code/i)).toBeInTheDocument()

  await userEvent.type(screen.getByPlaceholderText(/order claude code/i), 'run the tests')
  await userEvent.click(screen.getByRole('button', { name: /^send$/i }))

  await waitFor(() => expect(screen.getByText(/all green/)).toBeInTheDocument())
  expect(fetchMock).toHaveBeenCalledWith(
    '/claude/run',
    expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'x-claude-bridge': '1' }),
    }),
  )
  const sent = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
  expect(sent.prompt).toBe('run the tests')
  expect(sent.resume).toBeUndefined()
})

test('Start Here handoff enters claude mode bound to the claim session', async () => {
  stashStartHere({
    claimId: 'c1',
    text: 'the http server binds 8731',
    sessionId: '3cd62baa-db5d-42fb-8522-2fde434541ae',
  })
  renderWithProviders(<ChatView />, { route: '/chat?mode=claude' })
  // composer is prefilled with the claim and the session chip is shown
  const input = screen.getByPlaceholderText(/order claude code/i) as HTMLInputElement
  await waitFor(() => expect(input.value).toContain('the http server binds 8731'))
  expect(screen.getByText(/resuming session/i)).toBeInTheDocument()
  expect(screen.getByText('3cd62baa')).toBeInTheDocument()
  // and the handoff is consumed — a reload must not re-apply it
  expect(sessionStorage.getItem('vouch-ui.start-here')).toBeNull()
})

test('bridge errors surface as an error bubble', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(ndjsonResponse([{ type: 'bridge_error', message: 'failed to spawn claude' }])),
  )
  renderWithProviders(<ChatView />, { route: '/chat?mode=claude' })
  await userEvent.type(screen.getByPlaceholderText(/order claude code/i), 'do a thing')
  await userEvent.click(screen.getByRole('button', { name: /^send$/i }))
  await waitFor(() => expect(screen.getByText(/failed to spawn claude/)).toBeInTheDocument())
})
