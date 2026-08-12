import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'

type Theme = 'dark' | 'light'

const STORAGE_KEY = 'autotrader-theme'

const ThemeContext = createContext<{ theme: Theme; toggleTheme: () => void } | null>(null)

function getInitialTheme(): Theme {
  if (typeof window === 'undefined') return 'dark'
  const stored = window.localStorage.getItem(STORAGE_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  // Default to dark — this app's native theme — regardless of OS preference.
  return 'dark'
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(getInitialTheme)

  useEffect(() => {
    const root = document.documentElement
    root.classList.remove('dark', 'light')
    root.classList.add(theme)
    root.style.colorScheme = theme
    window.localStorage.setItem(STORAGE_KEY, theme)
  }, [theme])

  function toggleTheme() {
    setTheme((t) => (t === 'dark' ? 'light' : 'dark'))
  }

  return <ThemeContext.Provider value={{ theme, toggleTheme }}>{children}</ThemeContext.Provider>
}

export function useTheme() {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider')
  return ctx
}

// lightweight-charts paints to a canvas, so it can't pick up the CSS-level
// light-mode remap in index.css — callers apply this directly via
// createChart's `layout`/`grid` options and re-apply on chart.applyOptions
// when the theme changes.
export function chartTheme(theme: Theme) {
  return theme === 'light'
    ? {
        layout: { background: { color: '#f9f7f4' }, textColor: '#6b6f75' },
        grid: { vertLines: { color: '#e9e5df' }, horzLines: { color: '#e9e5df' } },
      }
    : {
        layout: { background: { color: '#151412' }, textColor: '#9ca3af' },
        grid: { vertLines: { color: '#2a2723' }, horzLines: { color: '#2a2723' } },
      }
}
