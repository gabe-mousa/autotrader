import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  ApiError,
  getAccounts,
  getActivity,
  getHealth,
  getKillStatus,
  getRuns,
  type ActivityEntry,
  type RunLiveStatus,
  type RunSummary,
} from '../lib/api'
import { StateDot } from './Running'
import DocsHelpLink from '../components/DocsHelpLink'

export default function Dashboard() {
  const navigate = useNavigate()
  const [accountsHidden, setAccountsHidden] = useState(false)

  const accountsQuery = useQuery({
    queryKey: ['accounts'],
    queryFn: getAccounts,
    retry: false,
  })

  const runsQuery = useQuery({
    queryKey: ['runs'],
    queryFn: getRuns,
    refetchInterval: 5000,
    retry: false,
  })

  // Same queryKey as StatusBar.tsx — TanStack Query dedupes the polling.
  const healthQuery = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 5000,
    retry: false,
  })

  const killQuery = useQuery({
    queryKey: ['kill-status'],
    queryFn: getKillStatus,
    refetchInterval: 5000,
    retry: false,
  })

  const activityQuery = useQuery({
    queryKey: ['activity', 20],
    queryFn: () => getActivity(20),
    refetchInterval: 10000,
    retry: false,
  })

  const needsAuth =
    accountsQuery.isError &&
    accountsQuery.error instanceof ApiError &&
    (accountsQuery.error.status === 401 || accountsQuery.error.status === 424)

  const runs = runsQuery.data?.runs ?? []
  const activeRuns = runs.filter((r): r is RunSummary & { live: RunLiveStatus } => !!r.live)
  const runningCount = activeRuns.filter((r) => r.state === 'RUNNING').length
  const pausedCount = activeRuns.filter((r) => r.state === 'PAUSED').length
  const liveRuns = activeRuns.filter((r) => r.mode === 'live')
  const paperRuns = activeRuns.filter((r) => r.mode !== 'live')
  const totalLivePnlToday = liveRuns.reduce((sum, r) => sum + r.live.realized_pnl_today, 0)
  const totalPnlToday = paperRuns.reduce((sum, r) => sum + r.live.realized_pnl_today, 0)
  const totalOrdersToday = activeRuns.reduce((sum, r) => sum + r.live.orders_today, 0)

  const positions = activeRuns.flatMap((r) =>
    Object.entries(r.live.positions).map(([symbol, p]) => ({
      run: r,
      symbol,
      ...p,
    })),
  )

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-gray-100">Dashboard</h1>
        <DocsHelpLink to="/docs#first-time-setup" label="New here? First-time setup" />
      </div>

      <div className="mb-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          label="Live P&L today (all active runs)"
          value={
            liveRuns.length === 0 ? (
              <span className="text-gray-500">—</span>
            ) : (
              <span className={totalLivePnlToday >= 0 ? 'text-teal-400' : 'text-red-400'}>
                {totalLivePnlToday.toLocaleString('en-US', { style: 'currency', currency: 'USD' })}
              </span>
            )
          }
          sub={
            liveRuns.length === 0
              ? 'No live runs active.'
              : 'Real fills at your broker — real day P&L.'
          }
        />
        <Tile
          label="Paper P&L today (all active runs)"
          value={
            <span className={totalPnlToday >= 0 ? 'text-teal-400' : 'text-red-400'}>
              {totalPnlToday.toLocaleString('en-US', { style: 'currency', currency: 'USD' })}
            </span>
          }
          sub="Simulated fills — not real brokerage day P&L."
        />
        <Tile
          label="Strategies"
          value={
            <span>
              {runningCount} running{' '}
              <span className="text-gray-500">/ {pausedCount} paused</span>
            </span>
          }
          sub={`${activeRuns.length} active run${activeRuns.length === 1 ? '' : 's'}`}
        />
        <Tile
          label="Positions / orders today"
          value={<span>{positions.length} open</span>}
          sub={`${totalOrdersToday} order${totalOrdersToday === 1 ? '' : 's'} placed today`}
        />
      </div>

      <section className="mb-6 rounded border border-white/10 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-200">Accounts</h2>
          {accountsQuery.data && accountsQuery.data.accounts.length > 0 && (
            <button
              type="button"
              onClick={() => setAccountsHidden((v) => !v)}
              className="text-gray-500 hover:text-gray-200"
              title={accountsHidden ? 'Show account details' : 'Hide account details'}
              aria-label={accountsHidden ? 'Show account details' : 'Hide account details'}
            >
              {accountsHidden ? <EyeOffIcon /> : <EyeIcon />}
            </button>
          )}
        </div>

        {accountsQuery.isLoading && <p className="text-sm text-gray-500">Loading accounts…</p>}

        {accountsQuery.isError && (
          <p className="text-sm text-red-400">
            {needsAuth
              ? 'Connect Schwab in Settings first.'
              : accountsQuery.error instanceof Error
                ? accountsQuery.error.message
                : 'Failed to load accounts.'}
          </p>
        )}

        {accountsQuery.data && accountsQuery.data.accounts.length === 0 && (
          <p className="text-sm text-gray-500">No accounts found.</p>
        )}

        {accountsQuery.data && accountsQuery.data.accounts.length > 0 && (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Account</th>
                  <th className="px-3 py-2 font-medium">Type</th>
                  <th className="px-3 py-2 font-medium">Equity</th>
                  <th className="px-3 py-2 font-medium">Cash</th>
                  <th className="px-3 py-2 font-medium">Buying Power</th>
                </tr>
              </thead>
              <tbody>
                {accountsQuery.data.accounts.map((acct) => (
                  <tr key={acct.account_number_masked} className="border-t border-white/10">
                    <td className="px-3 py-2 text-gray-300">
                      {accountsHidden ? '••••••' : acct.account_number_masked}
                    </td>
                    <td className="px-3 py-2 text-gray-300">
                      {accountsHidden ? '••••' : acct.type}
                    </td>
                    <td className="px-3 py-2 text-gray-300">
                      {accountsHidden ? '••••••' : formatCurrency(acct.equity)}
                    </td>
                    <td className="px-3 py-2 text-gray-300">
                      {accountsHidden ? '••••••' : formatCurrency(acct.cash)}
                    </td>
                    <td className="px-3 py-2 text-gray-300">
                      {accountsHidden ? '••••••' : formatCurrency(acct.buying_power)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="mb-6 rounded border border-white/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">Strategy status</h2>

        {runsQuery.isLoading && <p className="text-sm text-gray-500">Loading runs…</p>}
        {runsQuery.isError && <p className="text-sm text-red-400">Could not load runs.</p>}

        {!runsQuery.isLoading && activeRuns.length === 0 && (
          <p className="text-sm text-gray-600">No active runs. Start one from the Running page.</p>
        )}

        {activeRuns.length > 0 && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {activeRuns.map((r) => (
              <button
                key={r.id}
                onClick={() => navigate(`/running/${r.id}`)}
                className={`rounded border p-3 text-left hover:bg-white/5 ${
                  r.state === 'RUNNING'
                    ? 'border-white/10'
                    : r.state === 'ERROR'
                      ? 'border-red-500/30 bg-red-500/5'
                      : 'border-amber-500/30 bg-amber-500/5'
                }`}
              >
                <div className="mb-1 flex items-center gap-1.5 text-sm text-gray-200">
                  <StateDot state={r.state} />
                  <span className="font-medium">{r.name}</span>
                  <span className="text-gray-500">({r.slug})</span>
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-400">
                  <span>
                    P&L today{' '}
                    <span className={r.live.realized_pnl_today >= 0 ? 'text-teal-400' : 'text-red-400'}>
                      {r.live.realized_pnl_today.toFixed(2)}
                    </span>
                  </span>
                  <span>{Object.keys(r.live.positions).length} positions</span>
                  <span>{r.live.orders_today} orders today</span>
                </div>
              </button>
            ))}
          </div>
        )}
      </section>

      <section className="mb-6 rounded border border-white/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">Open positions (all runs)</h2>

        {positions.length === 0 ? (
          <p className="text-sm text-gray-600">No open positions.</p>
        ) : (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Symbol</th>
                  <th className="px-3 py-2 font-medium">Qty</th>
                  <th className="px-3 py-2 font-medium">Avg price</th>
                  <th className="px-3 py-2 font-medium">Bars held</th>
                  <th className="px-3 py-2 font-medium">Strategy</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr
                    key={`${p.run.id}-${p.symbol}`}
                    onClick={() => navigate(`/running/${p.run.id}`)}
                    className="cursor-pointer border-t border-white/10 hover:bg-white/5"
                  >
                    <td className="px-3 py-2 text-gray-300">{p.symbol}</td>
                    <td className="px-3 py-2 text-gray-300">{p.qty}</td>
                    <td className="px-3 py-2 text-gray-300">{p.avg_price.toFixed(2)}</td>
                    <td className="px-3 py-2 text-gray-300">{p.bars_held}</td>
                    <td className="px-3 py-2 text-gray-300">
                      {p.run.name} <span className="text-gray-500">({p.run.slug})</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <section className="rounded border border-white/10 p-4">
          <h2 className="mb-3 text-sm font-semibold text-gray-200">Recent activity</h2>

          {activityQuery.isLoading && <p className="text-sm text-gray-500">Loading…</p>}
          {activityQuery.isError && (
            <p className="text-sm text-red-400">Could not load activity.</p>
          )}

          {activityQuery.data && activityQuery.data.activity.length === 0 && (
            <p className="text-sm text-gray-600">No activity yet.</p>
          )}

          {activityQuery.data && activityQuery.data.activity.length > 0 && (
            <ul className="max-h-96 space-y-1.5 overflow-y-auto text-sm">
              {activityQuery.data.activity.map((entry, i) => (
                <li key={i} className="flex items-start gap-2 border-t border-white/10 pt-1.5 first:border-t-0 first:pt-0">
                  <span className="w-16 shrink-0 whitespace-nowrap text-xs text-gray-500">
                    {new Date(entry.ts).toLocaleTimeString()}
                  </span>
                  <span className="w-16 shrink-0 text-xs text-gray-400">{entry.category}</span>
                  <span className="flex-1 truncate text-gray-300" title={JSON.stringify(entry.payload)}>
                    {summarizeActivity(entry)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="rounded border border-white/10 p-4">
          <h2 className="mb-3 text-sm font-semibold text-gray-200">System health</h2>

          <div className="space-y-2 text-sm">
            <div className="flex items-center gap-2">
              <Dot ok={!healthQuery.isError && healthQuery.data?.auth.connected === true} />
              <span className="text-gray-300">
                Schwab {healthQuery.data?.auth.connected ? 'connected' : 'disconnected'}
              </span>
              {healthQuery.data?.auth.access_token_expires_in != null && (
                <span className="text-xs text-gray-500">
                  (token expires in {formatDuration(healthQuery.data.auth.access_token_expires_in)})
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Dot ok={healthQuery.data?.streamer === 'connected'} />
              <span className="text-gray-300">
                Streamer {healthQuery.data?.streamer ?? 'unknown'}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <Dot ok={!(killQuery.data?.active ?? false)} />
              <span className="text-gray-300">
                Kill switch {killQuery.data?.active ? 'ACTIVE' : 'inactive'}
              </span>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}

function Tile({
  label,
  value,
  sub,
}: {
  label: string
  value: ReactNode
  sub?: string
}) {
  return (
    <div className="rounded border border-white/10 p-4">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="mt-1 text-lg font-semibold text-gray-100">{value}</div>
      {sub && <div className="mt-1 text-xs text-gray-500">{sub}</div>}
    </div>
  )
}

function Dot({ ok }: { ok: boolean }) {
  return (
    <span className={`inline-block h-2 w-2 rounded-full ${ok ? 'bg-emerald-500' : 'bg-red-500'}`} />
  )
}

function EyeIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4"
    >
      <path d="M1.5 12S5 5 12 5s10.5 7 10.5 7-3.5 7-10.5 7S1.5 12 1.5 12Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  )
}

function EyeOffIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-4 w-4"
    >
      <path d="M3 3l18 18" />
      <path d="M10.6 5.1A10.6 10.6 0 0 1 12 5c7 0 10.5 7 10.5 7a17.6 17.6 0 0 1-3.13 4.02M6.6 6.6C3.4 8.6 1.5 12 1.5 12s3.5 7 10.5 7c1.4 0 2.68-.28 3.83-.75" />
      <path d="M9.5 9.6a3 3 0 0 0 4.24 4.24" />
    </svg>
  )
}

function summarizeActivity(entry: ActivityEntry): string {
  const { category, payload } = entry
  if (category === 'kill_switch') {
    const event = payload.event ?? (payload.active ? 'activated' : 'deactivated')
    return `Kill switch: ${event}`
  }
  if (category === 'strategy') {
    const slug = payload.slug ?? payload.name
    return slug ? `Strategy ${payload.event ?? 'updated'}: ${slug}` : 'Strategy updated'
  }
  if (category === 'auth') {
    return `Auth: ${payload.event ?? 'event'}`
  }
  if (category === 'backfill') {
    return `Backfill ${payload.symbol ?? ''}: ${payload.daily_bars_fetched ?? '?'} daily / ${payload.minute_bars_fetched ?? '?'} minute bars`
  }
  if (category === 'backtest') {
    return `Backtest run ${payload.run_id ?? ''} (${payload.slug ?? ''})`
  }
  if (category === 'lifecycle') {
    return `App ${payload.event ?? 'event'}`
  }
  return JSON.stringify(payload)
}

function formatDuration(seconds: number): string {
  if (seconds <= 0) return 'expired'
  const m = Math.floor(seconds / 60)
  const h = Math.floor(m / 60)
  if (h > 0) return `${h}h ${m % 60}m`
  return `${m}m`
}

function formatCurrency(value: number): string {
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}
