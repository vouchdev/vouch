// method-card.test.tsx — RTL tests for src/renderer/src/components/MethodCard.tsx
//
// Covers: successful run shows result + calls actions.refreshPending() when method.mutates;
// api.call rejection renders ErrorBox; collect() rejection renders ErrorBox without spinner;
// spinner visible while running and hidden in finally; re-run clears previous result.

import React from 'react'
import { render, screen, fireEvent, waitFor, act, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import type { Method } from '../src/shared/methods.types'

// ---------------------------------------------------------------------------
// Mocks — must come before component imports that transitively use them
// ---------------------------------------------------------------------------

// Mock window.vouch so client.ts (which reads it at module init) doesn't crash in jsdom
Object.defineProperty(window, 'vouch', {
  value: {
    call: vi.fn(),
    on: vi.fn(() => () => {}),
    pickFile: vi.fn(),
    pickSave: vi.fn(),
    pickKb: vi.fn(),
    openKb: vi.fn(),
    initKb: vi.fn(),
    recentRoots: vi.fn(),
    capabilities: vi.fn(),
    status: vi.fn(),
    getPrefs: vi.fn(),
    setPref: vi.fn(),
    procInfo: vi.fn(),
    ds: {
      preconditions: vi.fn(),
      ensure: vi.fn(),
      run: vi.fn(),
      job: vi.fn(),
      choose: vi.fn(),
    },
  },
  writable: true,
})

// client.ts is NOT mocked as a module — we let it use window.vouch.
// Instead we spy on the named export after import.
import * as api from '../src/renderer/src/lib/client'

// Mock VouchContext so MethodCard doesn't need a real Provider
vi.mock('../src/renderer/src/lib/VouchContext', () => {
  const refreshPending = vi.fn()
  const navigate = vi.fn()
  return {
    useVouch: () => ({
      state: {
        root: '/kb',
        caps: null,
        capMethods: null,
        gitRepo: false,
        view: 'browse',
        pending: null,
        jsonl: true,
        kbError: null,
        statusError: null,
      },
      dispatch: vi.fn(),
      actions: { refreshPending, navigate, openKb: vi.fn(), initKb: vi.fn(), pickKb: vi.fn() },
    }),
    isAvailable: () => true,
  }
})

import MethodCard from '../src/renderer/src/components/MethodCard'
import { useVouch } from '../src/renderer/src/lib/VouchContext'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const noOpOnOpen = vi.fn()

const simpleMethod: Method = {
  name: 'kb.demo',
  view: 'browse',
  params: [
    { name: 'query', type: 'string', required: true, control: 'text' },
  ],
}

const mutatingMethod: Method = {
  name: 'kb.write',
  view: 'browse',
  mutates: true,
  params: [
    { name: 'query', type: 'string', required: true, control: 'text' },
  ],
}

const noParamsMethod: Method = {
  name: 'kb.ping',
  view: 'browse',
  params: [],
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------
// Helpers to fill form
// ---------------------------------------------------------------------------
function fillQuery(value: string) {
  const inp = screen.getByLabelText(/^query/i)
  fireEvent.change(inp, { target: { value } })
}

// ---------------------------------------------------------------------------
// Successful run shows result
// ---------------------------------------------------------------------------
describe('MethodCard — successful run', () => {
  beforeEach(() => {
    vi.spyOn(api, 'call').mockResolvedValue({ answer: 42 })
  })

  it('shows result after successful run', async () => {
    render(<MethodCard method={simpleMethod} onOpen={noOpOnOpen} />)
    fillQuery('hello')
    fireEvent.click(screen.getByRole('button', { name: /run/i }))
    // { answer: 42 } is rendered via ResultView synthesis path → synth-prose contains "42"
    await waitFor(() => expect(document.querySelector('.synth-prose')).not.toBeNull())
    expect(document.querySelector('.synth-prose')!.textContent).toBe('42')
  })

  it('calls actions.refreshPending() when method.mutates is true', async () => {
    render(<MethodCard method={mutatingMethod} onOpen={noOpOnOpen} />)
    fillQuery('hello')
    fireEvent.click(screen.getByRole('button', { name: /run/i }))
    await waitFor(() => expect(document.querySelector('.synth-prose')).not.toBeNull())
    const { actions } = useVouch()
    expect(actions.refreshPending).toHaveBeenCalledOnce()
  })

  it('does NOT call actions.refreshPending() when method.mutates is false/absent', async () => {
    render(<MethodCard method={simpleMethod} onOpen={noOpOnOpen} />)
    fillQuery('hello')
    fireEvent.click(screen.getByRole('button', { name: /run/i }))
    await waitFor(() => expect(document.querySelector('.synth-prose')).not.toBeNull())
    const { actions } = useVouch()
    expect(actions.refreshPending).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// api.call rejection renders ErrorBox
// ---------------------------------------------------------------------------
describe('MethodCard — api.call rejection', () => {
  it('renders ErrorBox when api.call rejects', async () => {
    const err = Object.assign(new Error('not found'), { code: 'NOT_FOUND' })
    vi.spyOn(api, 'call').mockRejectedValue(err)

    render(<MethodCard method={simpleMethod} onOpen={noOpOnOpen} />)
    fillQuery('hello')
    fireEvent.click(screen.getByRole('button', { name: /run/i }))

    await waitFor(() =>
      expect(screen.getByText(/not found/i)).toBeTruthy()
    )
    // errbox class should be present
    expect(document.querySelector('.errbox')).not.toBeNull()
  })

  it('hides spinner after api.call rejects', async () => {
    const err = new Error('server error')
    vi.spyOn(api, 'call').mockRejectedValue(err)

    render(<MethodCard method={simpleMethod} onOpen={noOpOnOpen} />)
    fillQuery('hello')
    fireEvent.click(screen.getByRole('button', { name: /run/i }))

    await waitFor(() => expect(screen.queryByText(/calling/)).toBeNull())
  })

  it('does not show result pre element when api.call rejects', async () => {
    const err = new Error('fail')
    vi.spyOn(api, 'call').mockRejectedValue(err)

    render(<MethodCard method={simpleMethod} onOpen={noOpOnOpen} />)
    fillQuery('hello')
    fireEvent.click(screen.getByRole('button', { name: /run/i }))

    await waitFor(() => expect(document.querySelector('.errbox')).not.toBeNull())
    expect(document.querySelector('pre.mono')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// collect() rejection renders ErrorBox without spinner
// ---------------------------------------------------------------------------
describe('MethodCard — collect() rejection', () => {
  it('shows error and no spinner when required field missing', async () => {
    vi.spyOn(api, 'call').mockResolvedValue({})

    render(<MethodCard method={simpleMethod} onOpen={noOpOnOpen} />)
    // Do NOT fill required field 'query'
    fireEvent.click(screen.getByRole('button', { name: /run/i }))

    // ErrorBox appears (required error)
    await waitFor(() => expect(document.querySelector('.errbox')).not.toBeNull())

    // Spinner never appears (run bails before setRunning(true))
    expect(screen.queryByText(/calling/)).toBeNull()
    // api.call was never called
    expect(api.call).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// Spinner visible while running, hidden afterwards
// ---------------------------------------------------------------------------
describe('MethodCard — spinner lifecycle', () => {
  it('shows spinner while running and hides it after', async () => {
    let resolve!: (v: unknown) => void
    vi.spyOn(api, 'call').mockReturnValue(new Promise((res) => { resolve = res }))

    render(<MethodCard method={noParamsMethod} onOpen={noOpOnOpen} />)
    fireEvent.click(screen.getByRole('button', { name: /run/i }))

    // Spinner should be visible now
    expect(screen.getByText(/calling/)).toBeTruthy()

    // Resolve the call
    await act(async () => { resolve({ done: true }) })

    // Spinner should be gone
    expect(screen.queryByText(/calling/)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Re-run clears previous result
// ---------------------------------------------------------------------------
describe('MethodCard — re-run clears previous result', () => {
  it('clears previous result and shows new result on second run', async () => {
    vi.spyOn(api, 'call')
      .mockResolvedValueOnce({ first: true })
      .mockResolvedValueOnce({ second: true })

    render(<MethodCard method={noParamsMethod} onOpen={noOpOnOpen} />)

    // First run
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /run/i }))
    })
    // { first: true } falls to JsonTree; jt-key renders "first: "
    await waitFor(() => expect(screen.getByText(/first/)).toBeTruthy())

    // Second run — button is named 'Run' again (not 'running…') at this point
    const btn = screen.getByRole('button', { name: /run/i })
    await act(async () => {
      fireEvent.click(btn)
    })

    // After the second run completes, only the second result should be shown
    await waitFor(() => expect(screen.getByText(/second/)).toBeTruthy())
    expect(screen.queryByText(/first/)).toBeNull()
  })
})
