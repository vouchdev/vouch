// App.tsx — root component. Composes VouchProvider + useVouchEvents + the shell
// (Rail / Topbar / view section / StatusBar / Drawer).
// Faithful port of shell()/wireEvents()/routing in src/renderer/app.js.
import { useState } from 'react'
import { VouchProvider, useVouch } from './lib/VouchContext'
import { useVouchEvents } from './lib/useVouchEvents'
import Rail from './components/Rail'
import Topbar from './components/Topbar'
import StatusBar from './components/StatusBar'
import EmptyState from './components/EmptyState'
import Drawer from './components/Drawer'
import { VIEWS } from './components/Rail'
import { viewBlurb } from './views/blurbs'
import { registry } from './views/registry'
import { useOnOpen } from './lib/useOnOpen'

// ---------------------------------------------------------------------------
// Helpers (mirrors app.js:390)
// ---------------------------------------------------------------------------
function basename(p: string): string {
  return (p || '').replace(/\/+$/, '').split('/').pop() || p
}

// ---------------------------------------------------------------------------
// Inner shell — must sit inside VouchProvider so hooks work
// ---------------------------------------------------------------------------
function Shell() {
  const { state, actions } = useVouch()

  // Drawer state — lifted into Shell so StatusBar diagnostics and future
  // view-level onOpen can both open the drawer with arbitrary content.
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [drawerContent, setDrawerContent] = useState<React.ReactNode>(null)

  function openDrawer(content: React.ReactNode) {
    setDrawerContent(content)
    setDrawerOpen(true)
  }
  function closeDrawer() {
    setDrawerOpen(false)
  }

  // diagnostics handler for StatusBar — mirrors showProcInfo() in app.js:377-381
  function handleDiagnostics(info: Record<string, unknown>) {
    openDrawer(
      <div>
        <h2>diagnostics</h2>
        <pre className="mono small">{JSON.stringify(info, null, 2)}</pre>
        <button
          className="btn ghost sm"
          onClick={() => window.vouch.openLogs()}
        >
          open logs folder
        </button>
      </div>,
    )
  }

  // KB open flow — mirrors doOpen() in app.js:361-368
  async function handleOpen(root: string) {
    try {
      await actions.openKb(root)
    } catch (e: unknown) {
      const err = e as { code?: string; message?: string }
      if (err.code === 'no_kb_here') {
        if (window.confirm(`No .vouch/ in ${basename(root)} — initialize one here?`)) {
          try {
            await actions.initKb(root)
          } catch {
            // noop
          }
        }
      }
      // other errors are surfaced via kbError in state
    }
  }

  // Initialize new KB flow — mirrors initKbFlow() in app.js:353-360
  async function handleInitKb() {
    try {
      const dir = await import('./lib/client').then((c) => c.pickKb())
      if (!dir) return
      await actions.initKb(dir)
    } catch {
      // noop
    }
  }

  // Switch KB — mirrors openKbPicker() in app.js:347-352
  async function handleSwitchKb() {
    try {
      await actions.pickKb()
    } catch {
      // noop
    }
  }

  // Push-event subscriptions — mirrors wireEvents() in app.js:403-420
  useVouchEvents({
    pickKb: () => void handleSwitchKb(),
    navigate: (v) => actions.navigate(v),
  })

  // onOpen — opens a KB object in the drawer (app.js:288-314)
  const onOpen = useOnOpen({ openDrawer })

  // Determine the current view label for the view-head
  const currentView = VIEWS.find((v) => v.id === state.view)

  // Look up the component for the current view (fall back to dashboard)
  const ViewComponent = registry[state.view] ?? registry['dashboard']

  return (
    <>
      <Rail onSwitchKb={() => void handleSwitchKb()} />

      <main className="main">
        <Topbar />

        <section className="view">
          {state.root ? (
            <>
              <div className="view-head">
                <h1>{currentView?.label ?? state.view}</h1>
                <p className="muted">{viewBlurb(state.view)}</p>
              </div>
              <div className="view-body">
                <ViewComponent onOpen={onOpen} />
              </div>
            </>
          ) : (
            <EmptyState
              onOpen={(root) => void handleOpen(root)}
              onInitKb={() => void handleInitKb()}
            />
          )}
        </section>

        <StatusBar onShowDiagnostics={handleDiagnostics} />
      </main>

      <Drawer open={drawerOpen} onClose={closeDrawer}>
        {drawerContent}
      </Drawer>
    </>
  )
}

// ---------------------------------------------------------------------------
// Root — wraps Shell in VouchProvider
// ---------------------------------------------------------------------------
export default function App() {
  return (
    <VouchProvider>
      <Shell />
    </VouchProvider>
  )
}
