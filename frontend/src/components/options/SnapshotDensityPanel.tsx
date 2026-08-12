import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getOptionIntradayCoverage } from '../../lib/api'

/**
 * Snapshots per session day, for one underlying.
 *
 * The number that decides whether an intraday backtest is defensible. A day
 * showing 1 snapshot is a day whose only chain is the 15:50 one — any intraday
 * result over that day is the model talking, not recorded data. Averaged
 * coverage figures hide exactly this, which is why it is broken out per day.
 */
export default function SnapshotDensityPanel({ symbols }: { symbols: string[] }) {
  const [symbol, setSymbol] = useState(symbols[0] ?? '')
  const active = symbols.includes(symbol) ? symbol : (symbols[0] ?? '')

  const { data, isLoading } = useQuery({
    queryKey: ['optionIntradayCoverage', active],
    queryFn: () => getOptionIntradayCoverage(active, 30),
    enabled: !!active,
    retry: false,
  })

  if (symbols.length === 0) return null
  const expected = data?.expected_per_day ?? 0

  return (
    <section className="mb-6 rounded border border-white/10 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-gray-200">Snapshot density per day</h2>
        <div className="flex flex-wrap gap-1.5">
          {symbols.map((s) => (
            <button
              key={s}
              onClick={() => setSymbol(s)}
              className={`rounded border px-2 py-0.5 text-xs transition-colors ${
                s === active
                  ? 'border-amber-500 bg-amber-500/15 text-amber-200'
                  : 'border-white/10 text-gray-400 hover:bg-white/5'
              }`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      <p className="mb-3 text-xs leading-relaxed text-gray-500">
        A complete session is <strong className="text-gray-400">{expected || '—'}</strong>{' '}
        snapshots. A day showing 1 has only its 15:50 close chain, so any intraday backtest
        over that day is modelled rather than measured — days recorded before intraday
        snapshotting was enabled will read that way permanently.
      </p>

      {isLoading && <p className="text-sm text-gray-500">Loading…</p>}
      {data && data.by_day.length === 0 && (
        <p className="text-sm text-gray-600">Nothing recorded for {active} in the last 30 days.</p>
      )}

      {data && data.by_day.length > 0 && (
        <div className="overflow-x-auto rounded border border-white/10">
          <table className="w-full text-left text-sm">
            <thead className="bg-white/5 text-gray-400">
              <tr>
                <th className="px-3 py-2 font-medium">Date</th>
                <th className="px-3 py-2 font-medium">Snapshots</th>
                <th className="px-3 py-2 font-medium">Coverage</th>
                <th className="px-3 py-2 font-medium">Rows</th>
                <th className="px-3 py-2 font-medium">Window</th>
              </tr>
            </thead>
            <tbody>
              {data.by_day.map((d) => {
                const full = expected > 0 && d.snapshots >= expected
                const thin = d.snapshots <= 1
                return (
                  <tr key={d.date} className="border-t border-white/10">
                    <td className="whitespace-nowrap px-3 py-2 text-gray-300">{d.date}</td>
                    <td
                      className={`px-3 py-2 font-mono ${
                        thin ? 'text-amber-300' : full ? 'text-emerald-300' : 'text-gray-300'
                      }`}
                    >
                      {d.snapshots}
                      {expected > 0 && <span className="text-gray-600"> / {expected}</span>}
                    </td>
                    <td className="px-3 py-2">
                      <div className="h-1.5 w-24 overflow-hidden rounded bg-white/10">
                        <div
                          className={thin ? 'h-full bg-amber-500' : 'h-full bg-emerald-500'}
                          style={{
                            width: `${Math.min(
                              100,
                              expected > 0 ? (d.snapshots / expected) * 100 : 100,
                            )}%`,
                          }}
                        />
                      </div>
                    </td>
                    <td className="px-3 py-2 text-gray-300">{d.rows.toLocaleString()}</td>
                    <td className="whitespace-nowrap px-3 py-2 text-gray-400">
                      {fmtTime(d.first_ts)} – {fmtTime(d.last_ts)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function fmtTime(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
