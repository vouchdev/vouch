import { describe, expect, it, vi } from 'vitest'

vi.mock('./rpc', () => ({ rpc: vi.fn() }))
import { rpc } from './rpc'
import { fetchTranscript } from './transcript'
import type { VouchConnectionInfo } from './types'

const conn: VouchConnectionInfo = { endpoint: 'http://127.0.0.1:8731' }

describe('fetchTranscript', () => {
  it('calls kb.session_transcript with session id + agent', async () => {
    vi.mocked(rpc).mockResolvedValue({ available: false, reason: 'x', observations: [] })
    await fetchTranscript(conn, 'sid-1', 'claude')
    expect(rpc).toHaveBeenCalledWith(conn, 'kb.session_transcript', { session_id: 'sid-1', agent: 'claude' })
  })

  it('omits agent when not given', async () => {
    vi.mocked(rpc).mockResolvedValue({ available: false, reason: 'x', observations: [] })
    await fetchTranscript(conn, 'sid-2')
    expect(rpc).toHaveBeenCalledWith(conn, 'kb.session_transcript', { session_id: 'sid-2' })
  })
})
