import { render } from '@testing-library/react'
import type { RenderResult } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '../components/Toast'
import { ALL_SCOPE, ConnectionProvider, projectLabel, STORAGE_KEY, STORAGE_KEY_V2 } from '../connection/ConnectionContext'
import type { ProjectState } from '../connection/ConnectionContext'
import type { Capabilities, VouchConnectionInfo } from '../lib/types'

export const TEST_ENDPOINT = 'http://127.0.0.1:8731'
export const TEST_ENDPOINT_B = 'http://127.0.0.1:8732'

/** Seed the legacy v1 single-endpoint storage (migrates to one project). */
export function seedConnection(info: Partial<VouchConnectionInfo> = {}): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ endpoint: TEST_ENDPOINT, ...info }))
}

/** Seed the v2 multi-project storage directly. */
export function seedProjects(projects: VouchConnectionInfo[], scope: string = ALL_SCOPE): void {
  localStorage.setItem(STORAGE_KEY_V2, JSON.stringify({ projects, scope }))
}

/** A ready-made ProjectState for components that take the project as a prop. */
export function makeProject(
  caps: Capabilities | null = null,
  info: Partial<VouchConnectionInfo> = {},
): ProjectState {
  const conn = { endpoint: TEST_ENDPOINT, ...info }
  return { conn, label: projectLabel(conn), caps, health: 'ok' }
}

export function renderWithProviders(ui: ReactElement, { route = '/' } = {}): RenderResult {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <ToastProvider>
        <ConnectionProvider>{ui}</ConnectionProvider>
      </ToastProvider>
    </MemoryRouter>,
  )
}
