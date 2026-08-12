import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getOptionCoverage,
  postOptionSnapshot,
  putOptionWatchlist,
} from '../../lib/api'
import SnapshotDensityPanel from './SnapshotDensityPanel'
import TickRecordingPanel from './TickRecordingPanel'

/**
 * Option chain recording: watchlist, coverage, and snapshot diagnostics.
 *
 * The framing here is deliberate and repeated in the copy. Schwab sells NO
 * historical option prices, so unlike equity bars — which can be backfilled 20
 * years deep on demand — option history only exists if this app recorded it on
 * the day. That makes "start recording now" the single most valuable action on
 * this page, and makes a gap in coverage permanent. The UI says so plainly
 * rather than letting a user discover it months later.
 */
export default function OptionsDataPanel() {
  const queryClient = useQueryClient()
  const [newSymbol, setNewSymbol] = useState('')

  const { data, isLoading, isError } = useQuery({
    queryKey: ['optionCoverage'],
    queryFn: getOptionCoverage,
    retry: false,
  })

  const saveMutation = useMutation({
    mutationFn: putOptionWatchlist,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['optionCoverage'] }),
  })

  const snapshotMutation = useMutation({
    mutationFn: (symbol?: string) => postOptionSnapshot(symbol),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['optionCoverage'] }),
  })

  const symbols = data?.watchlist ?? []
  const coverageBy = new Map(data?.coverage.map((c) => [c.underlying, c]) ?? [])
  const allSymbols = Array.from(
    new Set([...symbols, ...(data?.coverage.map((c) => c.underlying) ?? [])]),
  ).sort()

  function addSymbol() {
    const t = newSymbol.trim().toUpperCase()
    if (!t || symbols.includes(t) || symbols.length >= 50) {
      setNewSymbol('')
      return
    }
    saveMutation.mutate([...symbols, t])
    setNewSymbol('')
  }

  return (
    <>
      <section className="mb-6 rounded border border-amber-500/20 bg-amber-500/[0.03] p-4">
        <h2 className="mb-2 text-sm font-semibold text-amber-200">
          Why option data must be recorded
        </h2>
        <p className="text-xs leading-relaxed text-gray-400">
          Schwab serves <strong className="text-gray-300">no historical option prices</strong>{' '}
          — its price-history endpoint is equities-only. Every option price this app will
          ever have for a past date exists because it was captured live on that day. A day
          not recorded is gone permanently and cannot be backfilled, which is the opposite
          of how the Stocks tab behaves.
        </p>
        <p className="mt-2 text-xs leading-relaxed text-gray-400">
          Until enough is recorded, options backtests price contracts with a{' '}
          <strong className="text-gray-300">model</strong> rather than observed quotes.
          Recording is what eventually replaces the model with reality — and what measures
          how wrong the model was.
        </p>
      </section>

      <section className="mb-6 rounded border border-white/10 p-4">
        <h2 className="mb-2 text-sm font-semibold text-gray-200">Chain recording watchlist</h2>
        <p className="mb-3 text-xs text-gray-500">
          Full-chain snapshots per underlying on every weekday session, at{' '}
          {data?.schedule?.length
            ? `${data.schedule.length} times (${data.schedule[0]}–${
                data.schedule[data.schedule.length - 1]
              } ET)`
            : '15:50 ET'}
          . Roughly 400–900 contracts per underlying per snapshot. Independent of the
          Stocks watchlist.
        </p>
        {data?.schedule && data.schedule.length > 0 && (
          <div className="mb-3 flex flex-wrap gap-1">
            {data.schedule.map((t) => (
              <span
                key={t}
                className="rounded border border-white/10 px-1.5 py-0.5 font-mono text-[10px] text-gray-500"
              >
                {t}
              </span>
            ))}
          </div>
        )}

        {isLoading && <p className="text-sm text-gray-500">Loading…</p>}
        {isError && <p className="text-sm text-red-400">Could not load options coverage.</p>}

        {data && (
          <>
            <div className="mb-3 flex flex-wrap gap-2">
              {symbols.length === 0 && (
                <span className="text-sm text-gray-600">
                  No underlyings recorded yet — options data collection is off.
                </span>
              )}
              {symbols.map((s) => (
                <span
                  key={s}
                  className="flex items-center gap-1.5 rounded-full border border-amber-500/30 bg-amber-500/10 py-1 pl-3 pr-1.5 text-xs text-amber-200"
                >
                  {s}
                  <button
                    onClick={() => saveMutation.mutate(symbols.filter((x) => x !== s))}
                    disabled={saveMutation.isPending}
                    aria-label={`Remove ${s}`}
                    className="rounded-full px-1.5 text-amber-500/70 hover:bg-white/10 hover:text-amber-200 disabled:opacity-50"
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <input
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value.toUpperCase())}
                onKeyDown={(e) => e.key === 'Enter' && addSymbol()}
                placeholder="Underlying"
                className="w-32 rounded border border-white/10 bg-white/5 px-3 py-1.5 text-sm text-gray-200 placeholder:text-gray-600 focus:border-amber-500 focus:outline-none"
              />
              <button
                onClick={addSymbol}
                disabled={saveMutation.isPending || symbols.length >= 50}
                className="rounded bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-500 disabled:opacity-50"
              >
                Add
              </button>
              <button
                onClick={() => snapshotMutation.mutate(undefined)}
                disabled={snapshotMutation.isPending || symbols.length === 0}
                className="rounded border border-amber-500/40 px-3 py-1.5 text-sm font-medium text-amber-200 hover:bg-amber-500/10 disabled:opacity-50"
              >
                {snapshotMutation.isPending ? 'Snapshotting…' : 'Snapshot now'}
              </button>
              {symbols.length >= 50 && (
                <span className="text-xs text-gray-500">Max 50 underlyings.</span>
              )}
            </div>

            {snapshotMutation.isSuccess && (
              <p className="mt-2 text-sm text-amber-300">
                {snapshotMutation.data.results
                  .map((r) =>
                    r.error
                      ? `${r.underlying}: failed (${r.error})`
                      : `${r.underlying}: ${r.written} contracts`,
                  )
                  .join(' · ')}
              </p>
            )}
            {(saveMutation.isError || snapshotMutation.isError) && (
              <p className="mt-2 text-sm text-red-400">
                {(saveMutation.error ?? snapshotMutation.error) instanceof Error
                  ? ((saveMutation.error ?? snapshotMutation.error) as Error).message
                  : 'Request failed.'}
              </p>
            )}
          </>
        )}
      </section>

      <section className="mb-6 rounded border border-white/10 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-200">Recorded coverage</h2>
          {data && (
            <span className="text-xs text-gray-500">
              this session: {data.snapshots_taken} snapshots ·{' '}
              {data.rows_recorded.toLocaleString()} contracts
            </span>
          )}
        </div>

        {data && allSymbols.length === 0 && (
          <p className="text-sm text-gray-600">Nothing recorded yet.</p>
        )}

        {data && allSymbols.length > 0 && (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Underlying</th>
                  <th className="px-3 py-2 font-medium">Recorded range</th>
                  <th className="px-3 py-2 font-medium">Snapshots</th>
                  <th className="px-3 py-2 font-medium">Contracts</th>
                  <th className="px-3 py-2 font-medium">Avg / snapshot</th>
                  <th className="px-3 py-2 font-medium">Expiries</th>
                </tr>
              </thead>
              <tbody>
                {allSymbols.map((sym) => {
                  const c = coverageBy.get(sym)
                  return (
                    <tr key={sym} className="border-t border-white/10">
                      <td className="px-3 py-2 text-amber-200">{sym}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-gray-300">
                        {c ? `${fmtDate(c.first_ts)} → ${fmtDate(c.last_ts)}` : (
                          <span className="text-gray-600">not recorded yet</span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-gray-300">{c?.snapshots ?? '—'}</td>
                      <td className="px-3 py-2 text-gray-300">
                        {c ? c.rows.toLocaleString() : '—'}
                      </td>
                      <td className="px-3 py-2 text-gray-300">
                        {c?.mean_contracts_per_snapshot ?? '—'}
                      </td>
                      <td className="px-3 py-2 text-gray-300">{c?.expiries ?? '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <SnapshotDensityPanel symbols={allSymbols} />

      <TickRecordingPanel />

      <section className="rounded border border-white/10 p-4">
        <h2 className="mb-2 text-sm font-semibold text-gray-200">Snapshot diagnostics</h2>
        <p className="mb-3 text-xs text-gray-500">
          Contracts dropped by the data-quality guards — stale quotes, crossed markets,
          adjusted-contract roots, deep wings. A high rejection rate is worth investigating
          rather than ignoring, which is why it is shown here instead of only in the log.
        </p>
        {data && data.recent_runs.length === 0 && (
          <p className="text-sm text-gray-600">No snapshots taken yet.</p>
        )}
        {data && data.recent_runs.length > 0 && (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">When</th>
                  <th className="px-3 py-2 font-medium">Underlying</th>
                  <th className="px-3 py-2 font-medium">Price</th>
                  <th className="px-3 py-2 font-medium">Kept</th>
                  <th className="px-3 py-2 font-medium">Dropped</th>
                  <th className="px-3 py-2 font-medium">Reasons</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_runs.map((r, i) => (
                  <tr key={`${r.underlying}-${r.ts}-${i}`} className="border-t border-white/10">
                    <td className="whitespace-nowrap px-3 py-2 text-gray-400">
                      {fmtDateTime(r.ts)}
                    </td>
                    <td className="px-3 py-2 text-amber-200">{r.underlying}</td>
                    <td className="px-3 py-2 text-gray-300">
                      {r.underlying_px != null ? r.underlying_px.toFixed(2) : '—'}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{r.written}</td>
                    <td className="px-3 py-2 text-gray-300">
                      {r.rejected > 0 ? (
                        <span
                          className={
                            r.requested > 0 && r.rejected / r.requested > 0.4
                              ? 'text-amber-300'
                              : 'text-gray-400'
                          }
                        >
                          {r.rejected}
                          {r.requested > 0 &&
                            ` (${Math.round((r.rejected / r.requested) * 100)}%)`}
                        </span>
                      ) : (
                        '0'
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs text-gray-500">
                      {r.error ? (
                        <span className="text-red-400">{r.error}</span>
                      ) : (
                        Object.entries(r.reject_reasons)
                          .sort((a, b) => b[1] - a[1])
                          .map(([k, v]) => `${k} ${v}`)
                          .join(', ') || '—'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}

function fmtDate(ms: number): string {
  const d = new Date(ms)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate(),
  ).padStart(2, '0')}`
}

function fmtDateTime(ms: number): string {
  const d = new Date(ms)
  return `${fmtDate(ms)} ${String(d.getHours()).padStart(2, '0')}:${String(
    d.getMinutes(),
  ).padStart(2, '0')}`
}
