import { render, screen } from '@testing-library/react'
import { beforeEach, expect, test } from 'vitest'
import App from './App'

beforeEach(() => localStorage.clear())

test('boots to the connect dialog when no endpoint is stored', () => {
  render(<App />)
  expect(screen.getByText(/connect to your knowledge base/i)).toBeInTheDocument()
})
