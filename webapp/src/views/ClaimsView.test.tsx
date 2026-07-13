import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { START_HERE_KEY } from '../lib/claude'
import { fetchCapabilities, fetchHealth, rpc } from '../lib/rpc'
import { renderWithProviders, seedConnection } from '../test/utils'
import { ClaimsView, provenanceSession } from './ClaimsView'

const CAPS = {
  name: 'vouch',
  level: 3,
  methods: ['kb.list_claims', 'kb.why'],
  review_gated: true,
}

const CLAIM = {
  id: 'openclaw-manifest-version-lockstep',
  text: 'when bumping the package version, update openclaw.plugin.json in the same change',
  type: 'workflow',
  status: 'working',
  confidence: 0.9,
  tags: ['release'],
  created_at: '2026-07-02T06:44:10Z',
}

const WHY = {
  root: CLAIM.id,
  node_kind: 'claim',
  depth: 3,
  provenance: [
    {
      kind: 'approvedBy',
      target: 'evt1',
      target_kind: 'event',
      event_ts: null,
      session_id: '3cd62baa-db5d-42fb-8522-2fde434541ae',
      cycle: false,
      children: [],
    },
  ],
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  seedConnection()
})

test('provenanceSession finds the first session id, even nested', () => {
  expect(provenanceSession([])).toBeNull()
  expect(
    provenanceSession([
      {
        kind: 'cites', target: 's', target_kind: 'source', event_ts: null,
        session_id: null, cycle: false,
        children: [
          {
            kind: 'proposedIn', target: 'x', target_kind: 'event', event_ts: null,
            session_id: 'deep-session', cycle: false, children: [],
          },
        ],
      },
    ]),
  ).toBe('deep-session')
})

test('shows a Delete button on a selected claim when kb.propose_delete is advertised', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({ ...CAPS, methods: [...CAPS.methods, 'kb.propose_delete'] })
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_claims') return [CLAIM]
    if (method === 'kb.why') return WHY
    return []
  })
  renderWithProviders(<ClaimsView />)
  await userEvent.click(await screen.findByText(/openclaw-manifest-version-lockstep/))
  expect(await screen.findByRole('button', { name: /delete/i })).toBeInTheDocument()
})

test('shows Archive and Supersede on a selected claim when advertised', async () => {
  vi.mocked(fetchCapabilities).mockResolvedValue({
    ...CAPS,
    methods: [...CAPS.methods, 'kb.archive', 'kb.supersede'],
  })
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_claims') return [CLAIM]
    if (method === 'kb.why') return WHY
    return []
  })
  renderWithProviders(<ClaimsView />)
  await userEvent.click(await screen.findByText(/openclaw-manifest-version-lockstep/))
  expect(await screen.findByRole('button', { name: /archive/i })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /supersede/i })).toBeInTheDocument()
})

test('hides archived claims from the list (archive makes a claim disappear)', async () => {
  const ARCHIVED = { ...CLAIM, id: 'archived-claim-xyz', status: 'archived' }
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_claims') return [CLAIM, ARCHIVED]
    if (method === 'kb.why') return WHY
    return []
  })
  renderWithProviders(<ClaimsView />)
  expect(await screen.findByText(/openclaw-manifest-version-lockstep/)).toBeInTheDocument()
  expect(screen.queryByText(/archived-claim-xyz/)).not.toBeInTheDocument()
})

test('lists approved claims with Start Here instead of approve/reject', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_claims') return [CLAIM]
    if (method === 'kb.why') return WHY
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ClaimsView />)
  await userEvent.click(await screen.findByText(/openclaw-manifest-version-lockstep/))
  expect(await screen.findByRole('button', { name: /start here/i })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /reject/i })).not.toBeInTheDocument()
  // provenance session surfaced in the caption
  expect(await screen.findByText(/resuming session 3cd62baa/i)).toBeInTheDocument()
})

test('Start Here opens a dialog with the /vouch-start command and closes on Escape', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_claims') return [CLAIM]
    if (method === 'kb.why') return WHY
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ClaimsView />)
  await userEvent.click(await screen.findByText(/openclaw-manifest-version-lockstep/))
  await screen.findByText(/resuming session 3cd62baa/i)
  await userEvent.click(screen.getByRole('button', { name: /start here/i }))
  const dialog = await screen.findByRole('dialog')
  expect(within(dialog).getByText('claude "/vouch-start openclaw-manifest-version-lockstep"')).toBeInTheDocument()
  // provenance session known → the resume command is offered too
  expect(
    within(dialog).getByText(/claude --resume 3cd62baa-db5d-42fb-8522-2fde434541ae/),
  ).toBeInTheDocument()
  // nothing stashed yet — the dialog is informational until "Open in Chat"
  expect(sessionStorage.getItem(START_HERE_KEY)).toBeNull()
  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

test('the dialog copy button writes the command to the clipboard', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(window.navigator, 'clipboard', { value: { writeText }, configurable: true })
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_claims') return [CLAIM]
    if (method === 'kb.why') return WHY
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ClaimsView />)
  await userEvent.click(await screen.findByText(/openclaw-manifest-version-lockstep/))
  await userEvent.click(await screen.findByRole('button', { name: /start here/i }))
  const dialog = await screen.findByRole('dialog')
  await userEvent.click(within(dialog).getAllByRole('button', { name: /^copy$/i })[0])
  expect(writeText).toHaveBeenCalledWith('claude "/vouch-start openclaw-manifest-version-lockstep"')
  // feedback: the button flips to "Copied"
  expect(await within(dialog).findByRole('button', { name: /copied/i })).toBeInTheDocument()
})

test('Open in Chat stashes the claim + session handoff for the chat', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method) => {
    if (method === 'kb.list_claims') return [CLAIM]
    if (method === 'kb.why') return WHY
    throw new Error(`unexpected ${method}`)
  })
  renderWithProviders(<ClaimsView />)
  await userEvent.click(await screen.findByText(/openclaw-manifest-version-lockstep/))
  await screen.findByText(/resuming session 3cd62baa/i)
  await userEvent.click(screen.getByRole('button', { name: /start here/i }))
  await userEvent.click(await screen.findByRole('button', { name: /open in chat/i }))
  const stash = JSON.parse(sessionStorage.getItem(START_HERE_KEY) ?? 'null')
  expect(stash).toEqual({
    claimId: CLAIM.id,
    text: CLAIM.text,
    sessionId: '3cd62baa-db5d-42fb-8522-2fde434541ae',
  })
})

test('shows the empty state when there are no approved claims', async () => {
  vi.mocked(rpc).mockResolvedValue([])
  renderWithProviders(<ClaimsView />)
  expect(await screen.findByText(/no approved claims yet/i)).toBeInTheDocument()
})
