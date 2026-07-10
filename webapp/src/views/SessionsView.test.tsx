import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc } from '../lib/rpc'
import { renderWithProviders, seedConnection } from '../test/utils'
import { SessionsView } from './SessionsView'

const CAPS = {
  name: 'vouch',
  version: '1',
  level: 3,
  methods: ['kb.list_sessions', 'kb.session_transcript'],
  review_gated: true,
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS as never)
  seedConnection()
})

describe('SessionsView', () => {
  it('lists sessions and renders a picked transcript', async () => {
    vi.mocked(rpc).mockImplementation(async (_c, method) => {
      if (method === 'kb.list_sessions') {
        return {
          sessions: [
            {
              session_id: 'sid-1',
              stage: 'buffer',
              proposal_id: null,
              kind: null,
              title: 'Fix parser',
              summarized: false,
              observations: 3,
              last_activity: '2026-07-10T00:00:00Z',
            },
          ],
        }
      }
      if (method === 'kb.session_transcript') {
        return {
          available: true,
          source: { agent: 'claude', path: '/x' },
          session: {
            id: 'sid-1',
            agent: 'claude',
            cwd: '/repo',
            git_branch: 'main',
            title: 'Fix parser',
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
              blocks: [{ type: 'text', text: 'hello from claude' }],
            },
          ],
          truncated: false,
        }
      }
      return {}
    })
    renderWithProviders(<SessionsView />, { route: '/sessions' })
    await waitFor(() => expect(screen.getByText('Fix parser')).toBeInTheDocument())
    await userEvent.click(screen.getByText('Fix parser'))
    await waitFor(() => expect(screen.getByText('hello from claude')).toBeInTheDocument())
  })
})
