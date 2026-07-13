import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc, VouchRpcError } from '../lib/rpc'
import { renderWithProviders, seedConnection } from '../test/utils'
import { ReviewView } from './ReviewView'

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.list_sessions', 'kb.summarize_session'],
  review_gated: true,
}

const CAPS_WITH_TRANSCRIPT = {
  ...CAPS,
  methods: ['kb.list_sessions', 'kb.summarize_session', 'kb.session_transcript'],
}

const SESSIONS = {
  sessions: [
    {
      session_id: 'sess-open',
      stage: 'buffer',
      proposal_id: null,
      kind: null,
      title: null,
      summarized: false,
      observations: 3,
      last_activity: '2026-07-04T10:00:00+00:00',
    },
    {
      session_id: 'sess-filed',
      stage: 'pending',
      proposal_id: 'prop-1',
      kind: 'claim',
      title: 'session: fix the parser',
      summarized: false,
      observations: 12,
      last_activity: '2026-07-04T11:30:00+00:00',
    },
    {
      session_id: 'sess-done',
      stage: 'pending',
      proposal_id: 'prop-2',
      kind: 'claim',
      title: 'session: already summarized',
      summarized: true,
      observations: 7,
      last_activity: '2026-07-04T09:00:00+00:00',
    },
  ],
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  seedConnection()
})

test('shows the empty state when there are no sessions', async () => {
  vi.mocked(rpc).mockResolvedValue({ sessions: [] })
  renderWithProviders(<ReviewView />)
  expect(await screen.findByText(/no captured sessions/i)).toBeInTheDocument()
})

test('lists all sessions including already-summarized ones', async () => {
  vi.mocked(rpc).mockResolvedValue(SESSIONS)
  renderWithProviders(<ReviewView />)
  // title fallback chain: title, then session_id
  expect(await screen.findByText('session: fix the parser')).toBeInTheDocument()
  expect(screen.getAllByText('sess-open').length).toBeGreaterThan(0)
  // summarized sessions are now shown too (viewable, read-only)
  expect(screen.getByText('session: already summarized')).toBeInTheDocument()
  // stage / summarized badges
  expect(screen.getByText('open buffer')).toBeInTheDocument()
  expect(screen.getByText('needs summary')).toBeInTheDocument()
  expect(screen.getByText('summarized')).toBeInTheDocument()
  // observation counts and sliced timestamps
  expect(screen.getByText(/12 observations/)).toBeInTheDocument()
  expect(screen.getByText(/2026-07-04 11:30:00/)).toBeInTheDocument()
})

test('renders the transcript for the selected session and lets you summarize it', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS_WITH_TRANSCRIPT)
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_sessions') return SESSIONS
    if (method === 'kb.session_transcript') {
      return {
        available: true,
        source: { agent: 'claude', path: '/x' },
        session: {
          id: 'sess-filed',
          agent: 'claude',
          cwd: '/repo',
          git_branch: 'main',
          title: 'session: fix the parser',
          started_at: null,
          ended_at: null,
          model: 'claude-opus-4-8',
          tokens: { input: 1, output: 1, cache_read: 0, cache_creation: 0 },
        },
        messages: [
          {
            role: 'assistant',
            id: 'm1',
            model: 'claude-opus-4-8',
            timestamp: null,
            tokens: null,
            blocks: [{ type: 'text', text: 'transcript in review pane' }],
          },
        ],
        truncated: false,
      }
    }
    if (method === 'kb.summarize_session')
      return { session_id: 'sess-filed', summarized: true, proposal_id: 'prop-1' }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ReviewView />)
  await userEvent.click(await screen.findByText('session: fix the parser'))
  // transcript renders in the detail pane...
  expect(await screen.findByText('transcript in review pane')).toBeInTheDocument()
  // ...and Summarize still works from the same pane
  await userEvent.click(screen.getByRole('button', { name: 'Summarize' }))
  await waitFor(() =>
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.summarize_session', {
      session_id: 'sess-filed',
    }),
  )
})

test('Summarize calls kb.summarize_session for the selected session and refetches', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_sessions') return SESSIONS
    if (method === 'kb.summarize_session')
      return { session_id: 'sess-filed', summarized: true, proposal_id: 'prop-1' }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ReviewView />)
  await userEvent.click(await screen.findByText('session: fix the parser'))
  await userEvent.click(screen.getByRole('button', { name: 'Summarize' }))
  await waitFor(() =>
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.summarize_session', {
      session_id: 'sess-filed',
    }),
  )
  expect(await screen.findByText(/summary ready — moved to pending/i)).toBeInTheDocument()
  // success invalidates ['sessions'] — the active list refetches
  await waitFor(() =>
    expect(
      vi.mocked(rpc).mock.calls.filter((c) => c[1] === 'kb.list_sessions').length,
    ).toBeGreaterThanOrEqual(2),
  )
})

test('a skipped result surfaces an error toast naming the reason', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_sessions') return SESSIONS
    if (method === 'kb.summarize_session')
      return { session_id: 'sess-filed', summarized: false, skipped: 'not-configured' }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ReviewView />)
  await userEvent.click(await screen.findByText('session: fix the parser'))
  await userEvent.click(screen.getByRole('button', { name: 'Summarize' }))
  expect(
    await screen.findByText(/not-configured: set capture\.summary_llm_cmd/i),
  ).toBeInTheDocument()
})

test('an rpc error surfaces an error toast', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_sessions') return SESSIONS
    if (method === 'kb.summarize_session')
      throw new VouchRpcError('internal_error', 'summary command exited 1')
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ReviewView />)
  await userEvent.click(await screen.findByText('session: fix the parser'))
  await userEvent.click(screen.getByRole('button', { name: 'Summarize' }))
  expect(await screen.findByText(/internal_error: summary command exited 1/i)).toBeInTheDocument()
})

test('shows an unavailable state when kb.list_sessions is not advertised', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: [] })
  renderWithProviders(<ReviewView />)
  expect(await screen.findByText(/review is not available on this endpoint/i)).toBeInTheDocument()
  expect(screen.queryByText(/loading sessions/i)).not.toBeInTheDocument()
})

test('hides the Summarize button when kb.summarize_session is not advertised', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: ['kb.list_sessions'] })
  vi.mocked(rpc).mockResolvedValue(SESSIONS)
  renderWithProviders(<ReviewView />)
  await userEvent.click(await screen.findByText('session: fix the parser'))
  expect(screen.queryByRole('button', { name: 'Summarize' })).not.toBeInTheDocument()
  expect(screen.getByText(/kb\.summarize_session is not advertised/i)).toBeInTheDocument()
})
