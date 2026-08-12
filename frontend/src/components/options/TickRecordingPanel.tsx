import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  getOptionTickCoverage,
  refreshOptionTickUniverse,
  type OptionTickDay,
} from '../../lib/api'

/**
 * Streamed option quotes — the high-resolution half of options recording.
 *
 * The distinction this panel has to make clear, because it decides what a
 * backtest can honestly claim: chain snapshots are POINT SAMPLES of the whole
 * chain every 30 minutes, so a contract that spiked and came back between two
 * slots looks like it never moved. Streamed quotes cover only a narrow band
 * around the money, but they cover it continuously — which is what makes an
 * intrabar stop simulable at all.
 *
 * `dropped_conflated` is given its own prominence on purpose. It is the only
 * number here that represents information genuinely thrown away.
 */
export default function TickRecordingPanel() {
  const queryClient = useQueryClient()
  const { data, isLoading, isError } = useQuery({
    queryKey: ['optionTickCoverage'],
    queryFn: () => getOptionTickCoverage(14),
    retry: false,
    refetchInterval: 30_000,
  })

  const refresh = useMutation({
    mutationFn: refreshOptionTickUniverse,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['optionTickCoverage'] }),
  })

  const s = data?.stats
  const sum = data?.summary
  const changes = s ? s.recorded + s.dropped_conflated : 0
  const suppressedPct = changes > 0 ? (s!.dropped_conflated / changes) * 100 : 0

  // by_day arrives newest-first, one row per (underlying, day); group by date so
  // the table reads as a session log rather than a symbol list.
  const byDate = new Map<string, OptionTickDay[]>()
  for (const r of data?.by_day ?? []) {
    const list = byDate.get(r.date) ?? []
    list.push(r)
    byDate.set(r.date, list)
  }

  return (
    <section className="mb-6 rounded border border-white/10 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-gray-200">Streamed quotes (tick recording)</h2>
        <div className="flex items-center gap-2">
          {s && (
            <span
              className={`rounded px-2 py-0.5 text-xs ${
                s.enabled
                  ? 'bg-emerald-500/10 text-emerald-300'
                  : 'bg-white/5 text-gray-500'
              }`}
            >
              {s.enabled ? 'recording' : 'off'}
            </span>
          )}
          <button
            onClick={() => refresh.mutate()}
            disabled={refresh.isPending}
            className="rounded border border-amber-500/40 px-2.5 py-1 text-xs font-medium text-amber-200 hover:bg-amber-500/10 disabled:opacity-50"
            title="Rebuild the streamed contract band now instead of waiting for the next 15-minute refresh"
          >
            {refresh.isPending ? 'Refreshing…' : 'Refresh universe'}
          </button>
        </div>
      </div>

      <p className="mb-3 text-xs leading-relaxed text-gray-500">
        Every quote change for a band around the money, written continuously — unlike the
        chain snapshots above, which sample the whole chain every 30 minutes and cannot see
        what happened in between. This is what lets a backtest check an intrabar premium
        stop instead of only the bar close.
      </p>

      {isLoading && <p className="text-sm text-gray-500">Loading…</p>}
      {isError && <p className="text-sm text-red-400">Could not load tick coverage.</p>}

      {s && (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Contracts streaming" value={s.universe.toLocaleString()}
                  sub={`±${s.strikes_each_side} strikes · ${s.expiries} expiries`} />
            <Stat label="Ticks stored" value={(sum?.rows ?? 0).toLocaleString()}
                  sub={`${(sum?.contracts ?? 0).toLocaleString()} contracts`} />
            <Stat label="History depth"
                  value={sum?.days ? `${sum.days} ${sum.days === 1 ? 'day' : 'days'}` : '—'}
                  sub={sum?.first_ts ? `since ${fmtDateTime(sum.first_ts)}` : 'nothing yet'} />
            <Stat
              label="Suppressed"
              value={`${suppressedPct.toFixed(1)}%`}
              sub={
                s.min_interval_ms === 0
                  ? 'no conflation — full fidelity'
                  : `min interval ${s.min_interval_ms}ms`
              }
              tone={s.min_interval_ms === 0 ? 'good' : suppressedPct > 10 ? 'warn' : undefined}
            />
          </div>

          <p className="mb-4 text-xs leading-relaxed text-gray-500">
            This session: {s.recorded.toLocaleString()} written ·{' '}
            <span className={s.dropped_conflated > 0 ? 'text-amber-300' : ''}>
              {s.dropped_conflated.toLocaleString()} real moves suppressed
            </span>{' '}
            · {s.dropped_unchanged.toLocaleString()} unchanged repeats dropped (free).
            {s.min_interval_ms > 0 && (
              <>
                {' '}Set <code className="text-gray-400">options_tick_min_interval_ms=0</code> to
                keep every change — it is the finest resolution the feed offers.
              </>
            )}
          </p>

          {byDate.size === 0 ? (
            <p className="text-sm text-gray-600">
              No streamed quotes recorded yet. The band builds at startup and refreshes every
              15 minutes while the market is open.
            </p>
          ) : (
            <div className="overflow-x-auto rounded border border-white/10">
              <table className="w-full text-left text-sm">
                <thead className="bg-white/5 text-gray-400">
                  <tr>
                    <th className="px-3 py-2 font-medium">Date</th>
                    <th className="px-3 py-2 font-medium">Underlying</th>
                    <th className="px-3 py-2 font-medium">Ticks</th>
                    <th className="px-3 py-2 font-medium">Contracts</th>
                    <th className="px-3 py-2 font-medium">Covered</th>
                  </tr>
                </thead>
                <tbody>
                  {[...byDate.entries()].map(([date, rows]) =>
                    rows.map((r, i) => (
                      <tr key={`${date}-${r.underlying}`} className="border-t border-white/10">
                        <td className="whitespace-nowrap px-3 py-2 text-gray-400">
                          {i === 0 ? date : ''}
                        </td>
                        <td className="px-3 py-2 text-amber-200">{r.underlying}</td>
                        <td className="px-3 py-2 text-gray-300">{r.rows.toLocaleString()}</td>
                        <td className="px-3 py-2 text-gray-300">{r.contracts}</td>
                        <td className="whitespace-nowrap px-3 py-2 text-gray-400">
                          {fmtTime(r.first_ts)} – {fmtTime(r.last_ts)}
                        </td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
          )}

          <p className="mt-3 text-xs text-gray-600">
            Tick data ages out on a retention schedule (chain snapshots never do — they are
            irreplaceable). Deleted ticks cannot be recovered from any source.
          </p>
        </>
      )}
    </section>
  )
}

function Stat({
  label, value, sub, tone,
}: {
  label: string
  value: string
  sub?: string
  tone?: 'good' | 'warn'
}) {
  const color =
    tone === 'warn' ? 'text-amber-300' : tone === 'good' ? 'text-emerald-300' : 'text-gray-100'
  return (
    <div className="rounded border border-white/10 bg-white/[0.02] px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`font-mono text-lg ${color}`}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-gray-500">{sub}</div>}
    </div>
  )
}

function fmtTime(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function fmtDateTime(ms: number): string {
  const d = new Date(ms)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate(),
  ).padStart(2, '0')} ${fmtTime(ms)}`
}
