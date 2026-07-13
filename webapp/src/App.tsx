import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ErrorBoundary } from './components/ErrorBoundary'
import { Shell } from './components/Shell'
import { ToastProvider } from './components/Toast'
import { ConnectionProvider } from './connection/ConnectionContext'
import { BrowseView } from './views/BrowseView'
import { ChatView } from './views/ChatView'
import { ClaimsView } from './views/ClaimsView'
import { DashboardView } from './views/DashboardView'
import { PendingView } from './views/PendingView'
import { ReviewView } from './views/ReviewView'
import { StatsView } from './views/StatsView'

export default function App() {
  return (
    <ToastProvider>
      <ConnectionProvider>
        <ErrorBoundary>
          <BrowserRouter>
            <Routes>
              <Route element={<Shell />}>
                <Route index element={<Navigate to="/dashboard" replace />} />
                <Route path="/chat" element={<ChatView />} />
                <Route path="/review" element={<ReviewView />} />
                <Route path="/pending" element={<PendingView />} />
                <Route path="/claims" element={<ClaimsView />} />
                <Route path="/browse/:kind?/:id?" element={<BrowseView />} />
                <Route path="/dashboard" element={<DashboardView />} />
                <Route path="/stats" element={<StatsView />} />
              </Route>
            </Routes>
          </BrowserRouter>
        </ErrorBoundary>
      </ConnectionProvider>
    </ToastProvider>
  )
}
