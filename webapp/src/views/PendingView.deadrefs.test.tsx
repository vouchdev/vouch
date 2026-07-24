import { screen, waitFor } from '@testing-library/react'
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
  methods: ['kb.list_pending', 'kb.approve', 'kb.reject', 'kb.wipe_dead_refs'],
  review_gated: true,
}

const PAGE_PROPOSAL = {
  id: '20260724-160246-deadref1',
  kind: 'page',
  proposed_by: 'wiki-compiler',
  session_id: null,
  payload: {
    title: 'Compiled topic page',
    body: 'prose. [claim: gone-claim] more prose.',
    claims: ['gone-claim'],
  },
  status: 'pending',
  proposed_at: '2026-07-24T16:02:46+00:00',
}

const DEAD_REFS_ERROR = new VouchRpcError(
  'dead_claim_refs',
  'page proposal 20260724-160246-deadref1 references missing claim(s): gone-claim',
)

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  vi.mocked(fetchHealth).mockResolvedValue(true)
  vi.mocked(fetchCapabilities).mockResolvedValue(CAPS)
  seedConnection()
})

test('dead_claim_refs on approve offers strip-and-retry, retry sends the flag', async () => {
  vi.mocked(rpc).mockImplementation(async (_c, method, params) => {
    if (method === 'kb.list_pending') return [PAGE_PROPOSAL]
    if (method === 'kb.approve') {
      if ((params as { drop_missing_claims?: boolean }).drop_missing_claims)
        return { kind: 'page', id: 'compiled-topic-page' }
      throw DEAD_REFS_ERROR
    }
    return []
  })
  renderWithProviders(<PendingView />)

  await userEvent.click(await screen.findByText(PAGE_PROPOSAL.id))
  await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))

  // The refusal renders as a decision card, not a bare error.
  expect(await screen.findByText(/cites claim\(s\) that no longer exist/i)).toBeInTheDocument()
  expect(screen.getByText(/references missing claim\(s\): gone-claim/)).toBeInTheDocument()

  await userEvent.click(screen.getByRole('button', { name: /strip dead refs & approve/i }))
  await waitFor(() => {
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.approve', {
      proposal_id: PAGE_PROPOSAL.id,
      drop_missing_claims: true,
    })
  })
})

test('wipe dead refs button previews via dry-run, then applies', async () => {
  const report = {
    pages: { 'stale-page': ['gone-claim'] },
    proposals: { [PAGE_PROPOSAL.id]: ['gone-claim'] },
    dropped: 2,
  }
  vi.mocked(rpc).mockImplementation(async (_c, method, params) => {
    if (method === 'kb.list_pending') return [PAGE_PROPOSAL]
    if (method === 'kb.wipe_dead_refs')
      return { ...report, dry_run: (params as { dry_run?: boolean }).dry_run === true }
    return []
  })
  renderWithProviders(<PendingView />)

  await userEvent.click(await screen.findByRole('button', { name: /wipe dead claim refs/i }))
  await waitFor(() => {
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.wipe_dead_refs', { dry_run: true })
  })

  // Dry-run preview: counts + explicit confirm before anything is written.
  expect(await screen.findByText(/2 dead claim ref\(s\) in 1 page\(s\)/i)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: /wipe 2 dead ref\(s\)/i }))
  await waitFor(() => {
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.wipe_dead_refs', { dry_run: false })
  })
  expect(await screen.findByText(/stripped 2 dead reference\(s\)/i)).toBeInTheDocument()
})
