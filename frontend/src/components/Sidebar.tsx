import { NavLink } from 'react-router-dom'

const items: Array<{ to: string; label: string; end?: boolean; accent?: 'options' }> = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/strategies', label: 'Strategies' },
  { to: '/running', label: 'Running' },
  { to: '/backtests', label: 'Backtests' },
  { to: '/optimize', label: 'Optimize' },
  { to: '/charts', label: 'Charts' },
  // Options surfaces are amber throughout; see components/AssetTypeBadge.
  { to: '/chains', label: 'Chains', accent: 'options' as const },
  { to: '/orders', label: 'Orders' },
  { to: '/data', label: 'Data' },
  { to: '/live-probe', label: 'Live probe' },
  { to: '/docs', label: 'Docs' },
  { to: '/settings', label: 'Settings' },
]

export default function Sidebar() {
  return (
    <nav className="flex w-44 shrink-0 flex-col border-r border-white/10 bg-[#1d1a17] py-3">
      <div className="mb-3 px-3 text-sm font-semibold tracking-wide text-gray-200">
        autotrader
      </div>
      <ul className="flex flex-col gap-0.5">
        {items.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `block px-3 py-1.5 text-sm transition-colors ${
                  isActive
                    ? 'bg-white/10 text-gray-100'
                    : item.accent === 'options'
                      ? 'text-amber-400/70 hover:bg-white/5 hover:text-amber-300'
                      : 'text-gray-400 hover:bg-white/5 hover:text-gray-200'
                }`
              }
            >
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
