// Shared sweep result rendering: the heat-scaled results table/heatmap plus
// the small formatting helpers it needs. Used by both the Backtests page
// (results of the sweep you just ran) and the sweep detail page (results of a
// sweep pulled back out of the DB) — the two must look identical, so they
// render through this one component rather than two copies.
import type { LeverValue, SweepResultItem, SweepSkippedItem } from '../../lib/api'

export const SWEEP_METRICS = [
  'total_return_pct',
  'sharpe',
  'max_drawdown_pct',
  'win_rate_pct',
] as const
export type SweepMetric = (typeof SWEEP_METRICS)[number]

// Bar sizes in duration order — timeframe.bar is a sweepable lever, and its
// values must sort by duration, not lexicographically ("5m" before "1h").
export const BAR_VALUES = ['1m', '5m', '15m', '30m', '1h', '1d']

/** Order a heatmap axis: numeric levers numerically, bar strings by duration,
 * anything else alphabetically. */
export function compareLeverValues(x: LeverValue, y: LeverValue): number {
  if (typeof x === 'number' && typeof y === 'number') return x - y
  const xi = BAR_VALUES.indexOf(String(x))
  const yi = BAR_VALUES.indexOf(String(y))
  if (xi >= 0 && yi >= 0) return xi - yi
  return String(x).localeCompare(String(y))
}

// green (good) <-> red (bad) heat scale, normalized within this sweep's
// own min/max for the selected metric — a simple HTML-table "heatmap"
// per the brief (no charting library needed for this).
export function heatColor(
  value: number | null,
  min: number,
  max: number,
  metric: SweepMetric,
): string {
  if (value == null || !Number.isFinite(value) || max === min) return 'transparent'
  let t = (value - min) / (max - min)
  if (metric === 'max_drawdown_pct') t = 1 - t // less negative/smaller drawdown = better
  const hue = 4 + t * (168 - 4) // red (4) -> teal (168)
  return `hsla(${hue}, 65%, 45%, 0.35)`
}

export function fmtNum(v: number | null | undefined): string {
  if (v == null) return '—'
  return v.toFixed(2)
}

export function pctColor(v: number | null | undefined): string {
  if (v == null) return 'text-gray-300'
  return v >= 0 ? 'text-teal-400' : 'text-red-400'
}

export default function SweepResults({
  data,
  metric,
  navigate,
}: {
  data: { results: SweepResultItem[]; skipped: SweepSkippedItem[] }
  metric: SweepMetric
  navigate: (path: string) => void
}) {
  const { results, skipped } = data
  if (results.length === 0) {
    return (
      <p className="mt-3 text-sm text-gray-500">
        No combination ran successfully — see skipped combos below.
      </p>
    )
  }
  const leverNames = Object.keys(results[0].combo)
  const values = results.map((r) => r.metrics_summary[metric]).filter((v): v is number => v != null)
  const min = values.length ? Math.min(...values) : 0
  const max = values.length ? Math.max(...values) : 0

  return (
    <div className="mt-4">
      <h3 className="mb-2 text-sm font-semibold text-gray-200">
        Results ({results.length} run{results.length === 1 ? '' : 's'})
      </h3>

      {leverNames.length <= 1 ? (
        <div className="overflow-x-auto rounded border border-white/10">
          <table className="w-full text-left text-sm">
            <thead className="bg-white/5 text-gray-400">
              <tr>
                <th className="px-3 py-2 font-medium">{leverNames[0] ?? 'combo'}</th>
                <th className="px-3 py-2 font-medium">{metric}</th>
              </tr>
            </thead>
            <tbody>
              {[...results]
                .sort((x, y) => compareLeverValues(x.combo[leverNames[0]], y.combo[leverNames[0]]))
                .map((r) => (
                  <tr
                    key={r.backtest_id}
                    onClick={() => navigate(`/backtests/${r.backtest_id}`)}
                    className="cursor-pointer border-t border-white/10 hover:bg-white/5"
                    style={{
                      backgroundColor: heatColor(r.metrics_summary[metric], min, max, metric),
                    }}
                  >
                    <td className="px-3 py-2 text-gray-200">{r.combo[leverNames[0]]}</td>
                    <td className="px-3 py-2 text-gray-200">{fmtNum(r.metrics_summary[metric])}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      ) : (
        <SweepHeatmapGrid
          results={results}
          leverNames={leverNames}
          metric={metric}
          min={min}
          max={max}
          navigate={navigate}
        />
      )}

      {skipped.length > 0 && (
        <div className="mt-3">
          <h3 className="mb-1 text-xs font-semibold text-gray-400">
            Skipped ({skipped.length}) — failed lint/validation
          </h3>
          <ul className="space-y-1 text-xs text-gray-500">
            {skipped.map((s, i) => (
              <li key={i}>
                {JSON.stringify(s.combo)} — {s.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function SweepHeatmapGrid({
  results,
  leverNames,
  metric,
  min,
  max,
  navigate,
}: {
  results: SweepResultItem[]
  leverNames: string[]
  metric: SweepMetric
  min: number
  max: number
  navigate: (path: string) => void
}) {
  const [a, b] = leverNames
  const aValues = Array.from(new Set(results.map((r) => r.combo[a]))).sort(compareLeverValues)
  const bValues = Array.from(new Set(results.map((r) => r.combo[b]))).sort(compareLeverValues)
  const byKey = new Map(results.map((r) => [`${r.combo[a]}|${r.combo[b]}`, r]))

  return (
    <div className="overflow-x-auto rounded border border-white/10">
      <table className="w-full text-left text-sm">
        <thead className="bg-white/5 text-gray-400">
          <tr>
            <th className="px-3 py-2 font-medium">
              {a} \ {b}
            </th>
            {bValues.map((bv) => (
              <th key={bv} className="px-3 py-2 font-medium">
                {bv}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {aValues.map((av) => (
            <tr key={av} className="border-t border-white/10">
              <td className="px-3 py-2 font-medium text-gray-400">{av}</td>
              {bValues.map((bv) => {
                const r = byKey.get(`${av}|${bv}`)
                const v = r?.metrics_summary[metric] ?? null
                return (
                  <td
                    key={bv}
                    onClick={() => r && navigate(`/backtests/${r.backtest_id}`)}
                    className={
                      r
                        ? 'cursor-pointer px-3 py-2 text-gray-200 hover:opacity-80'
                        : 'px-3 py-2 text-gray-600'
                    }
                    style={{ backgroundColor: heatColor(v, min, max, metric) }}
                  >
                    {fmtNum(v)}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
