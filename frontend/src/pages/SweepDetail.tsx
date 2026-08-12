import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { getStrategy, getSweep } from '../lib/api'
import SweepResults, {
  SWEEP_METRICS,
  compareLeverValues,
  type SweepMetric,
} from '../components/sweep/SweepResults'
import CollapsiblePanel from '../components/editor/CollapsiblePanel'

/** A past sweep, pulled back out of the DB: which levers were swept over what
 * values, and the same heatmap the Backtests page shows right after a sweep
 * runs. Reached from the collapsed sweep row in the runs table — without this
 * there was no way to see a sweep's grid after leaving the page that ran it.
 *
 * GET /api/backtests/sweep/{id} has no `skipped` list (combos that failed
 * lint were never persisted as backtests), so nothing is passed for it. */
export default function SweepDetail() {
  const { sweepId } = useParams<{ sweepId: string }>()
  const navigate = useNavigate()
  const [metric, setMetric] = useState<SweepMetric>('total_return_pct')

  const { data, isLoading, isError } = useQuery({
    queryKey: ['sweep', sweepId],
    queryFn: () => getSweep(sweepId as string),
    enabled: !!sweepId,
    retry: false,
  })

  // The strategy document, for the lever-path reference panel below. This is
  // the CURRENT document on disk, not the frozen snapshot the sweep ran
  // against — sweep combos are never frozen, so no snapshot exists to show.
  const slug = data?.strategy_slug ?? ''
  const strategyDocQuery = useQuery({
    queryKey: ['strategy-doc', slug],
    queryFn: () => getStrategy(slug),
    enabled: slug.length > 0,
    retry: false,
  })

  if (isLoading) return <p className="text-sm text-gray-500">Loading sweep…</p>
  if (isError || !data) return <p className="text-sm text-red-400">Could not load sweep.</p>

  const levers = Object.entries(data.grid)

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-gray-100">Sweep</h1>
        {data.strategy_slug && (
          <Link
            to={`/strategies/${data.strategy_slug}`}
            title="Open this strategy in the editor"
            className="rounded-full border border-teal-400/40 bg-teal-400/10 px-3 py-1 text-xs text-teal-300 hover:bg-teal-400/20"
          >
            {data.strategy_name ?? data.strategy_slug} ↗
          </Link>
        )}
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 font-mono text-xs text-gray-400">
          {data.sweep_id.slice(0, 8)}
        </span>
        <Link to="/backtests" className="text-xs text-gray-500 hover:text-teal-400">
          ← back to runs
        </Link>
      </div>

      {strategyDocQuery.data && (
        <div className="mb-4">
          <CollapsiblePanel title="Strategy JSON (for lever paths)" defaultOpen={false}>
            <pre className="max-h-96 overflow-auto rounded bg-white/5 p-3 text-xs text-gray-400">
              {JSON.stringify(strategyDocQuery.data.document, null, 2)}
            </pre>
          </CollapsiblePanel>
          <p className="mt-1 text-xs text-gray-500">
            This is the strategy as it stands on disk now — it may have been edited since this
            sweep ran.
          </p>
        </div>
      )}

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold text-gray-200">Levers swept</h2>
        {levers.length === 0 ? (
          <p className="text-sm text-gray-600">No grid recorded for this sweep.</p>
        ) : (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Lever</th>
                  <th className="px-3 py-2 font-medium">Path</th>
                  <th className="px-3 py-2 font-medium">Values</th>
                </tr>
              </thead>
              <tbody>
                {levers.map(([name, spec]) => (
                  <tr key={name} className="border-t border-white/10">
                    <td className="px-3 py-2 text-gray-300">{name}</td>
                    <td className="px-3 py-2 font-mono text-xs text-gray-400">{spec.path}</td>
                    <td className="px-3 py-2 text-gray-200">
                      {[...spec.values].sort(compareLeverValues).join(', ')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="mb-2 flex items-center gap-2">
        <label className="text-xs text-gray-400">Heatmap metric</label>
        <select
          value={metric}
          onChange={(e) => setMetric(e.target.value as SweepMetric)}
          className="rounded border border-white/10 bg-transparent px-2 py-1 text-xs text-gray-300"
        >
          {SWEEP_METRICS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <span className="text-xs text-gray-500">click any cell to open that run</span>
      </div>

      <SweepResults
        data={{ results: data.results, skipped: [] }}
        metric={metric}
        navigate={navigate}
      />
    </div>
  )
}
