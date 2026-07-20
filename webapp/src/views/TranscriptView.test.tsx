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
    await userEvent.click(await screen.findByRole('button', { name: /full transcript/i }))
    await waitFor(() => expect(screen.getByText('Task')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /Task/i }))
    await userEvent.click(screen.getByRole('button', { name: /view subagent/i }))
    await waitFor(() => expect(screen.getByText('child says hi')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /back to parent/i }))
    await waitFor(() => expect(screen.getByText('Task')).toBeInTheDocument())
  })
})

const noisyTranscript = {
  available: true,
  source: { agent: 'claude', path: '/x' },
  session: {
    id: 's1',
    agent: 'claude',
    cwd: null,
    git_branch: null,
    title: null,
    started_at: null,
    ended_at: null,
    model: null,
    tokens: { input: 0, output: 0, cache_read: 0, cache_creation: 0 },
  },
  grading_available: true,
  messages: [
    {
      role: 'user',
      id: null,
      model: null,
      timestamp: null,
      tokens: null,
      noise: 'system-reminder',
      blocks: [{ type: 'text', text: 'recall blob', noise: 'system-reminder' }],
    },
    {
      role: 'user',
      id: null,
      model: null,
      timestamp: null,
      tokens: null,
      noise: 'hook-context',
      blocks: [{ type: 'text', text: 'injected kb context', noise: 'hook-context' }],
    },
    {
      role: 'user',
      id: null,
      model: null,
      timestamp: null,
      tokens: null,
      blocks: [
        { type: 'text', text: 'inline reminder', noise: 'system-reminder' },
        { type: 'text', text: 'fix the login bug' },
      ],
    },
    {
      role: 'assistant',
      id: 'm0',
      model: null,
      timestamp: null,
      tokens: null,
      blocks: [
        {
          type: 'tool_use',
          id: 't1',
          name: 'Bash',
          input: { command: 'pytest -q' },
          result: { content: 'ok', is_error: false, subagent_session_id: null },
        },
      ],
    },
    {
      role: 'assistant',
      id: 'm1',
      model: null,
      timestamp: null,
      tokens: null,
      blocks: [{ type: 'text', text: 'on it' }],
    },
  ],
  truncated: false,
}

describe('TranscriptView noise filtering', () => {
  it('dialog view shows only prompts and replies, folding work into stubs', async () => {
    vi.mocked(rpc).mockResolvedValue(noisyTranscript)
    renderWithProviders(<TranscriptView conn={conn} sessionId="s1" />)
    await waitFor(() => expect(screen.getByText('fix the login bug')).toBeInTheDocument())
    // injected content is silently absent from the dialog
    expect(screen.queryByText('recall blob')).not.toBeInTheDocument()
    expect(screen.queryByText('injected kb context')).not.toBeInTheDocument()
    expect(screen.queryByText('inline reminder')).not.toBeInTheDocument()
    // the tool-only assistant message folds into a working-steps stub
    const stub = screen.getByRole('button', { name: /1 working step hidden \(1 tool run\)/i })
    expect(screen.queryByText('Bash')).not.toBeInTheDocument()
    await userEvent.click(stub)
    await waitFor(() => expect(screen.getByText('Bash')).toBeInTheDocument())
  })

  it('full transcript brings back tools and stubs injected messages', async () => {
    vi.mocked(rpc).mockResolvedValue(noisyTranscript)
    renderWithProviders(<TranscriptView conn={conn} sessionId="s1" />)
    await waitFor(() => expect(screen.getByText('fix the login bug')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /full transcript/i }))
    await waitFor(() => expect(screen.getByText('Bash')).toBeInTheDocument())
    // injected messages stay behind a stub until expanded
    expect(screen.queryByText('recall blob')).not.toBeInTheDocument()
    await userEvent.click(
      screen.getByRole('button', { name: /▸ 2 injected\/system messages hidden/i }),
    )
    await waitFor(() => expect(screen.getByText('recall blob')).toBeInTheDocument())
    expect(screen.getByText('injected kb context')).toBeInTheDocument()
  })

  it('full transcript stubs a noise block inside a substantive message', async () => {
    vi.mocked(rpc).mockResolvedValue(noisyTranscript)
    renderWithProviders(<TranscriptView conn={conn} sessionId="s1" />)
    await waitFor(() => expect(screen.getByText('fix the login bug')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /full transcript/i }))
    expect(screen.queryByText('inline reminder')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /system-reminder hidden/i }))
    await waitFor(() => expect(screen.getByText('inline reminder')).toBeInTheDocument())
  })

  it('the global toggle reveals and re-hides injected messages', async () => {
    vi.mocked(rpc).mockResolvedValue(noisyTranscript)
    renderWithProviders(<TranscriptView conn={conn} sessionId="s1" />)
    await waitFor(() => expect(screen.getByText('fix the login bug')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /full transcript/i }))
    await userEvent.click(
      screen.getByRole('button', { name: /2 injected\/system messages hidden — show/i }),
    )
    await waitFor(() => expect(screen.getByText('recall blob')).toBeInTheDocument())
    await userEvent.click(
      screen.getByRole('button', { name: /hide injected\/system messages/i }),
    )
    await waitFor(() => expect(screen.queryByText('recall blob')).not.toBeInTheDocument())
  })

  it('grades on demand and renders key/low annotations', async () => {
    const graded = {
      ...noisyTranscript,
      grading: { graded_at: '2026-07-14T00:00:00Z', cached: false, graded_messages: 2 },
      messages: noisyTranscript.messages.map((m, i) =>
        i === 2
          ? { ...m, relevance: { grade: 'key', note: 'the actual ask' } }
          : i === 4
            ? { ...m, relevance: { grade: 'low', note: null } }
            : m,
      ),
    }
    vi.mocked(rpc).mockImplementation(async (_c, _m, params) =>
      (params as Record<string, unknown>).grade ? graded : noisyTranscript,
    )
    renderWithProviders(<TranscriptView conn={conn} sessionId="s1" />)
    await waitFor(() => expect(screen.getByText('fix the login bug')).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /grade relevance with llm/i }))
    await waitFor(() => expect(screen.getByText(/key moment/i)).toBeInTheDocument())
    expect(screen.getByText(/the actual ask/)).toBeInTheDocument()
    expect(screen.getByText(/graded/)).toBeInTheDocument()
    // low-relevance dialog collapses its body behind the header chip
    expect(screen.queryByText('on it')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /low relevance/i }))
    await waitFor(() => expect(screen.getByText('on it')).toBeInTheDocument())
  })
})
