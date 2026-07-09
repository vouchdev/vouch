import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { rpc, VouchRpcError } from '../lib/rpc'
import { makeProject, renderWithProviders } from '../test/utils'
import { ClaimLifecycleActions } from './ClaimLifecycleActions'

const CAPS = { name: 'vouch', level: 3, methods: ['kb.archive', 'kb.supersede'], review_gated: true }
const NONE = { name: 'vouch', level: 3, methods: ['kb.read_claim'], review_gated: true }
const ARCHIVE_ONLY = { name: 'vouch', level: 3, methods: ['kb.archive'], review_gated: true }

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
})

test('hidden when neither archive nor supersede is advertised', () => {
  renderWithProviders(<ClaimLifecycleActions project={makeProject(NONE)} claimId="c1" />)
  expect(screen.queryByRole('button', { name: /archive/i })).toBeNull()
  expect(screen.queryByRole('button', { name: /supersede/i })).toBeNull()
})

test('shows only Archive when only kb.archive is advertised', () => {
  renderWithProviders(<ClaimLifecycleActions project={makeProject(ARCHIVE_ONLY)} claimId="c1" />)
  expect(screen.getByRole('button', { name: /archive/i })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /supersede/i })).toBeNull()
})

test('archive calls kb.archive and toasts success', async () => {
  vi.mocked(rpc).mockResolvedValue({ id: 'c1', status: 'archived' })
  const onDone = vi.fn()
  renderWithProviders(<ClaimLifecycleActions project={makeProject(CAPS)} claimId="c1" onDone={onDone} />)
  await userEvent.click(screen.getByRole('button', { name: /^archive$/i }))
  await userEvent.click(screen.getByRole('button', { name: /confirm archive/i }))
  await waitFor(() =>
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.archive', { claim_id: 'c1' }),
  )
  expect(await screen.findByText(/archived/i)).toBeInTheDocument()
  expect(onDone).toHaveBeenCalled()
})

test('supersede calls kb.supersede with the entered replacement id', async () => {
  vi.mocked(rpc).mockResolvedValue({ old: 'c1', new: 'c2', status: 'superseded' })
  renderWithProviders(<ClaimLifecycleActions project={makeProject(CAPS)} claimId="c1" />)
  await userEvent.click(screen.getByRole('button', { name: /^supersede$/i }))
  const confirmBtn = screen.getByRole('button', { name: /confirm supersede/i })
  expect(confirmBtn).toBeDisabled() // empty input
  await userEvent.type(screen.getByPlaceholderText(/existing claim id/i), 'c2')
  await userEvent.click(confirmBtn)
  await waitFor(() =>
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.supersede', {
      old_claim_id: 'c1',
      new_claim_id: 'c2',
    }),
  )
  expect(await screen.findByText(/superseded/i)).toBeInTheDocument()
})

test('supersede confirm stays disabled when the new id equals the old', async () => {
  renderWithProviders(<ClaimLifecycleActions project={makeProject(CAPS)} claimId="c1" />)
  await userEvent.click(screen.getByRole('button', { name: /^supersede$/i }))
  await userEvent.type(screen.getByPlaceholderText(/existing claim id/i), 'c1')
  expect(screen.getByRole('button', { name: /confirm supersede/i })).toBeDisabled()
})

test('surfaces a backend error verbatim', async () => {
  vi.mocked(rpc).mockRejectedValue(new VouchRpcError('invalid_request', 'a claim cannot supersede itself'))
  renderWithProviders(<ClaimLifecycleActions project={makeProject(CAPS)} claimId="c1" />)
  await userEvent.click(screen.getByRole('button', { name: /^supersede$/i }))
  await userEvent.type(screen.getByPlaceholderText(/existing claim id/i), 'c2')
  await userEvent.click(screen.getByRole('button', { name: /confirm supersede/i }))
  expect(await screen.findByText(/cannot supersede itself/i)).toBeInTheDocument()
})
