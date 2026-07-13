import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => cleanup())

// jsdom has no scrollIntoView; ChatView autoscroll calls it after each message.
if (typeof window !== 'undefined') {
  window.HTMLElement.prototype.scrollIntoView ??= () => {}
}
