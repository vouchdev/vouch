import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc, VouchRpcError } from '../lib/rpc'
import { renderWithProviders, seedConnection } from '../test/utils'
import { PendingView } from './PendingView'

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.list_pending', 'kb.approve', 'kb.reject'],
  review_gated: true,
}

const PROPOSAL = {
  id: '20260704-021728-c4b86871',
  kind: 'claim',
  proposed_by: 'agent-a',
  session_id: null,
  payload: { text: 'The vouch HTTP server binds 127.0.0.1:8731 by default', confidence: 0.7 },
  status: 'pending',
  proposed_at: '2026-07-04T02:17:28+00:00',
}

const DELETE_PROPOSAL = {
  id: '20260709-del-1',
  kind: 'delete',
  proposed_by: 'agent',
  session_id: null,
  payload: {
    target_kind: 'claim',
    id: 'auth-uses-jwt',
    snapshot: { id: 'auth-uses-jwt', text: 'Auth uses JWT', type: 'observation' },
  },
  status: 'pending',
  proposed_at: '2026-07-09T00:00:00Z',
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  seedConnection()
})

test('shows the empty state when the queue is clear', async () => {
  vi.mocked(rpc).mockResolvedValue([])
  renderWithProviders(<PendingView />)
  expect(await screen.findByText(/queue is clear/i)).toBeInTheDocument()
})

test('renders a delete proposal readably (label + snapshot, not raw json)', async () => {
  vi.mocked(rpc).mockResolvedValue([DELETE_PROPOSAL])
  renderWithProviders(<PendingView />)
  expect(await screen.findByText(/delete claim auth-uses-jwt/i)).toBeInTheDocument()
  await userEvent.click(screen.getByText(/20260709-del-1/))
  expect(screen.getByText('Auth uses JWT')).toBeInTheDocument()
  expect(screen.queryByText(/"snapshot"/)).not.toBeInTheDocument()
})

test('Clear queue rejects every pending row with a shared reason', async () => {
  const P2 = { ...PROPOSAL, id: '20260704-021728-second' }
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_pending') return [PROPOSAL, P2]
    if (method === 'kb.reject') return { proposal_id: 'x', status: 'rejected' }
    return []
  })
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByRole('button', { name: /clear queue/i }))
  await userEvent.type(screen.getByPlaceholderText(/why clear/i), 'housekeeping')
  await userEvent.click(screen.getByRole('button', { name: /reject all/i }))
  await waitFor(() => {
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.reject', {
      proposal_id: PROPOSAL.id,
      reason: 'housekeeping',
    })
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.reject', {
      proposal_id: P2.id,
      reason: 'housekeeping',
    })
  })
})

test('Clear queue is hidden when kb.reject is not advertised', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: ['kb.list_pending'] })
  vi.mocked(rpc).mockResolvedValue([PROPOSAL])
  renderWithProviders(<PendingView />)
  await screen.findByText(/20260704-021728-c4b86871/)
  expect(screen.queryByRole('button', { name: /clear queue/i })).not.toBeInTheDocument()
})

test('lists pending proposals and shows the payload on select', async () => {
  vi.mocked(rpc).mockResolvedValue([PROPOSAL])
  renderWithProviders(<PendingView />)
  const row = await screen.findByText(/20260704-021728-c4b86871/)
  await userEvent.click(row)
  // The text appears twice: queue-row preview + detail <dd> — assert both.
  expect(screen.getAllByText(/binds 127\.0\.0\.1:8731 by default/)).toHaveLength(2)
  expect(screen.getByText('agent-a')).toBeInTheDocument()
})

test('renders a page body as markdown, not a flat string', async () => {
  const pageProposal = {
    ...PROPOSAL,
    id: '20260704-065819-85d593b4',
    kind: 'page',
    payload: {
      title: 'session summary',
      body: '# session: hello\n\n## prompt\n\n> do the thing\n\n## files\n\n- src/a.py\n- src/b.py',
      type: 'session',
    },
  }
  vi.mocked(rpc).mockResolvedValue([pageProposal])
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByText(/20260704-065819-85d593b4/))
  // markdown structure, not raw text: headings and list items exist as elements
  expect(screen.getByRole('heading', { name: 'session: hello' })).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'prompt' })).toBeInTheDocument()
  expect(screen.getByText('src/a.py')).toBeInTheDocument()
  // no literal markdown syntax leaks into the rendered body
  expect(screen.queryByText(/^# session/)).not.toBeInTheDocument()
})

test('merge: selecting two page proposals sends kb.merge_pending and shows the merged result', async () => {
  const pageA = {
    ...PROPOSAL,
    id: 'prop-page-a',
    kind: 'page',
    payload: { title: 'session: do thing 1', body: '# a', type: 'session' },
  }
  const pageB = {
    ...PROPOSAL,
    id: 'prop-page-b',
    kind: 'page',
    payload: { title: 'session: do thing 2', body: '# b', type: 'session' },
  }
  vi.mocked(fetchCapabilities).mockResolvedValue({
    ...CAPS,
    methods: [...CAPS.methods, 'kb.merge_pending'],
  })
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_pending') return [pageA, pageB]
    if (method === 'kb.merge_pending')
      return { proposal_id: 'prop-merged', merged_from: ['prop-page-a', 'prop-page-b'], status: 'pending' }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByLabelText('select prop-page-a'))
  // one selection is not mergeable yet
  expect(screen.queryByRole('button', { name: /merge/i })).not.toBeInTheDocument()
  await userEvent.click(screen.getByLabelText('select prop-page-b'))
  await userEvent.click(screen.getByRole('button', { name: /merge 2 into one/i }))
  await waitFor(() =>
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.merge_pending', {
      proposal_ids: ['prop-page-a', 'prop-page-b'],
    }),
  )
  expect(await screen.findByText(/merged 2 → prop-merged/i)).toBeInTheDocument()
})

test('selection checkbox drives batch-approve; merge button is absent without kb.merge_pending', async () => {
  const pageA = {
    ...PROPOSAL,
    id: 'prop-page-a',
    kind: 'page',
    payload: { title: 'session: do thing 1', body: '# a', type: 'session' },
  }
  vi.mocked(rpc).mockResolvedValue([pageA])
  renderWithProviders(<PendingView />)
  await screen.findByText(/prop-page-a/)
  // the row is approvable, so a selection checkbox is present for batch approve …
  expect(screen.getByLabelText('select prop-page-a')).toBeInTheDocument()
  // … but with kb.merge_pending unadvertised there is no merge action
  await userEvent.click(screen.getByLabelText('select prop-page-a'))
  expect(screen.queryByRole('button', { name: /merge/i })).not.toBeInTheDocument()
})

test('approve removes the proposal from the queue before the server responds (optimistic)', async () => {
  let resolveApprove: (v: unknown) => void = () => {}
  vi.mocked(rpc).mockImplementation((_c, method) => {
    if (method === 'kb.list_pending') return Promise.resolve([PROPOSAL])
    if (method === 'kb.approve') return new Promise((r) => (resolveApprove = r))
    return Promise.reject(new Error(`unexpected ${method}`))
  })
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByText(/20260704-021728-c4b86871/))
  await userEvent.click(screen.getByRole('button', { name: /approve/i }))
  await waitFor(() => expect(screen.queryByText(/20260704-021728-c4b86871/)).not.toBeInTheDocument())
  resolveApprove({ kind: 'claim', id: 'x' })
})

test('approve calls kb.approve and reports success', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_pending') return [PROPOSAL]
    if (method === 'kb.approve') return { kind: 'claim', id: 'the-vouch-http-server-binds' }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByText(/20260704-021728-c4b86871/))
  await userEvent.click(screen.getByRole('button', { name: /approve/i }))
  await waitFor(() =>
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.approve', { proposal_id: PROPOSAL.id }),
  )
  expect(await screen.findByText(/approved → claim\/the-vouch-http-server-binds/i)).toBeInTheDocument()
})

test('reject requires a reason and sends it', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_pending') return [PROPOSAL]
    if (method === 'kb.reject') return { proposal_id: PROPOSAL.id, status: 'rejected' }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByText(/20260704-021728-c4b86871/))
  await userEvent.click(screen.getByRole('button', { name: /^reject$/i }))
  // reason input appears; confirm is disabled until non-empty
  const confirm = screen.getByRole('button', { name: /confirm reject/i })
  expect(confirm).toBeDisabled()
  await userEvent.type(screen.getByPlaceholderText(/why is this rejected/i), 'unsupported claim')
  await userEvent.click(confirm)
  await waitFor(() =>
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.reject', {
      proposal_id: PROPOSAL.id,
      reason: 'unsupported claim',
    }),
  )
})

test('surfaces forbidden_self_approval errors inline', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_pending') return [PROPOSAL]
    if (method === 'kb.approve')
      throw new VouchRpcError('invalid_request', 'forbidden_self_approval: a cannot approve their own proposal')
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByText(/20260704-021728-c4b86871/))
  await userEvent.click(screen.getByRole('button', { name: /approve/i }))
  // Deviation from brief: the same message also lands in a toast (role="status"),
  // so a bare findByText matches two nodes. Scope to the inline ErrorCard
  // (role="alert") to assert the "inline" part of the test name specifically.
  expect(within(await screen.findByRole('alert')).getByText(/forbidden_self_approval/)).toBeInTheDocument()
})

test('shows an unavailable state instead of loading forever when kb.list_pending is not advertised', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: [] })
  renderWithProviders(<PendingView />)
  // caps starts null, so the query briefly runs "enabled" before capabilities
  // load; what matters is that once they load without kb.list_pending, the
  // view settles on the unavailable EmptyState rather than spinning forever.
  expect(await screen.findByText(/review is not available on this endpoint/i)).toBeInTheDocument()
  expect(screen.queryByText(/loading queue/i)).not.toBeInTheDocument()
})

test('hides decision buttons when kb.approve is not advertised', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: ['kb.list_pending'] })
  vi.mocked(rpc).mockResolvedValue([PROPOSAL])
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByText(/20260704-021728-c4b86871/))
  expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument()
  expect(screen.getByText(/read-only/i)).toBeInTheDocument()
})

test('compile bar is absent when kb.compile is not advertised', async () => {
  vi.mocked(rpc).mockResolvedValue([])
  renderWithProviders(<PendingView />)
  await screen.findByText(/queue is clear/i)
  expect(screen.queryByRole('button', { name: /compile wiki/i })).not.toBeInTheDocument()
})

test('compile wiki runs kb.compile from the empty queue and refreshes pending', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({
    ...CAPS,
    methods: [...CAPS.methods, 'kb.compile'],
  })
  const draft = {
    ...PROPOSAL,
    id: 'prop-compiled-1',
    kind: 'page',
    proposed_by: 'wiki-compiler',
    payload: { title: 'Billing Retry Policy', body: '# billing', type: 'decision' },
  }
  let compiled = false
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_pending') return compiled ? [draft] : []
    if (method === 'kb.compile') {
      compiled = true
      return {
        proposed: [{ title: 'Billing Retry Policy', proposal_id: 'prop-compiled-1' }],
        dropped: [{ title: 'Ghost', reason: 'unknown claim id: nope' }],
        draft_count: 2,
        dry_run: false,
      }
    }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByRole('button', { name: /compile wiki/i }))
  await waitFor(() => expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.compile'))
  // toast reports both the filed and the dropped counts
  expect(await screen.findByText(/compiled 1 page draft.*1 dropped by citation checks/i)).toBeInTheDocument()
  // the queue refetched and now shows the compiler's draft
  expect(await screen.findByText(/prop-compiled-1/)).toBeInTheDocument()
  expect(screen.getByText(/by wiki-compiler/)).toBeInTheDocument()
})

test('compile bar nudges when claims were approved after the last compile', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({
    ...CAPS,
    methods: [...CAPS.methods, 'kb.compile', 'kb.audit'],
  })
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_pending') return []
    if (method === 'kb.audit')
      return {
        events: [
          { event: 'compile.run' },
          { event: 'proposal.claim.approve' },
          { event: 'proposal.claim.approve' },
        ],
      }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<PendingView />)
  expect(
    await screen.findByText(/2 claims approved since the last compile/i),
  ).toBeInTheDocument()
})

test('compile bar shows no nudge when the last compile is newer than every approval', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({
    ...CAPS,
    methods: [...CAPS.methods, 'kb.compile', 'kb.audit'],
  })
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_pending') return []
    if (method === 'kb.audit')
      return {
        events: [
          { event: 'proposal.claim.approve' },
          { event: 'proposal.claim.approve' },
          { event: 'compile.run' },
        ],
      }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<PendingView />)
  await screen.findByRole('button', { name: /compile wiki/i })
  expect(await screen.findByText(/distill approved claims into topic pages/i)).toBeInTheDocument()
  expect(screen.queryByText(/approved since the last compile/i)).not.toBeInTheDocument()
})

test('compile surfaces a clean error when llm_cmd is not configured', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({
    ...CAPS,
    methods: [...CAPS.methods, 'kb.compile'],
  })
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_pending') return []
    if (method === 'kb.compile')
      throw new VouchRpcError('invalid_request', 'compile.llm_cmd is not configured')
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<PendingView />)
  await userEvent.click(await screen.findByRole('button', { name: /compile wiki/i }))
  expect(await screen.findByText(/llm_cmd is not configured/)).toBeInTheDocument()
})

test('hides proposals whose session is still awaiting a summary', async () => {
  const sessionProposal = {
    ...PROPOSAL,
    id: 'prop-session-raw',
    proposed_by: 'vouch-capture',
    payload: { text: 'raw capture without a narrative', type: 'session' },
  }
  vi.mocked(fetchCapabilities).mockResolvedValue({
    ...CAPS,
    methods: [...CAPS.methods, 'kb.list_sessions'],
  })
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_pending') return [PROPOSAL, sessionProposal]
    if (method === 'kb.list_sessions')
      return {
        sessions: [
          {
            session_id: 'sess-1',
            stage: 'pending',
            proposal_id: 'prop-session-raw',
            kind: 'claim',
            title: 'session: raw capture',
            summarized: false,
            observations: 4,
            last_activity: '2026-07-04T02:17:28+00:00',
          },
        ],
      }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<PendingView />)
  await screen.findByText(/20260704-021728-c4b86871/)
  // once the sessions list is in, the unsummarized capture is filtered out
  await waitFor(() => expect(screen.queryByText(/prop-session-raw/)).not.toBeInTheDocument())
})
