import { useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  ApiError,
  getBacktests,
  getStrategies,
  getStrategy,
  postBacktest,
  postSweep,
  type BacktestConfig,
  type BacktestListItem,
  type LeverValue,
  type SweepGrid,
} from '../lib/api'
import SweepResults, {
  BAR_VALUES,
  SWEEP_METRICS,
  fmtNum,
  pctColor,
  type SweepMetric,
} from '../components/sweep/SweepResults'
import { inputCls } from '../components/editor/fields'
import CollapsiblePanel from '../components/editor/CollapsiblePanel'
import DocsHelpLink from '../components/DocsHelpLink'

// Lever values are numbers for most paths but bar strings ("5m", "1h") for
// timeframe.bar, so keep any token that isn't a clean number as a string
// rather than dropping it. The backend re-validates every combo against the
// strategy schema, so a typo'd bar lands in the sweep's `skipped` list with a
// reason instead of silently vanishing here.
function parseValues(raw: string): (number | string)[] {
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
    .map((s) => (s !== '' && !Number.isNaN(Number(s)) ? Number(s) : s))
}

// Levers vary indicator/exit/sizing/risk/order params plus timeframe.bar —
// not universe (the sweep backend rejects it outright, see
// backend/app/backtest/sweep.py's _FORBIDDEN_PATH_PREFIXES) or meta (name/id/
// version — not a tunable parameter). Excluded here too so the suggestions
// only ever show paths that'll actually be accepted. `timeframe` is walked
// separately below since its sweepable leaf is a string, not a number.
const LEVER_EXCLUDED_PREFIXES = new Set(['universe', 'timeframe', 'meta'])

interface LeverCandidate {
  path: string
  value: number | string
}

// Walk a strategy document and collect every numeric leaf's dotted path —
// exactly the path shape `set_by_path` in the sweep backend expects (list
// indices as plain ints), so anything found here is a valid lever path.
function collectLeverCandidates(doc: unknown): LeverCandidate[] {
  const out: LeverCandidate[] = []
  function walk(node: unknown, path: string[]) {
    if (path.length > 0 && LEVER_EXCLUDED_PREFIXES.has(path[0])) return
    if (typeof node === 'number' && Number.isFinite(node)) {
      out.push({ path: path.join('.'), value: node })
    } else if (Array.isArray(node)) {
      node.forEach((v, i) => walk(v, [...path, String(i)]))
    } else if (node && typeof node === 'object') {
      for (const [k, v] of Object.entries(node)) walk(v, [...path, k])
    }
  }
  walk(doc, [])
  const bar = (doc as { timeframe?: { bar?: string } })?.timeframe?.bar
  if (bar) out.unshift({ path: 'timeframe.bar', value: bar })
  return out
}

// A quick +/-30% spread around the strategy's current value, so picking a
// path from the dropdown gives you a reasonable starting grid instead of an
// empty values box.
function suggestValues(current: number | string): string {
  // timeframe.bar isn't numeric — suggest the current bar plus its immediate
  // neighbours rather than a +/-30% spread.
  if (typeof current === 'string') {
    const i = BAR_VALUES.indexOf(current)
    if (i < 0) return current
    return BAR_VALUES.slice(Math.max(0, i - 1), i + 2).join(', ')
  }
  const isInt = Number.isInteger(current)
  const round = (n: number) => (isInt ? Math.round(n) : Math.round(n * 10) / 10)
  const vals = Array.from(new Set([round(current * 0.7), current, round(current * 1.3)]))
  return vals.sort((a, b) => a - b).join(', ')
}

function dateStr(d: Date): string {
  const year = d.getFullYear()
  const month = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function todayStr(): string {
  return dateStr(new Date())
}

function oneWeekAgoStr(): string {
  const d = new Date()
  d.setDate(d.getDate() - 7)
  return dateStr(d)
}

export default function Backtests() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const presetSlug = searchParams.get('slug') ?? ''

  const [slug, setSlug] = useState(presetSlug)
  const [start, setStart] = useState(oneWeekAgoStr())
  const [end, setEnd] = useState(todayStr())
  const [initialCapital, setInitialCapital] = useState(100000)
  const [slippageBps, setSlippageBps] = useState(1)
  const [commission, setCommission] = useState(0)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  // ---- parameter sweep mode (feature 3) -----------------------------------
  const [mode, setMode] = useState<'single' | 'sweep'>('single')
  const [lever1Path, setLever1Path] = useState('')
  const [lever1Values, setLever1Values] = useState('')
  const [lever2Path, setLever2Path] = useState('')
  const [lever2Values, setLever2Values] = useState('')
  const [heatMetric, setHeatMetric] = useState<SweepMetric>('total_return_pct')

  const strategiesQuery = useQuery({
    queryKey: ['strategies'],
    queryFn: () => getStrategies(),
    retry: false,
  })

  // Only needed in sweep mode, to drive the lever-path autocomplete + JSON
  // viewer below — the single-run form doesn't need the full document.
  const strategyDocQuery = useQuery({
    queryKey: ['strategy-doc', slug],
    queryFn: () => getStrategy(slug),
    enabled: mode === 'sweep' && slug.length > 0,
    retry: false,
  })

  const leverCandidates = useMemo(
    () => (strategyDocQuery.data ? collectLeverCandidates(strategyDocQuery.data.document) : []),
    [strategyDocQuery.data],
  )

  function fillSuggestedValues(path: string, currentValues: string, setValues: (v: string) => void) {
    if (currentValues.trim()) return
    const match = leverCandidates.find((c) => c.path === path.trim())
    if (match) setValues(suggestValues(match.value))
  }

  const listQuery = useQuery({
    queryKey: ['backtests'],
    queryFn: getBacktests,
    retry: false,
  })

  // Sweeps collapse to one row; expansion is per-sweep and starts closed.
  const [expandedSweeps, setExpandedSweeps] = useState<Set<string>>(new Set())
  const runRows = useMemo(
    () => groupRuns(listQuery.data?.backtests ?? []),
    [listQuery.data?.backtests],
  )

  function toggleSweep(sweepId: string) {
    setExpandedSweeps((prev) => {
      const next = new Set(prev)
      if (next.has(sweepId)) next.delete(sweepId)
      else next.add(sweepId)
      return next
    })
  }

  const runMutation = useMutation({
    mutationFn: () => {
      const config: BacktestConfig = {
        start,
        end,
        initial_capital: initialCapital,
        slippage: { type: 'fixed_bps', value: slippageBps },
        commission: { per_order: commission },
        fill_on_touch: false, // conservative default per plan 05 (touch ≠ fill)
      }
      return postBacktest(slug, config)
    },
    onSuccess: (r) => {
      navigate(`/backtests/${r.id}`)
    },
  })

  const sweepMutation = useMutation({
    mutationFn: () => {
      const config: BacktestConfig = {
        start,
        end,
        initial_capital: initialCapital,
        slippage: { type: 'fixed_bps', value: slippageBps },
        commission: { per_order: commission },
        fill_on_touch: false,
      }
      const grid: SweepGrid = {}
      const v1 = parseValues(lever1Values)
      if (lever1Path.trim() && v1.length > 0) grid.lever_a = { path: lever1Path.trim(), values: v1 }
      const v2 = parseValues(lever2Values)
      if (lever2Path.trim() && v2.length > 0) grid.lever_b = { path: lever2Path.trim(), values: v2 }
      return postSweep(slug, config, grid)
    },
  })

  const strategies = strategiesQuery.data?.strategies ?? []

  function runBacktest() {
    if (slug.length === 0 || runMutation.isPending) return
    runMutation.mutate()
  }

  function runSweep() {
    if (slug.length === 0 || sweepMutation.isPending) return
    if (!lever1Path.trim() || parseValues(lever1Values).length === 0) return
    sweepMutation.mutate()
  }

  function toggleSelected(id: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else if (next.size < 4) {
        next.add(id)
      }
      return next
    })
  }

  function compareSelected() {
    if (selected.size < 2 || selected.size > 4) return
    navigate(`/backtests/compare?ids=${Array.from(selected).join(',')}`)
  }

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-gray-100">Backtests</h1>
        <DocsHelpLink />
      </div>

      <div className="mb-4 flex gap-1 rounded border border-white/10 p-1 w-fit">
        <button
          onClick={() => setMode('single')}
          className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
            mode === 'single' ? 'bg-emerald-600 text-white' : 'text-gray-400 hover:bg-white/5'
          }`}
        >
          Single run
        </button>
        <button
          onClick={() => setMode('sweep')}
          className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
            mode === 'sweep' ? 'bg-emerald-600 text-white' : 'text-gray-400 hover:bg-white/5'
          }`}
        >
          Sweep
        </button>
      </div>

      {mode === 'single' && (
      <section className="mb-6 rounded border border-white/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">Run a backtest</h2>
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
                  {s.name}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Start
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              className={inputCls('w-36')}
            />
          </label>

          <label className="flex flex-col gap-1 text-xs text-gray-400">
            End
            <input
              type="date"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              className={inputCls('w-36')}
            />
          </label>

          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Initial capital
            <input
              type="number"
              value={initialCapital}
              onChange={(e) => setInitialCapital(Number(e.target.value))}
              className={inputCls('w-32')}
            />
          </label>

          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Slippage (bps)
            <input
              type="number"
              value={slippageBps}
              onChange={(e) => setSlippageBps(Number(e.target.value))}
              className={inputCls('w-24')}
            />
          </label>

          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Commission ($/order)
            <input
              type="number"
              value={commission}
              onChange={(e) => setCommission(Number(e.target.value))}
              className={inputCls('w-28')}
            />
          </label>

          <button
            onClick={runBacktest}
            disabled={slug.length === 0 || runMutation.isPending}
            className="rounded bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {runMutation.isPending ? 'Running…' : 'Run'}
          </button>

          {runMutation.isPending && (
            <span className="text-xs text-gray-500">
              This can take up to a minute — hang tight…
            </span>
          )}
        </div>

        {runMutation.isError && (
          <p className="mt-3 text-sm text-red-400">
            {runMutation.error instanceof ApiError
              ? runMutation.error.status === 424
                ? 'Connect Schwab in Settings first.'
                : runMutation.error.message
              : 'Failed to run backtest.'}
          </p>
        )}
      </section>
      )}

      {mode === 'sweep' && (
        <section className="mb-6 rounded border border-white/10 p-4">
          <h2 className="mb-3 text-sm font-semibold text-gray-200">Parameter sweep</h2>
          <p className="mb-3 text-xs text-gray-500">
            Vary 1-2 levers by dotted path into the strategy document (e.g.{' '}
            <code className="text-gray-400">indicators.0.params.period</code> or{' '}
            <code className="text-gray-400">exit.stop_loss.value</code>), comma-separated values.
            Pick a strategy below to get path autocomplete and see its raw JSON. To sweep bar
            sizes, use <code className="text-gray-400">timeframe.bar</code> with values like{' '}
            <code className="text-gray-400">5m, 15m, 1h</code> — each bar size is fetched and
            warmed up separately.
          </p>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Strategy
              <select value={slug} onChange={(e) => setSlug(e.target.value)} className={inputCls('w-56')}>
                <option value="">Select a strategy…</option>
                {strategies.map((s) => (
                  <option key={s.slug} value={s.slug}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Start
              <input type="date" value={start} onChange={(e) => setStart(e.target.value)} className={inputCls('w-36')} />
            </label>
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              End
              <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className={inputCls('w-36')} />
            </label>

            {/* Same config the single-run form edits (shared state, and
                sweepMutation already sends these) — without the inputs here a
                sweep silently used whatever the single-run form was last set
                to, or the defaults. Every combo in a sweep runs against the
                same config; only the levers vary. */}
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Initial capital
              <input
                type="number"
                value={initialCapital}
                onChange={(e) => setInitialCapital(Number(e.target.value))}
                className={inputCls('w-32')}
              />
            </label>

            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Slippage (bps)
              <input
                type="number"
                value={slippageBps}
                onChange={(e) => setSlippageBps(Number(e.target.value))}
                className={inputCls('w-24')}
              />
            </label>

            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Commission ($/order)
              <input
                type="number"
                value={commission}
                onChange={(e) => setCommission(Number(e.target.value))}
                className={inputCls('w-28')}
              />
            </label>
          </div>

          {slug.length > 0 && (
            <div className="mt-3">
              <CollapsiblePanel title="Strategy JSON (for lever paths)" defaultOpen={false}>
                {strategyDocQuery.isLoading && (
                  <p className="text-xs text-gray-500">Loading…</p>
                )}
                {strategyDocQuery.data && (
                  <pre className="max-h-80 overflow-auto rounded bg-white/5 p-3 text-xs text-gray-400">
                    {JSON.stringify(strategyDocQuery.data.document, null, 2)}
                  </pre>
                )}
              </CollapsiblePanel>
            </div>
          )}

          <datalist id="lever-path-options">
            {leverCandidates.map((c) => (
              <option key={c.path} value={c.path} label={`= ${c.value}`} />
            ))}
          </datalist>

          <div className="mt-3 flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Lever A path
              <input
                value={lever1Path}
                onChange={(e) => setLever1Path(e.target.value)}
                onBlur={() => fillSuggestedValues(lever1Path, lever1Values, setLever1Values)}
                list="lever-path-options"
                placeholder="indicators.0.params.period"
                className={inputCls('w-64')}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Lever A values
              <input
                value={lever1Values}
                onChange={(e) => setLever1Values(e.target.value)}
                placeholder="10, 14, 20"
                className={inputCls('w-40')}
              />
            </label>
          </div>

          <div className="mt-3 flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Lever B path (optional)
              <input
                value={lever2Path}
                onChange={(e) => setLever2Path(e.target.value)}
                onBlur={() => fillSuggestedValues(lever2Path, lever2Values, setLever2Values)}
                list="lever-path-options"
                placeholder="exit.stop_loss.value"
                className={inputCls('w-64')}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Lever B values
              <input
                value={lever2Values}
                onChange={(e) => setLever2Values(e.target.value)}
                placeholder="1.5, 2.0, 3.0"
                className={inputCls('w-40')}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Heatmap metric
              <select
                value={heatMetric}
                onChange={(e) => setHeatMetric(e.target.value as SweepMetric)}
                className={inputCls('w-40')}
              >
                {SWEEP_METRICS.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
            <button
              onClick={runSweep}
              disabled={slug.length === 0 || sweepMutation.isPending}
              className="rounded bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
            >
              {sweepMutation.isPending ? 'Running sweep…' : 'Run sweep'}
            </button>
            {sweepMutation.isPending && (
              <span className="text-xs text-gray-500">
                Running every combination — this can take a while…
              </span>
            )}
          </div>

          {sweepMutation.isError && (
            <p className="mt-3 text-sm text-red-400">
              {sweepMutation.error instanceof ApiError
                ? sweepMutation.error.message
                : 'Failed to run sweep.'}
            </p>
          )}

          {sweepMutation.data && (
            <SweepResults data={sweepMutation.data} metric={heatMetric} navigate={navigate} />
          )}
        </section>
      )}

      <section className="rounded border border-white/10 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-200">Runs</h2>
          <button
            onClick={compareSelected}
            disabled={selected.size < 2 || selected.size > 4}
            className="rounded bg-white/10 px-3 py-1 text-xs font-medium text-gray-200 hover:bg-white/20 disabled:opacity-40"
          >
            Compare selected ({selected.size})
          </button>
        </div>

        {listQuery.isLoading && <p className="text-sm text-gray-500">Loading…</p>}
        {listQuery.isError && <p className="text-sm text-red-400">Could not load backtests.</p>}

        {listQuery.data && listQuery.data.backtests.length === 0 && (
          <p className="text-sm text-gray-600">No backtests yet.</p>
        )}

        {listQuery.data && listQuery.data.backtests.length > 0 && (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium"></th>
                  <th className="px-3 py-2 font-medium">Strategy</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Started</th>
                  <th className="px-3 py-2 font-medium">Return %</th>
                  <th className="px-3 py-2 font-medium">Sharpe</th>
                  <th className="px-3 py-2 font-medium">Max DD %</th>
                  <th className="px-3 py-2 font-medium">Win rate %</th>
                  <th className="px-3 py-2 font-medium">Trades</th>
                </tr>
              </thead>
              <tbody>
                {runRows.map((row) =>
                  row.kind === 'single' ? (
                    <RunTr
                      key={row.run.id}
                      bt={row.run}
                      navigate={navigate}
                      selected={selected}
                      toggleSelected={toggleSelected}
                    />
                  ) : (
                    <SweepRows
                      key={row.sweepId}
                      sweepId={row.sweepId}
                      runs={row.runs}
                      expanded={expandedSweeps.has(row.sweepId)}
                      onToggle={() => toggleSweep(row.sweepId)}
                      navigate={navigate}
                      selected={selected}
                      toggleSelected={toggleSelected}
                    />
                  ),
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}

/** One row of the runs table: either a standalone backtest, or a whole sweep
 * collapsed behind a disclosure arrow. A sweep of 40 combos would otherwise
 * bury every other run in the list. */
type RunRow =
  | { kind: 'single'; run: BacktestListItem }
  | { kind: 'sweep'; sweepId: string; runs: BacktestListItem[] }

/** Group consecutive-or-not sweep members under their sweep, preserving the
 * list's existing newest-first order by each group's newest member. */
function groupRuns(runs: BacktestListItem[]): RunRow[] {
  const out: RunRow[] = []
  const sweepRowIndex = new Map<string, number>()
  for (const run of runs) {
    if (!run.sweep_id) {
      out.push({ kind: 'single', run })
      continue
    }
    const at = sweepRowIndex.get(run.sweep_id)
    if (at === undefined) {
      sweepRowIndex.set(run.sweep_id, out.length)
      out.push({ kind: 'sweep', sweepId: run.sweep_id, runs: [run] })
    } else {
      ;(out[at] as { runs: BacktestListItem[] }).runs.push(run)
    }
  }
  return out
}

/** The sweep's best member by total return — what the collapsed row reports,
 * since a sweep has no single result of its own. Null-safe: runs that errored
 * or are still running have no summary. */
function bestOfSweep(runs: BacktestListItem[]): BacktestListItem | null {
  let best: BacktestListItem | null = null
  for (const r of runs) {
    const v = r.summary?.total_return_pct
    if (v == null) continue
    if (best == null || v > (best.summary?.total_return_pct ?? -Infinity)) best = r
  }
  return best
}

function comboLabel(combo: Record<string, LeverValue> | null): string {
  if (!combo) return '—'
  return Object.entries(combo)
    .map(([k, v]) => `${k}=${v}`)
    .join(', ')
}

interface RowSelection {
  selected: Set<string>
  toggleSelected: (id: string) => void
  navigate: (path: string) => void
}

/** A single backtest row. `indent` shifts sweep members under their parent. */
function RunTr({
  bt,
  navigate,
  selected,
  toggleSelected,
  label,
  indent = false,
}: RowSelection & { bt: BacktestListItem; label?: string; indent?: boolean }) {
  return (
    <tr
      onClick={() => navigate(`/backtests/${bt.id}`)}
      className={`cursor-pointer border-t border-white/10 hover:bg-white/5 ${
        indent ? 'bg-white/[0.02]' : ''
      }`}
    >
      {/* sweep members indent both the checkbox and the label, so the whole
          row reads as nested under its sweep rather than just its text. */}
      <td className={`px-3 py-2 ${indent ? 'pl-10' : ''}`} onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          checked={selected.has(bt.id)}
          onChange={() => toggleSelected(bt.id)}
          disabled={!selected.has(bt.id) && selected.size >= 4}
        />
      </td>
      <td className="px-3 py-2 text-gray-300">
        {label ?? bt.strategy_name ?? '—'}
      </td>
      <td className="px-3 py-2 text-gray-300">{bt.status}</td>
      <td className="px-3 py-2 text-gray-300">{new Date(bt.started_at).toLocaleString()}</td>
      <td className={`px-3 py-2 ${pctColor(bt.summary?.total_return_pct)}`}>
        {fmtNum(bt.summary?.total_return_pct)}
      </td>
      <td className="px-3 py-2 text-gray-300">{fmtNum(bt.summary?.sharpe)}</td>
      <td className="px-3 py-2 text-gray-300">{fmtNum(bt.summary?.max_drawdown_pct)}</td>
      <td className="px-3 py-2 text-gray-300">{fmtNum(bt.summary?.win_rate_pct)}</td>
      <td className="px-3 py-2 text-gray-300">{bt.summary?.trade_count ?? '—'}</td>
    </tr>
  )
}

/** A sweep collapsed to one row: the arrow expands its member runs beneath.
 * The collapsed row shows the sweep's BEST member by return (a sweep has no
 * result of its own), so the table stays scannable. */
function SweepRows({
  sweepId,
  runs,
  expanded,
  onToggle,
  navigate,
  selected,
  toggleSelected,
}: RowSelection & {
  sweepId: string
  runs: BacktestListItem[]
  expanded: boolean
  onToggle: () => void
}) {
  const best = bestOfSweep(runs)
  const name = runs[0]?.strategy_name ?? '—'
  const started = runs.reduce((min, r) => (r.started_at < min ? r.started_at : min), runs[0].started_at)
  const done = runs.filter((r) => r.status === 'done').length

  return (
    <>
      <tr onClick={onToggle} className="cursor-pointer border-t border-white/10 hover:bg-white/5">
        <td className="px-3 py-2 text-gray-500">
          <span className="inline-block w-3">{expanded ? '▾' : '▸'}</span>
        </td>
        <td className="px-3 py-2 text-gray-300">
          {name}{' '}
          <span
            onClick={(e) => {
              e.stopPropagation()
              navigate(`/backtests/sweep/${sweepId}`)
            }}
            title="Open this sweep's grid + heatmap"
            className="ml-1 rounded-full border border-teal-400/40 bg-teal-400/10 px-2 py-0.5 text-xs text-teal-300 hover:bg-teal-400/20"
          >
            sweep · {runs.length} run{runs.length === 1 ? '' : 's'} ↗
          </span>
        </td>
        <td className="px-3 py-2 text-gray-400">
          {done === runs.length ? 'done' : `${done}/${runs.length} done`}
        </td>
        <td className="px-3 py-2 text-gray-300">{new Date(started).toLocaleString()}</td>
        <td className={`px-3 py-2 ${pctColor(best?.summary?.total_return_pct)}`}>
          {fmtNum(best?.summary?.total_return_pct)}
          <span className="ml-1 text-xs text-gray-500">best</span>
        </td>
        <td className="px-3 py-2 text-gray-300">{fmtNum(best?.summary?.sharpe)}</td>
        <td className="px-3 py-2 text-gray-300">{fmtNum(best?.summary?.max_drawdown_pct)}</td>
        <td className="px-3 py-2 text-gray-300">{fmtNum(best?.summary?.win_rate_pct)}</td>
        <td className="px-3 py-2 text-gray-300">{best?.summary?.trade_count ?? '—'}</td>
      </tr>
      {expanded &&
        runs.map((r) => (
          <RunTr
            key={r.id}
            bt={r}
            navigate={navigate}
            selected={selected}
            toggleSelected={toggleSelected}
            label={comboLabel(r.sweep_combo)}
            indent
          />
        ))}
    </>
  )
}

