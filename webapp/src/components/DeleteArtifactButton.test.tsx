import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'

vi.mock('../lib/rpc', async () => {
  const actual = await vi.importActual<typeof import('../lib/rpc')>('../lib/rpc')
  return { ...actual, rpc: vi.fn(), fetchHealth: vi.fn(), fetchCapabilities: vi.fn() }
})
import { rpc, VouchRpcError } from '../lib/rpc'
import { makeProject, renderWithProviders } from '../test/utils'
import { DeleteArtifactButton } from './DeleteArtifactButton'

const CAPS = { name: 'vouch', level: 3, methods: ['kb.propose_delete'], review_gated: true }
const NO_DELETE = { name: 'vouch', level: 3, methods: ['kb.read_claim'], review_gated: true }

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
})

test('hidden when kb.propose_delete is not advertised', () => {
  renderWithProviders(<DeleteArtifactButton project={makeProject(NO_DELETE)} kind="claim" id="c1" />)
  expect(screen.queryByRole('button', { name: /delete/i })).toBeNull()
})

test('files a delete proposal and toasts success', async () => {
  vi.mocked(rpc).mockResolvedValue({ proposal_id: '20260709-1', status: 'pending', kind: 'delete' })
  const onDone = vi.fn()
  renderWithProviders(
    <DeleteArtifactButton project={makeProject(CAPS)} kind="claim" id="c1" onDone={onDone} />,
  )
  await userEvent.click(screen.getByRole('button', { name: /delete/i }))
  await userEvent.click(screen.getByRole('button', { name: /confirm delete/i }))
  await waitFor(() =>
    expect(rpc).toHaveBeenCalledWith(expect.anything(), 'kb.propose_delete', {
      target_kind: 'claim',
      target_id: 'c1',
    }),
  )
  expect(await screen.findByText(/delete proposal filed/i)).toBeInTheDocument()
  expect(onDone).toHaveBeenCalled()
})

test('surfaces a referenced-block error verbatim', async () => {
  vi.mocked(rpc).mockRejectedValue(
    new VouchRpcError(
      'invalid_request',
      "cannot delete claim c1: referenced by page 'foo' (supersede it instead?)",
    ),
  )
  renderWithProviders(<DeleteArtifactButton project={makeProject(CAPS)} kind="claim" id="c1" />)
  await userEvent.click(screen.getByRole('button', { name: /delete/i }))
  await userEvent.click(screen.getByRole('button', { name: /confirm delete/i }))
  expect(await screen.findByText(/referenced by page 'foo'/i)).toBeInTheDocument()
})
