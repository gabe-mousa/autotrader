import { AssetTypeBadge } from '../components/AssetTypeBadge'
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  ApiError,
  flattenRun,
  getRuns,
  getStrategies,
  pauseRun,
  recoverRun,
  resumeRun,
  startRun,
  stopRun,
  type LiveSupportGap,
  type RunState,
  type RunSummary,
} from '../lib/api'
import { inputCls } from '../components/editor/fields'

export function stateDotColor(state: RunState | string): string {
  switch (state) {
    case 'RUNNING':
      return 'bg-emerald-500'
    case 'STARTING':
    case 'PAUSED':
      return 'bg-amber-500'
    case 'ERROR':
    case 'STOPPED':
    case 'SUSPENDED_AUTH':
      return 'bg-red-500'
    case 'DEGRADED_DATA':
      return 'bg-amber-500'
    default:
      return 'bg-gray-500'
  }
}

/** Audit C1 — a run whose document uses something the live runtime does not
 * honour exactly as backtested. Blocking gaps read as a warning; the documented
 * approximations are muted, because they apply to a large share of strategies and
 * a loud badge on most rows would just be ignored. The detail text lives in the
 * title attribute so the table stays scannable. */
export function LiveGapBadge({ gaps }: { gaps?: LiveSupportGap[] }) {
  if (!gaps || gaps.length === 0) return null
  const blocking = gaps.filter((g) => g.blocking)
  const shown = blocking.length > 0 ? blocking : gaps
  const tone = blocking.length > 0
    ? 'bg-amber-500/15 text-amber-300 ring-amber-500/30'
    : 'bg-white/5 text-gray-400 ring-white/10'
  return (
    <span
      title={shown.map((g) => `${g.field}: ${g.detail}`).join('\n\n')}
      className={`ml-1.5 rounded px-1.5 py-0.5 text-[10px] ring-1 ${tone}`}
    >
      {blocking.length > 0 ? `${blocking.length} live gap${blocking.length > 1 ? 's' : ''}` : 'live notes'}
    </span>
  )
}

export function StateDot({ state }: { state: RunState | string }) {
  return <span className={`inline-block h-2 w-2 rounded-full ${stateDotColor(state)}`} />
}

export function pnlColor(v: number | null | undefined): string {
  if (v == null) return 'text-gray-300'
  return v >= 0 ? 'text-teal-400' : 'text-red-400'
}

type RunFilter = 'all' | 'live' | 'paper' | 'stopped'

const RUN_FILTERS: { id: RunFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'live', label: 'Live' },
  { id: 'paper', label: 'Paper' },
  { id: 'stopped', label: 'Stopped' },
]

export default function Running() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [slug, setSlug] = useState('')
  const [startingCash, setStartingCash] = useState(100000)
  const [runFilter, setRunFilter] = useState<RunFilter>('all')

  const strategiesQuery = useQuery({
    queryKey: ['strategies'],
    queryFn: () => getStrategies(),
    retry: false,
  })

  const runsQuery = useQuery({
    queryKey: ['runs'],
    queryFn: getRuns,
    refetchInterval: 5000,
    retry: false,
  })

  const startMutation = useMutation({
    mutationFn: () => startRun(slug, startingCash),
    onSuccess: (r) => {
      queryClient.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/running/${r.run_id}`)
    },
  })

  const recoverMutation = useMutation({
    mutationFn: (runId: string) => recoverRun(runId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  const actionMutation = useMutation({
    mutationFn: ({
      runId,
      action,
    }: {
      runId: string
      action: 'pause' | 'resume' | 'flatten' | 'stop'
    }) => {
      if (action === 'pause') return pauseRun(runId)
      if (action === 'resume') return resumeRun(runId)
      if (action === 'flatten') return flattenRun(runId)
      return stopRun(runId)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  const strategies = strategiesQuery.data?.strategies ?? []
  const runs = runsQuery.data?.runs ?? []

  // Stopped runs (stopped_at set) always sort to the bottom, newest-stopped
  // first within that block — so scrolling down moves strictly backward in
  // time and the oldest, least-relevant runs end up furthest away.
  const displayedRuns = useMemo(() => {
    const active = runs.filter((r) => r.stopped_at == null)
    const stopped = [...runs.filter((r) => r.stopped_at != null)].sort(
      (a, b) => (b.stopped_at ?? 0) - (a.stopped_at ?? 0),
    )
    switch (runFilter) {
      case 'live':
        return active.filter((r) => r.mode === 'live')
      case 'paper':
        return active.filter((r) => r.mode !== 'live')
      case 'stopped':
        return stopped
      default:
        return [...active, ...stopped]
    }
  }, [runs, runFilter])

  function doStart() {
    if (slug.length === 0 || startMutation.isPending) return
    startMutation.mutate()
  }

  function doAction(runId: string, action: 'pause' | 'resume' | 'flatten' | 'stop') {
    actionMutation.mutate({ runId, action })
  }

  return (
    <div>
      <h1 className="mb-4 text-xl font-semibold text-gray-100">Running</h1>

      <section className="mb-6 rounded border border-white/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">Start a new paper run</h2>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Strategy
            <select
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              className={inputCls('w-56')}
            >
              <option value="">Select a strategy…</option>
              {strategies.map((s) => (
                <option key={s.slug} value={s.slug}>
                  {s.slug} — {s.name}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Starting cash
            <input
              type="number"
              value={startingCash}
              onChange={(e) => setStartingCash(Number(e.target.value))}
              className={inputCls('w-36')}
            />
          </label>

          <button
            onClick={doStart}
            disabled={slug.length === 0 || startMutation.isPending}
            className="rounded bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {startMutation.isPending ? 'Starting…' : 'Start'}
          </button>
        </div>

        {startMutation.isError && (
          <p className="mt-3 text-sm text-red-400">
            {startMutation.error instanceof ApiError
              ? startMutation.error.message
              : 'Failed to start run.'}
          </p>
        )}
      </section>

      <section className="rounded border border-white/10 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-200">Runs</h2>
          <div className="flex gap-1 rounded border border-white/10 p-1">
            {RUN_FILTERS.map((f) => (
              <button
                key={f.id}
                onClick={() => setRunFilter(f.id)}
                className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                  runFilter === f.id
                    ? 'bg-white/10 text-gray-100'
                    : 'text-gray-500 hover:text-gray-300'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>

        {runsQuery.isLoading && <p className="text-sm text-gray-500">Loading…</p>}
        {runsQuery.isError && <p className="text-sm text-red-400">Could not load runs.</p>}

        {runs.length === 0 && !runsQuery.isLoading && (
          <p className="text-sm text-gray-600">No runs yet.</p>
        )}

        {runs.length > 0 && displayedRuns.length === 0 && (
          <p className="text-sm text-gray-600">No {runFilter} runs.</p>
        )}

        {displayedRuns.length > 0 && (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">State</th>
                  <th className="px-3 py-2 font-medium">Strategy</th>
                  <th className="px-3 py-2 font-medium">Mode</th>
                  <th className="px-3 py-2 font-medium">Started</th>
                  <th className="px-3 py-2 font-medium">Positions</th>
                  <th className="px-3 py-2 font-medium">Realized P&L today</th>
                  <th className="px-3 py-2 font-medium">Total P&L %</th>
                  <th className="px-3 py-2 font-medium">Orders today</th>
                  <th className="px-3 py-2 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {displayedRuns.map((r: RunSummary) => (
                  <tr
                    key={r.id}
                    onClick={() => navigate(`/running/${r.id}`)}
                    className="cursor-pointer border-t border-white/10 hover:bg-white/5"
                  >
                    <td className="px-3 py-2 text-gray-300">
                      <span className="flex items-center gap-1.5">
                        <StateDot state={r.state} />
                        {r.state}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-gray-300">
                      {r.name} <span className="text-gray-500">({r.slug})</span>
                      {r.instrument === 'options' && (
                        <AssetTypeBadge type="OPTION" size="xs" className="ml-2 align-middle" />
                      )}
                      <LiveGapBadge gaps={r.live_support_gaps} />
                    </td>
                    <td className="px-3 py-2 text-gray-300">{r.mode}</td>
                    <td className="px-3 py-2 text-gray-300">
                      {r.started_at ? new Date(r.started_at).toLocaleString() : '—'}
                    </td>
                    <td className="px-3 py-2 text-gray-300">
                      {r.live ? Object.keys(r.live.positions).length : '—'}
                    </td>
                    <td className={`px-3 py-2 ${r.live ? pnlColor(r.live.realized_pnl_today) : 'text-gray-300'}`}>
                      {r.live ? r.live.realized_pnl_today.toFixed(2) : '—'}
                    </td>
                    {/* Lifetime, not today: a run that has been going for days
                        shows nothing useful in the daily column alone. */}
                    <td
                      className={`px-3 py-2 ${
                        r.realized_pnl_total_pct != null
                          ? pnlColor(r.realized_pnl_total_pct)
                          : 'text-gray-300'
                      }`}
                      title={`Realized P&L since start: ${r.realized_pnl_total.toFixed(2)}`}
                    >
                      {r.realized_pnl_total_pct != null
                        ? `${r.realized_pnl_total_pct >= 0 ? '+' : ''}${r.realized_pnl_total_pct.toFixed(2)}%`
                        : '—'}
                    </td>
                    <td className="px-3 py-2 text-gray-300">
                      {r.live ? r.live.orders_today : '—'}
                    </td>
                    <td className="px-3 py-2" onClick={(e) => e.stopPropagation()}>
                      {r.live && (
                        <div className="flex flex-wrap gap-1.5">
                          {r.state === 'RUNNING' && (
                            <button
                              onClick={() => doAction(r.id, 'pause')}
                              disabled={actionMutation.isPending}
                              className="rounded border border-white/10 px-2 py-1 text-xs text-gray-300 hover:bg-white/5 disabled:opacity-50"
                            >
                              Pause
                            </button>
                          )}
                          {r.state === 'PAUSED' && (
                            <button
                              onClick={() => doAction(r.id, 'resume')}
                              disabled={actionMutation.isPending}
                              className="rounded border border-white/10 px-2 py-1 text-xs text-gray-300 hover:bg-white/5 disabled:opacity-50"
                            >
                              Resume
                            </button>
                          )}
                          <button
                            onClick={() => doAction(r.id, 'flatten')}
                            disabled={actionMutation.isPending}
                            className="rounded border border-amber-500/30 px-2 py-1 text-xs text-amber-400 hover:bg-amber-500/10 disabled:opacity-50"
                          >
                            Flatten
                          </button>
                          <button
                            onClick={() => doAction(r.id, 'stop')}
                            disabled={actionMutation.isPending}
                            className="rounded border border-red-500/30 px-2 py-1 text-xs text-red-400 hover:bg-red-500/10 disabled:opacity-50"
                          >
                            Stop
                          </button>
                        </div>
                      )}
                      {!r.live && r.stopped_at == null && (
                        <div className="flex flex-wrap justify-end gap-2">
                          <button
                            onClick={() => recoverMutation.mutate(r.id)}
                            disabled={recoverMutation.isPending}
                            title={
                              r.mode === 'live'
                                ? "Reconcile this run against Schwab — adopt the positions and resting protective orders it actually reports — and take control of it again. Comes back PAUSED; it places no order until you activate live trading."
                                : "Rebuild this run in the app from its saved positions, cash and today's risk counters, re-place its protective orders, and resume it — it keeps its history."
                            }
                            className="rounded border border-teal-500/30 px-2 py-1 text-xs text-teal-400 hover:bg-teal-500/10 disabled:opacity-50"
                          >
                            Reconnect
                          </button>
                          <button
                            onClick={() => doAction(r.id, 'stop')}
                            disabled={actionMutation.isPending || recoverMutation.isPending}
                            title="No active process for this run (e.g. after a restart) — this only closes the local record, it does not touch anything on Schwab."
                            className="rounded border border-red-500/30 px-2 py-1 text-xs text-red-400 hover:bg-red-500/10 disabled:opacity-50"
                          >
                            Force stop
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {recoverMutation.isError && (
          <p className="mt-3 text-sm text-red-400">
            {recoverMutation.error instanceof ApiError
              ? `Reconnect failed: ${recoverMutation.error.message}`
              : 'Reconnect failed.'}
          </p>
        )}
        {recoverMutation.data && (
          <p className="mt-3 text-sm text-teal-300">
            Reconnected — {recoverMutation.data.positions} open position
            {recoverMutation.data.positions === 1 ? '' : 's'} restored
            {recoverMutation.data.adopted_protective_orders
              ? `, ${recoverMutation.data.adopted_protective_orders} resting protective order${
                  recoverMutation.data.adopted_protective_orders === 1 ? '' : 's'
                } adopted`
              : ''}
            {recoverMutation.data.started_paused ? ', PAUSED.' : '.'} Open the run for the full
            reconciliation detail.
          </p>
        )}
        {actionMutation.isError && (
          <p className="mt-3 text-sm text-red-400">
            {actionMutation.error instanceof ApiError
              ? actionMutation.error.message
              : 'Action failed.'}
          </p>
        )}
        {actionMutation.data?.forced && (
          <p className="mt-3 text-sm text-amber-400">
            Closed the local record only — this run had no active connection to the app, so
            {actionMutation.data.mode === 'live'
              ? ' any resting orders or positions on Schwab were NOT touched. Check your Schwab account directly.'
              : ' nothing on the broker side was touched.'}
          </p>
        )}
      </section>
    </div>
  )
}
