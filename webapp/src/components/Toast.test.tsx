import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ToastProvider, useErrorToast } from './Toast'

function Probe({ isError, error }: { isError: boolean; error: unknown }) {
  useErrorToast(isError, error)
  return null
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

test('does not re-toast on every poll tick when isError stays true but the error identity changes', () => {
  const { rerender } = render(
    <ToastProvider>
      <Probe isError={true} error={new Error('boom')} />
    </ToastProvider>,
  )
  expect(screen.getAllByRole('status')).toHaveLength(1)
  expect(screen.getAllByText('boom')).toHaveLength(1)

  // Simulate a refetchInterval poll tick: isError stays true, but the failed
  // refetch produced a brand-new Error instance (new identity, same message).
  rerender(
    <ToastProvider>
      <Probe isError={true} error={new Error('boom')} />
    </ToastProvider>,
  )
  rerender(
    <ToastProvider>
      <Probe isError={true} error={new Error('boom')} />
    </ToastProvider>,
  )

  expect(screen.getAllByRole('status')).toHaveLength(1)
  expect(screen.getAllByText('boom')).toHaveLength(1)
})

test('toasts again after isError transitions true -> false -> true (two toasts fired total)', () => {
  const { rerender } = render(
    <ToastProvider>
      <Probe isError={true} error={new Error('first failure')} />
    </ToastProvider>,
  )
  expect(screen.getAllByText('first failure')).toHaveLength(1)

  // Let the first toast auto-dismiss (5s timer) so the next toast can be
  // attributed unambiguously to the next false->true transition.
  act(() => {
    vi.advanceTimersByTime(5000)
  })
  expect(screen.queryByText('first failure')).toBeNull()
  expect(screen.queryAllByRole('status')).toHaveLength(0)

  // isError flips back to false: this must reset the "already toasted" ref
  // so a later, distinct failure is not swallowed.
  rerender(
    <ToastProvider>
      <Probe isError={false} error={null} />
    </ToastProvider>,
  )
  expect(screen.queryAllByRole('status')).toHaveLength(0)

  // A later, distinct failure: isError flips false -> true again.
  rerender(
    <ToastProvider>
      <Probe isError={true} error={new Error('second failure')} />
    </ToastProvider>,
  )

  expect(screen.getAllByRole('status')).toHaveLength(1)
  expect(screen.getAllByText('second failure')).toHaveLength(1)
})

test('ToastProvider unmount clears pending dismiss timers without throwing', () => {
  const { unmount } = render(
    <ToastProvider>
      <Probe isError={true} error={new Error('unmount-safe')} />
    </ToastProvider>,
  )
  expect(screen.getAllByText('unmount-safe')).toHaveLength(1)

  unmount()

  // If the 5s dismiss timeout were not cleared, this would try to setState
  // on an unmounted component when the timer fires.
  expect(() => {
    act(() => {
      vi.advanceTimersByTime(5000)
    })
  }).not.toThrow()
})
