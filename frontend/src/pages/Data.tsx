import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { COVERAGE_TFS, getCoverage, getWatchlist, postBackfill, putWatchlist } from '../lib/api'
import OptionsDataPanel from '../components/options/OptionsDataPanel'
import { ASSET_STYLES } from '../components/AssetTypeBadge'
import DocsHelpLink from '../components/DocsHelpLink'

type DataTab = 'stocks' | 'options'

export default function Data() {
  const queryClient = useQueryClient()
  const [newSymbol, setNewSymbol] = useState('')
  const [backfillSymbol, setBackfillSymbol] = useState('')
  // Stocks and options have genuinely different data models -- equity bars are
  // backfillable on demand, option chains are only ever recorded forward -- so
  // they get separate tabs rather than one merged list that would imply they
  // behave alike.
  const [tab, setTab] = useState<DataTab>('stocks')

  const backfillMutation = useMutation({
    mutationFn: postBackfill,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['coverage'] })
    },
  })

  function runBackfill() {
    const trimmed = backfillSymbol.trim().toUpperCase()
    if (trimmed.length === 0 || backfillMutation.isPending) return
    backfillMutation.mutate(trimmed)
    setBackfillSymbol('')
  }

  const { data: watchlist, isLoading: watchlistLoading, isError: watchlistError } = useQuery({
    queryKey: ['watchlist'],
    queryFn: getWatchlist,
    retry: false,
  })

  const {
    data: coverage,
    isLoading: coverageLoading,
    isError: coverageError,
  } = useQuery({
    queryKey: ['coverage'],
    queryFn: getCoverage,
    retry: false,
  })

  const saveMutation = useMutation({
    mutationFn: putWatchlist,
    onSuccess: (data) => {
      queryClient.setQueryData(['watchlist'], data)
      queryClient.invalidateQueries({ queryKey: ['watchlist'] })
      queryClient.invalidateQueries({ queryKey: ['coverage'] })
    },
  })

  const symbols = watchlist?.symbols ?? []

  function addSymbol() {
    const trimmed = newSymbol.trim().toUpperCase()
    if (trimmed.length === 0) return
    if (symbols.includes(trimmed)) {
      setNewSymbol('')
      return
    }
    if (symbols.length >= 50) return
    saveMutation.mutate([...symbols, trimmed])
    setNewSymbol('')
  }

  function removeSymbol(symbol: string) {
    saveMutation.mutate(symbols.filter((s) => s !== symbol))
  }

  const coverageBySymbol = new Map(coverage?.coverage.map((c) => [c.symbol, c]) ?? [])
  const allSymbols = Array.from(
    new Set([...symbols, ...(coverage?.coverage.map((c) => c.symbol) ?? [])]),
  ).sort()

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-gray-100">Data</h1>
        <DocsHelpLink />
      </div>

      <div className="mb-5 flex gap-1 border-b border-white/10">
        {(['stocks', 'options'] as const).map((t) => {
          const active = tab === t
          const style = t === 'stocks' ? ASSET_STYLES.EQUITY : ASSET_STYLES.OPTION
          return (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium capitalize transition-colors ${
                active
                  ? `${style.text} ${t === 'stocks' ? 'border-emerald-500' : 'border-amber-500'}`
                  : 'border-transparent text-gray-500 hover:text-gray-300'
              }`}
            >
              {t}
            </button>
          )
        })}
      </div>

      {tab === 'options' && <OptionsDataPanel />}

      {tab === 'stocks' && (
      <>
      <div className="mb-6 grid grid-cols-1 gap-4 xl:grid-cols-2">
      <section className="rounded border border-white/10 p-4">
        <h2 className="mb-2 text-sm font-semibold text-gray-200">Watchlist</h2>
        <p className="mb-3 text-xs text-gray-500">
          Watchlisted symbols stream 1-minute bars into the local store whenever the app is
          running — recorded history becomes permanent intraday backtest depth.
        </p>

        {watchlistLoading && <p className="text-sm text-gray-500">Loading watchlist…</p>}
        {watchlistError && (
          <p className="text-sm text-red-400">Could not load watchlist.</p>
        )}

        {watchlist && (
          <>
            <div className="mb-3 flex flex-wrap gap-2">
              {symbols.length === 0 && (
                <span className="text-sm text-gray-600">No symbols yet.</span>
              )}
              {symbols.map((s) => (
                <span
                  key={s}
                  className="flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 py-1 pl-3 pr-1.5 text-xs text-gray-300"
                >
                  {s}
                  <button
                    onClick={() => removeSymbol(s)}
                    disabled={saveMutation.isPending}
                    aria-label={`Remove ${s}`}
                    className="rounded-full px-1.5 text-gray-500 hover:bg-white/10 hover:text-gray-200 disabled:opacity-50"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>

            <div className="flex items-center gap-2">
              <input
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value.toUpperCase())}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') addSymbol()
                }}
                placeholder="Add symbol"
                className="w-32 rounded border border-white/10 bg-white/5 px-3 py-1.5 text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500 focus:outline-none"
              />
              <button
                onClick={addSymbol}
                disabled={saveMutation.isPending || symbols.length >= 50}
                className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
              >
                Add
              </button>
              {symbols.length >= 50 && (
                <span className="text-xs text-gray-500">Max 50 symbols.</span>
              )}
            </div>

            {saveMutation.isError && (
              <p className="mt-2 text-sm text-red-400">
                {saveMutation.error instanceof Error
                  ? saveMutation.error.message
                  : 'Failed to save watchlist.'}
              </p>
            )}
          </>
        )}
      </section>

      <section className="rounded border border-white/10 p-4">
        <h2 className="mb-2 text-sm font-semibold text-gray-200">One-time backfill</h2>
        <p className="mb-3 text-xs text-gray-500">
          Pull full history for any ticker right now, without waiting for recording to
          accumulate: 20 years of daily bars, all available 1-minute bars (~6 weeks), and
          all available native 5m/15m/30m bars (~8.5 months). Schwab's history limit is
          per-frequency, so the coarser intraday bars reach back much further than 1m —
          they're fetched and stored directly rather than aggregated up from 1m. Safe to
          re-run on an existing symbol to top it up.
        </p>
        <div className="flex items-center gap-2">
          <input
            value={backfillSymbol}
            onChange={(e) => setBackfillSymbol(e.target.value.toUpperCase())}
            onKeyDown={(e) => {
              if (e.key === 'Enter') runBackfill()
            }}
            placeholder="Ticker"
            className="w-32 rounded border border-white/10 bg-white/5 px-3 py-1.5 text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500 focus:outline-none"
          />
          <button
            onClick={runBackfill}
            disabled={backfillMutation.isPending || backfillSymbol.trim().length === 0}
            className="rounded bg-teal-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-teal-500 disabled:opacity-50"
          >
            {backfillMutation.isPending ? 'Backfilling…' : 'Backfill'}
          </button>
          {backfillMutation.isPending && (
            <span className="text-xs text-gray-500">
              Fetching history from Schwab — takes ~20–60s…
            </span>
          )}
        </div>
        {backfillMutation.isSuccess && (
          <p className="mt-2 text-sm text-teal-400">
            {backfillMutation.data.symbol}: {backfillMutation.data.daily_bars_fetched} daily
            + {backfillMutation.data.minute_bars_fetched} 1-minute bars fetched
            {Object.keys(backfillMutation.data.intraday_bars_fetched ?? {}).length > 0 && (
              <>
                {' '}
                (plus{' '}
                {Object.entries(backfillMutation.data.intraday_bars_fetched)
                  .map(([tf, n]) => `${n} ${tf}`)
                  .join(', ')}
                )
              </>
            )}
            .
          </p>
        )}
        {backfillMutation.isError && (
          <p className="mt-2 text-sm text-red-400">
            {backfillMutation.error instanceof Error
              ? backfillMutation.error.message
              : 'Backfill failed.'}
          </p>
        )}
      </section>
      </div>

      <section className="rounded border border-white/10 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-200">Coverage</h2>
          {coverage && (
            <span className="text-xs text-gray-500">
              recorded this session: {coverage.recorded_bars_this_session} bars
            </span>
          )}
        </div>

        {coverageLoading && <p className="text-sm text-gray-500">Loading coverage…</p>}
        {coverageError && <p className="text-sm text-red-400">Could not load coverage.</p>}

        {coverage && allSymbols.length === 0 && (
          <p className="text-sm text-gray-600">No coverage data yet.</p>
        )}

        {coverage && allSymbols.length > 0 && (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Symbol</th>
                  {COVERAGE_TFS.map((tf) => (
                    <th key={tf} className="whitespace-nowrap px-3 py-2 font-medium">
                      {tf}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {allSymbols.map((symbol) => {
                  const entry = coverageBySymbol.get(symbol)
                  return (
                    <tr key={symbol} className="border-t border-white/10 align-top">
                      <td className="px-3 py-2 text-gray-300">{symbol}</td>
                      {COVERAGE_TFS.map((tf) => {
                        const cov = entry?.[tf] ?? null
                        return (
                          <td key={tf} className="whitespace-nowrap px-3 py-2 text-gray-300">
                            {cov ? (
                              <>
                                <div>{cov.bars.toLocaleString()} bars</div>
                                <div className="text-xs text-gray-500">
                                  {formatRange(cov)}
                                </div>
                              </>
                            ) : (
                              '—'
                            )}
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
      </>
      )}
    </div>
  )
}

function formatDate(ms: number): string {
  const d = new Date(ms)
  const year = d.getFullYear()
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function formatRange(cov: { first_ts: number; last_ts: number } | null): string {
  if (!cov) return '—'
  return `${formatDate(cov.first_ts)} → ${formatDate(cov.last_ts)}`
}
