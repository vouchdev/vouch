import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { fetchCapabilities, fetchHealth, rpc, VouchRpcError } from '../lib/rpc'
import { useConnection } from '../connection/ConnectionContext'
import { renderWithProviders, seedConnection, TEST_ENDPOINT } from '../test/utils'
import { chatStorageKey, ChatView } from './ChatView'

const OTHER_ENDPOINT = 'http://127.0.0.1:9999'

function SwitchEndpointProbe() {
  // Adding a project no longer replaces the current one; "switching" is
  // add-then-scope under the multi-project model.
  const { connect, setScope } = useConnection()
  return (
    <button
      onClick={() => void connect({ endpoint: OTHER_ENDPOINT }).then(() => setScope(OTHER_ENDPOINT))}
    >
      switch endpoint
    </button>
  )
}

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.synthesize', 'kb.search', 'kb.read_claim', 'kb.cite', 'kb.why'],
  review_gated: true,
}

const ANSWER = {
  query: 'what does the vouch http server bind',
  answer:
    'The vouch HTTP server binds 127.0.0.1:8731 by default [the-vouch-http-server-binds-127-0-0-1-8731-by-default].',
  claims: ['the-vouch-http-server-binds-127-0-0-1-8731-by-default'],
  gaps: [],
  _meta: { synthesis_confidence: 'medium' as const },
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  seedConnection()
})

async function ask(text: string) {
  // Locate by role rather than placeholder text: the placeholder now varies
  // with search-mode + capability gating, but there's always exactly one
  // chat input on screen.
  await userEvent.type(screen.getByRole('textbox'), text)
  await userEvent.keyboard('{Enter}')
}

test('shows the empty state before any messages', () => {
  renderWithProviders(<ChatView />)
  expect(screen.getByText(/ask your knowledge base/i)).toBeInTheDocument()
})

test('submits a query to kb.synthesize and renders the cited answer', async () => {
  vi.mocked(rpc).mockResolvedValue(ANSWER)
  renderWithProviders(<ChatView />)
  await ask('what does the vouch http server bind')
  expect(await screen.findByText(/binds 127\.0\.0\.1:8731 by default/)).toBeInTheDocument()
  expect(
    screen.getByRole('button', { name: 'the-vouch-http-server-binds-127-0-0-1-8731-by-default' }),
  ).toBeInTheDocument()
  expect(screen.getByText(/medium/i)).toBeInTheDocument()
  expect(rpc).toHaveBeenCalledWith(
    expect.objectContaining({ endpoint: TEST_ENDPOINT }),
    'kb.synthesize',
    { query: 'what does the vouch http server bind', llm: true },
  )
})

test('falls back to deterministic synthesis when the llm is not configured', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method, params) => {
    if (method !== 'kb.synthesize') throw new Error(`unexpected ${method}`)
    if ((params as { llm?: boolean }).llm) {
      throw new VouchRpcError(
        'invalid_params',
        'llm synthesis is not configured — set compile.llm_cmd in .vouch/config.yaml',
      )
    }
    return ANSWER
  })
  renderWithProviders(<ChatView />)
  await ask('what does the vouch http server bind')
  expect(await screen.findByText(/binds 127\.0\.0\.1:8731 by default/)).toBeInTheDocument()
  expect(rpc).toHaveBeenCalledWith(
    expect.anything(),
    'kb.synthesize',
    { query: 'what does the vouch http server bind' },
  )
})

test('a broken llm surfaces as an error instead of silently degrading', async () => {
  vi.mocked(rpc).mockRejectedValue(
    new VouchRpcError('invalid_params', 'compile.llm_cmd failed (1): boom'),
  )
  renderWithProviders(<ChatView />)
  await ask('bind')
  expect((await screen.findAllByText(/llm_cmd failed/)).length).toBeGreaterThan(0)
  expect(rpc).toHaveBeenCalledTimes(1)
})

test('llm answers badge the backend and open page citations as pages', async () => {
  const llmAnswer = {
    query: 'how does auth work',
    answer: 'Access tokens are short-lived JWTs [auth-overview].',
    claims: [],
    pages: ['auth-overview'],
    gaps: [],
    _meta: { synthesis_confidence: 'medium' as const, synthesis_backend: 'llm' },
  }
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.synthesize') return llmAnswer
    if (method === 'kb.read_page')
      return { id: 'auth-overview', title: 'Auth Overview', body: 'JWTs.', type: 'concept', status: 'active' }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ChatView />)
  await ask('how does auth work')
  expect(await screen.findByTestId('synthesis-backend')).toHaveTextContent('llm')
  await userEvent.click(screen.getByRole('button', { name: 'auth-overview' }))
  expect(await screen.findByTestId('drawer')).toBeInTheDocument()
  await waitFor(() =>
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.read_page', { page_id: 'auth-overview' }),
  )
})

test('clicking a citation chip opens the claim drawer', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.synthesize') return ANSWER
    if (method === 'kb.read_claim')
      return {
        id: ANSWER.claims[0],
        text: 'The vouch HTTP server binds 127.0.0.1:8731 by default',
        type: 'observation',
        status: 'working',
        confidence: 0.7,
      }
    if (method === 'kb.cite') return []
    if (method === 'kb.why') return { root: ANSWER.claims[0], node_kind: 'claim', depth: 3, provenance: [] }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ChatView />)
  await ask('bind')
  await userEvent.click(await screen.findByRole('button', { name: ANSWER.claims[0] }))
  expect(await screen.findByTestId('drawer')).toBeInTheDocument()
})

test('renders gaps and the no-answer note when nothing matches', async () => {
  vi.mocked(rpc).mockResolvedValue({
    query: 'kubernetes',
    answer: '',
    claims: [],
    gaps: ['kubernetes'],
    _meta: { synthesis_confidence: 'medium' as const },
  })
  renderWithProviders(<ChatView />)
  await ask('kubernetes')
  expect(await screen.findByText(/no approved claims matched/i)).toBeInTheDocument()
  expect(screen.getByText(/kubernetes/, { selector: 'span' })).toBeInTheDocument()
})

test('renders an error bubble plus an error toast on rpc failure', async () => {
  vi.mocked(rpc).mockRejectedValue(new VouchRpcError('invalid_request', 'query is required'))
  renderWithProviders(<ChatView />)
  await ask('boom')
  expect(await screen.findByText(/query is required/, { selector: 'span' })).toBeInTheDocument()
  expect(screen.getByText('invalid_request')).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('invalid_request: query is required')
})

test('disables the input when kb.synthesize is not advertised', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: ['kb.search'] })
  renderWithProviders(<ChatView />)
  await waitFor(() =>
    expect(screen.getByPlaceholderText(/kb\.synthesize is not advertised/i)).toBeDisabled(),
  )
})

test('disables the search-mode toggle when kb.search is not advertised', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: ['kb.synthesize'] })
  renderWithProviders(<ChatView />)
  const toggle = await screen.findByRole('button', { name: /search mode/i })
  await waitFor(() => expect(toggle).toBeDisabled())
  expect(toggle).toHaveAttribute('title', expect.stringMatching(/kb\.search is not advertised/i))
})

test('persists the thread per endpoint and restores it', async () => {
  vi.mocked(rpc).mockResolvedValue(ANSWER)
  const first = renderWithProviders(<ChatView />)
  await ask('what does the vouch http server bind')
  await screen.findByText(/binds 127\.0\.0\.1:8731/)
  await waitFor(() => expect(localStorage.getItem(chatStorageKey(TEST_ENDPOINT))).toContain('binds 127.0.0.1:8731'))
  first.unmount()
  renderWithProviders(<ChatView />)
  expect(await screen.findByText(/binds 127\.0\.0\.1:8731/)).toBeInTheDocument()
})

test('clear thread wipes messages and storage', async () => {
  vi.mocked(rpc).mockResolvedValue(ANSWER)
  renderWithProviders(<ChatView />)
  await ask('bind')
  await screen.findByText(/binds 127\.0\.0\.1:8731/)
  await userEvent.click(screen.getByRole('button', { name: /clear/i }))
  expect(screen.queryByText(/binds 127\.0\.0\.1:8731/)).not.toBeInTheDocument()
  expect(localStorage.getItem(chatStorageKey(TEST_ENDPOINT))).toBeNull()
})

test('switching endpoints loads the new thread without leaking the old one into its storage', async () => {
  vi.mocked(rpc).mockResolvedValue(ANSWER)
  const bThread = [{ role: 'user', text: 'hello from B' }]
  localStorage.setItem(chatStorageKey(OTHER_ENDPOINT), JSON.stringify(bThread))

  renderWithProviders(
    <>
      <SwitchEndpointProbe />
      <ChatView />
    </>,
  )

  await ask('what does the vouch http server bind')
  await screen.findByText(/binds 127\.0\.0\.1:8731/)

  await userEvent.click(screen.getByRole('button', { name: /switch endpoint/i }))

  await waitFor(() => expect(screen.getByText('hello from B')).toBeInTheDocument())
  expect(screen.queryByText(/binds 127\.0\.0\.1:8731/)).not.toBeInTheDocument()
  expect(screen.queryByText('what does the vouch http server bind')).not.toBeInTheDocument()

  await waitFor(() => {
    const stored = localStorage.getItem(chatStorageKey(OTHER_ENDPOINT))
    expect(stored).toContain('hello from B')
    expect(stored).not.toContain('what does the vouch http server bind')
  })
})

test('drops an in-flight response that resolves after an endpoint switch', async () => {
  const bThread = [{ role: 'user', text: 'hello from B' }]
  localStorage.setItem(chatStorageKey(OTHER_ENDPOINT), JSON.stringify(bThread))

  let resolveAnswer!: (value: typeof ANSWER) => void
  vi.mocked(rpc).mockReturnValue(
    new Promise<typeof ANSWER>((resolve) => {
      resolveAnswer = resolve
    }),
  )

  renderWithProviders(
    <>
      <SwitchEndpointProbe />
      <ChatView />
    </>,
  )

  await ask('what does the vouch http server bind')
  expect(screen.getByText('what does the vouch http server bind')).toBeInTheDocument()

  await userEvent.click(screen.getByRole('button', { name: /switch endpoint/i }))
  await waitFor(() => expect(screen.getByText('hello from B')).toBeInTheDocument())

  // A's request resolves only now — after the thread already belongs to B.
  resolveAnswer(ANSWER)
  await waitFor(() => expect(screen.getByPlaceholderText(/ask the kb/i)).not.toBeDisabled())

  expect(screen.queryByText(/binds 127\.0\.0\.1:8731/)).not.toBeInTheDocument()
  const stored = localStorage.getItem(chatStorageKey(OTHER_ENDPOINT))
  expect(stored).toContain('hello from B')
  expect(stored).not.toContain('binds 127.0.0.1:8731')
})

const SEARCH = {
  backend: 'fts5',
  hits: [
    {
      kind: 'claim',
      id: 'the-vouch-http-server-binds-127-0-0-1-8731-by-default',
      snippet: 'The vouch «HTTP» «server» binds 127.0.0.1:8731 by default',
      score: 2.2e-6,
      backend: 'fts5',
    },
    { kind: 'source', id: 'ea1cc5801740a467', snippet: 'a «server» note', score: 1.1e-6, backend: 'fts5' },
  ],
}

test('/search routes to kb.search and renders highlighted hit cards', async () => {
  vi.mocked(rpc).mockResolvedValue(SEARCH)
  renderWithProviders(<ChatView />)
  await ask('/search http server')
  expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.search', { query: 'http server', limit: 10 })
  const marks = await screen.findAllByText('HTTP', { selector: 'mark' })
  expect(marks.length).toBeGreaterThan(0)
  expect(screen.getByText('fts5', { selector: '[data-testid="search-backend"]' })).toBeInTheDocument()
})

test('search mode toggle routes plain input to kb.search', async () => {
  vi.mocked(rpc).mockResolvedValue(SEARCH)
  renderWithProviders(<ChatView />)
  await userEvent.click(screen.getByRole('button', { name: /search mode/i }))
  await ask('http server')
  await waitFor(() =>
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.search', { query: 'http server', limit: 10 }),
  )
})

test('claim hits open the drawer; source hits are inert', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.search') return SEARCH
    if (method === 'kb.read_claim')
      return { id: SEARCH.hits[0].id, text: 'The vouch HTTP server binds…', type: 'observation', status: 'working', confidence: 0.7 }
    if (method === 'kb.cite') return []
    if (method === 'kb.why') return { root: SEARCH.hits[0].id, node_kind: 'claim', depth: 3, provenance: [] }
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ChatView />)
  await ask('/search http server')
  const claimHit = await screen.findByRole('button', { name: /the-vouch-http-server/i })
  await userEvent.click(claimHit)
  expect(await screen.findByTestId('drawer')).toBeInTheDocument()
  // source hit renders as a non-button card
  expect(screen.queryByRole('button', { name: /ea1cc5801740a467/i })).not.toBeInTheDocument()
})
