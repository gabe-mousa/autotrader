import { Outlet, useLocation } from 'react-router-dom'
import Sidebar from './Sidebar'
import StatusBar from './StatusBar'
import Breadcrumbs from './Breadcrumbs'
import ErrorBoundary from './ErrorBoundary'

export default function Layout() {
  const location = useLocation()
  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#151412] text-gray-300">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <StatusBar />
        <Breadcrumbs />
        <main className="flex-1 overflow-auto p-6">
          {/* key={pathname} so navigating to a different page resets a
              tripped boundary instead of showing the old page's error */}
          <ErrorBoundary key={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </div>
  )
}
