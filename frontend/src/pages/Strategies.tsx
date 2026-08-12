import { AssetTypeBadge } from '../components/AssetTypeBadge'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiError, getRuns, getStrategies, setStrategyArchived, setStrategyStarred } from '../lib/api'
import type { RunSummary } from '../lib/api'
import { fmtNum } from '../components/sweep/SweepResults'
import { StateDot } from './Running'

const MAX_TAGS = 3  // beyond this, tags collapse to a +N chip

function formatReturn(pct: number | null): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return '—'
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`
}

function returnClass(pct: number | null): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return 'text-gray-500'
  return pct >= 0 ? 'text-emerald-400' : 'text-red-400'
}

function formatRange(start: string | null, end: string | null): string {
  if (!start || !end) return ''
  return `${start} → ${end}`
}

/** "This strategy is running now" indicator. It sits under the star in the
 *  narrow leading column -- that column has no text to align to, so the name
 *  column stays flush. Too narrow for a label, hence dot-only + tooltip. */
function RunDot({ run, onOpen }: { run: RunSummary; onOpen: () => void }) {
  return (
    <button
      onClick={(e) => {
        e.stopPropagation()
        onOpen()
      }}
      title={`${run.mode} run — ${run.state}. Open run`}
      aria-label={`${run.state} run, open it`}
      className={`flex h-3.5 w-3.5 items-center justify-center rounded-full hover:bg-white/10 ${
        run.state === 'RUNNING' ? 'animate-pulse' : ''
      }`}
    >
      <StateDot state={run.state} />
    </button>
  )
}

export default function Strategies() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [tab, setTab] = useState<'active' | 'archived'>('active')
  const [favoritesOnly, setFavoritesOnly] = useState(false)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['strategies', tab],
    queryFn: () => getStrategies({ archived: tab === 'archived' }),
    retry: false,
  })

  // a run only counts as "currently running" when the run manager still has it
  // in process (r.live present) -- a STOPPED/ERROR DB row must not light a row up
  const { data: runsData } = useQuery({
    queryKey: ['runs'],
    queryFn: getRuns,
    refetchInterval: 5000,
    retry: false,
  })

  const activeBySlug = new Map<string, RunSummary>()
  for (const r of runsData?.runs ?? []) {
    if (r.live && !activeBySlug.has(r.slug)) activeBySlug.set(r.slug, r)
  }

  const starMutation = useMutation({
    mutationFn: ({ slug, starred }: { slug: string; starred: boolean }) =>
      setStrategyStarred(slug, starred),
    // row order is unaffected by starring; refetch just resyncs the flag
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['strategies'] }),
  })

  const archiveMutation = useMutation({
    mutationFn: ({ slug, archived }: { slug: string; archived: boolean }) =>
      setStrategyArchived(slug, archived),
    onSuccess: () => {
      // both tabs' lists shift membership on a toggle
      queryClient.invalidateQueries({ queryKey: ['strategies'] })
    },
  })

  const strategies = data
    ? data.strategies.filter((s) => !favoritesOnly || s.starred)
    : []

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-gray-100">Strategies</h1>
        <button
          onClick={() => navigate('/strategies/new')}
          className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500"
        >
          New strategy
        </button>
      </div>

      <div className="mb-4 flex gap-2 rounded border border-white/10 p-1 w-fit">
        <button
          onClick={() => setTab('active')}
          className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
            tab === 'active' ? 'bg-emerald-600 text-white' : 'text-gray-400 hover:bg-white/5'
          }`}
        >
          Active
        </button>
        <button
          onClick={() => setTab('archived')}
          className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
            tab === 'archived' ? 'bg-emerald-600 text-white' : 'text-gray-400 hover:bg-white/5'
          }`}
        >
          Archived
        </button>
        <button
          onClick={() => setFavoritesOnly((v) => !v)}
          className={`ml-2 rounded px-3 py-1 text-xs font-medium transition-colors ${
            favoritesOnly ? 'bg-amber-500/20 text-amber-300' : 'text-gray-400 hover:bg-white/5'
          }`}
        >
          ★ Favorites only
        </button>
      </div>

      {isLoading && <p className="text-sm text-gray-500">Loading strategies…</p>}
      {isError && <p className="text-sm text-red-400">Could not load strategies.</p>}

      {(archiveMutation.isError || starMutation.isError) && (
        <p className="mb-3 text-sm text-red-400">
          {(archiveMutation.error ?? starMutation.error) instanceof ApiError
            ? ((archiveMutation.error ?? starMutation.error) as ApiError).message
            : 'Failed to update strategy.'}
        </p>
      )}

      {data && data.errors.length > 0 && (
        <div className="mb-4 rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-300">
          <p className="mb-1 font-medium">Some strategies failed to load:</p>
          <ul className="list-inside list-disc space-y-0.5">
            {data.errors.map((err, i) => (
              <li key={i}>
                <span className="text-amber-400">{err.path}</span>: {err.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {data && strategies.length === 0 && (
        <p className="text-sm text-gray-600">
          {favoritesOnly
            ? 'No favorites yet. Click the star on a strategy to add one.'
            : tab === 'active'
              ? 'No strategies yet. Create one to get started.'
              : 'No archived strategies.'}
        </p>
      )}

      {data && strategies.length > 0 && (
        <div className="overflow-x-auto rounded border border-white/10">
          <table className="w-full text-left text-sm">
            <thead className="bg-white/5 text-gray-400">
              <tr>
                <th className="w-8 px-3 py-2 font-medium"><span className="sr-only">Favorite</span></th>
                <th className="w-2/5 px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Symbols</th>
                <th className="px-3 py-2 font-medium whitespace-nowrap">Bar</th>
                <th className="px-3 py-2 font-medium">Last backtest</th>
                <th className="px-3 py-2 font-medium whitespace-nowrap" title="Worst peak-to-trough equity decline in that backtest">Max DD</th>
                <th className="px-3 py-2 font-medium" title="Annualized Sharpe ratio for that backtest">Sharpe</th>
                <th className="px-3 py-2 font-medium">Version</th>
                <th className="px-3 py-2 font-medium">Hash</th>
                <th className="px-3 py-2 font-medium">Tags</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {strategies.map((s) => {
                const run = activeBySlug.get(s.slug)
                return (
                <tr
                  key={s.slug}
                  onClick={() => navigate(`/strategies/${s.slug}`)}
                  className="h-14 cursor-pointer border-t border-white/10 hover:bg-white/5"
                >
                  <td className="px-3 py-2">
                    <div className="flex flex-col items-center gap-1">
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        starMutation.mutate({ slug: s.slug, starred: !s.starred })
                      }}
                      disabled={starMutation.isPending}
                      title={s.starred ? 'Remove from favorites' : 'Add to favorites'}
                      aria-label={s.starred ? 'Remove from favorites' : 'Add to favorites'}
                      aria-pressed={s.starred}
                      className={`text-base leading-none disabled:opacity-50 ${
                        s.starred ? 'text-amber-400' : 'text-gray-600 hover:text-gray-400'
                      }`}
                    >
                      {s.starred ? '★' : '☆'}
                    </button>
                    {run && <RunDot run={run} onOpen={() => navigate(`/running/${run.id}`)} />}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-gray-200">
                    {/* the name is the ONE column allowed to wrap: it sets the
                        row height and is never truncated. Every other cell is
                        clipped to a single line, so nothing else can grow a
                        row -- rows stay uniform unless a long name says so. */}
                    <div className="min-w-[20rem] leading-tight break-words">
                      {/* live-run indicator rides inside the name cell so the
                          table keeps its column count; it only appears for
                          strategies with an in-process run */}
                      {s.name}
                      {s.instrument === 'options' && (
                        <AssetTypeBadge type="OPTION" size="xs" className="ml-2 align-middle" />
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-gray-400">
                    {/* universes run to 100 symbols; one clipped line keeps every
                        row the same height, full list in the tooltip */}
                    <div className="max-w-[14rem] truncate" title={s.symbols.join(', ')}>
                      {s.symbols.join(', ')}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-gray-400">{s.bar}</td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    {s.last_backtest ? (
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          navigate(`/backtests/${s.last_backtest!.id}`)
                        }}
                        className="text-left hover:underline"
                        title="Open this backtest"
                      >
                        <span className={returnClass(s.last_backtest.total_return_pct)}>
                          {formatReturn(s.last_backtest.total_return_pct)}
                        </span>
                        <span className="block text-xs text-gray-500">
                          {formatRange(s.last_backtest.start, s.last_backtest.end)}
                        </span>
                      </button>
                    ) : (
                      <span className="text-xs text-gray-600">Never run</span>
                    )}
                  </td>
                  {/* risk alongside return: drawdown is stored negative
                      (metrics.py takes dd.min()), so it needs no sign flip */}
                  <td className="px-3 py-2 whitespace-nowrap text-red-400">
                    {s.last_backtest?.max_drawdown_pct != null
                      ? `${fmtNum(s.last_backtest.max_drawdown_pct)}%`
                      : '—'}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap text-gray-300">
                    {s.last_backtest ? fmtNum(s.last_backtest.sharpe) : '—'}
                  </td>
                  <td className="px-3 py-2 text-gray-400">v{s.version}</td>
                  <td className="px-3 py-2">
                    <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 font-mono text-xs text-gray-400">
                      {s.content_hash}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    {/* one line only: extras collapse to a +N chip so a
                        heavily tagged strategy can't grow its row */}
                    <div className="flex gap-1" title={s.tags.join(', ')}>
                      {s.tags.slice(0, MAX_TAGS).map((t) => (
                        <span
                          key={t}
                          className="max-w-[7rem] shrink-0 truncate rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-xs text-gray-400"
                        >
                          {t}
                        </span>
                      ))}
                      {s.tags.length > MAX_TAGS && (
                        <span className="shrink-0 px-1 py-0.5 text-xs text-gray-500">
                          +{s.tags.length - MAX_TAGS}
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        archiveMutation.mutate({ slug: s.slug, archived: tab === 'active' })
                      }}
                      disabled={archiveMutation.isPending}
                      className="rounded border border-white/10 px-2.5 py-1 text-xs font-medium text-gray-300 hover:bg-white/5 disabled:opacity-50"
                    >
                      {tab === 'active' ? 'Archive' : 'Restore'}
                    </button>
                  </td>
                </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
