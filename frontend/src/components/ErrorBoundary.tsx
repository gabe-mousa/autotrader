import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

// Without this, an uncaught render error anywhere (e.g. RunDetail.tsx's
// starting_equity bug, found live 2026-07-24) unmounts the ENTIRE app —
// sidebar and all — leaving a blank page with no way to navigate away short
// of manually editing the URL. Scoped to just the routed page content (see
// Layout.tsx) so Sidebar/StatusBar survive a single page's crash.
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Uncaught error in page content:', error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="rounded border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-300">
          <p className="mb-2 font-medium">This page hit an unexpected error and couldn't render.</p>
          <p className="mb-3 font-mono text-xs text-red-400">{this.state.error.message}</p>
          <button
            onClick={() => this.setState({ error: null })}
            className="rounded border border-white/10 px-3 py-1.5 text-xs text-gray-300 hover:bg-white/5"
          >
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
