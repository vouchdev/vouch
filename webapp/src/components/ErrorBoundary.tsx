import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'
import { ErrorCard } from './ErrorCard'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * Last-resort fallback for uncaught render errors anywhere below it. Without
 * this, a single bad render (e.g. a malformed capabilities response reaching
 * a view that doesn't guard for it) blanks the whole app to a white screen
 * with no way back short of a manual reload.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('vouch-ui: uncaught error in render tree', error, info)
  }

  render() {
    const { error } = this.state
    if (error) {
      return (
        <div className="flex h-screen items-center justify-center bg-paper px-6">
          <div className="w-full max-w-md space-y-4">
            <ErrorCard message={error.message || 'Something went wrong.'} />
            <button
              onClick={() => location.reload()}
              className="w-full rounded-xl bg-accent px-4 py-2.5 text-sm font-semibold text-paper transition hover:bg-accent-2"
            >
              Reload
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
