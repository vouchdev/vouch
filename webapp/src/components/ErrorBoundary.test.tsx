import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ErrorBoundary } from './ErrorBoundary'

function Bomb(): never {
  throw new Error('kaboom: render blew up')
}

beforeEach(() => {
  // React (and jsdom) log the caught error to the console; silence it so
  // the expected-failure test doesn't spam stderr.
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

test('renders a fallback panel with the error message when a child throws', () => {
  render(
    <ErrorBoundary>
      <Bomb />
    </ErrorBoundary>,
  )
  expect(screen.getByText(/kaboom: render blew up/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /reload/i })).toBeInTheDocument()
})
