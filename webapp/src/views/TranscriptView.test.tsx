import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { rpc } from '../lib/rpc'
import { renderWithProviders } from '../test/utils'
import { TranscriptView } from './TranscriptView'

const conn = { endpoint: 'http://127.0.0.1:8731' }

beforeEach(() => {
  vi.clearAllMocks()
})

describe('TranscriptView', () => {
  it('renders the degraded observation timeline', async () => {
    vi.mocked(rpc).mockResolvedValue({
      available: false,
      reason: 'raw transcript not found',
      observations: [{ ts: 1, tool: 'Edit', summary: 'Edited types.go' }],
    })
    renderWithProviders(<TranscriptView conn={conn} sessionId="sid-x" />)
    await waitFor(() => expect(screen.getByText('Edited types.go')).toBeInTheDocument())
    expect(screen.getByText(/original transcript unavailable/i)).toBeInTheDocument()
  })

  it('drills into a subagent and back', async () => {
    vi.mocked(rpc).mockImplementation(async (_c, _m, params) => {
      const id = (params as { session_id: string }).session_id
      const blocks =
        id === 'child-9'
          ? [{ type: 'text', text: 'child says hi' }]
          : [
              {
                type: 'tool_use',
                id: 't1',
                name: 'Task',
                input: { prompt: 'go' },
                result: { content: 'done', is_error: false, subagent_session_id: 'child-9' },
              },
            ]
      return {
        available: true,
        source: { agent: 'claude', path: '/x' },
        session: {
          id,
          agent: 'claude',
          cwd: null,
          git_branch: null,
          title: null,
          started_at: null,
          ended_at: null,
          model: null,
          tokens: { input: 0, output: 0, cache_read: 0, cache_creation: 0 },
        },
        messages: [
          { role: 'assistant', id: 'm', model: null, timestamp: null, tokens: null, blocks },
        ],
        truncated: false,
      }
    })
    renderWithProviders(<TranscriptView conn={conn} sessionId="parent-1" />)
    await waitFor(() => expect(screen.getByText('Task')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /Task/i }))
    await userEvent.click(screen.getByRole('button', { name: /view subagent/i }))
    await waitFor(() => expect(screen.getByText('child says hi')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /back to parent/i }))
    await waitFor(() => expect(screen.getByText('Task')).toBeInTheDocument())
  })
})
