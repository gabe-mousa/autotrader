// Optimize — the study launcher and study list (docs/plan/14-optimizer.md §11).
//
// A NEW page, per the user's requirement: the Backtests page and its sweep UI
// are untouched. A sweep is still the right tool for eyeballing a 2-lever
// heatmap; this is for "search N parameters jointly and tell me honestly
// whether the answer holds up".
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import {
  ApiError,
  createStudy,
  deleteStudy,
  getOptimizeMeta,
  getStrategies,
  getStudies,
  previewPlan,
  suggestSpace,
  type OptimizePlan,
  type SpaceDim,
} from '../lib/api'
import CollapsiblePanel from '../components/editor/CollapsiblePanel'
import { PlanPreview, btnCls, fmt, inputCls, statusChip } from '../components/optimize/shared'

interface ConstraintsForm {
  min_trades_per_fold: number
  min_trades_total: number
  max_drawdown_pct: number
  max_losing_streak: number
  require_all_folds_traded: boolean
}

const DEFAULT_CONSTRAINTS: ConstraintsForm = {
  min_trades_per_fold: 30,
  min_trades_total: 150,
  max_drawdown_pct: 40,
  max_losing_streak: 15,
  require_all_folds_traded: true,
}

export default function Optimize() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [slug, setSlug] = useState('')
  const [space, setSpace] = useState<SpaceDim[]>([])
  const [metric, setMetric] = useState('sharpe')
  const [lambda, setLambda] = useState(0.5)
  const [constraints, setConstraints] = useState<ConstraintsForm>(DEFAULT_CONSTRAINTS)
  const [nTrials, setNTrials] = useState(150)
  const [folds, setFolds] = useState<number | ''>('')
  const [mode, setMode] = useState('rolling')
  const [sampler, setSampler] = useState('tpe')
  const [seed, setSeed] = useState(0)
  const [capital, setCapital] = useState(100_000)
  const [plan, setPlan] = useState<OptimizePlan | null>(null)
  const [estimated, setEstimated] = useState<number | null>(null)
  const [planError, setPlanError] = useState<string | null>(null)
  const [planDetail, setPlanDetail] = useState<Record<string, unknown> | null>(null)
  const [launchError, setLaunchError] = useState<string | null>(null)

  const strategiesQuery = useQuery({ queryKey: ['strategies'], queryFn: () => getStrategies() })
  const metaQuery = useQuery({ queryKey: ['optimize-meta'], queryFn: getOptimizeMeta })
  const studiesQuery = useQuery({
    queryKey: ['studies'],
    queryFn: getStudies,
    // cheap, and keeps a running study's progress bar moving on this page too
    refetchInterval: 4000,
  })

  // Picking a strategy prefills an EDITABLE search space derived from its own
  // parameters — nobody should have to hand-type `indicators.0.params.period`.
  const suggestMutation = useMutation({
    mutationFn: (s: string) => suggestSpace(s),
    onSuccess: (data) => setSpace(data.space),
  })
  useEffect(() => {
    if (slug) suggestMutation.mutate(slug)
    setPlan(null)
    setPlanError(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug])

  const body = () => ({
    slug,
    space: space.map((d) => ({
      path: d.path,
      type: d.type,
      ...(d.type === 'categorical'
        ? { choices: d.choices }
        : { low: Number(d.low), high: Number(d.high), log: !!d.log, step: d.step ?? undefined }),
    })),
    objective: { metric, lambda, constraints },
    n_trials: nTrials,
    folds: folds === '' ? undefined : Number(folds),
    mode,
    sampler,
    seed,
    config: { initial_capital: capital },
  })

  const planMutation = useMutation({
    mutationFn: () => previewPlan({ slug, n_trials: nTrials, folds: folds === '' ? undefined : Number(folds), mode }),
    onSuccess: (data) => {
      setPlan(data.plan)
      setEstimated(data.estimated_backtests)
      setPlanError(null)
      setPlanDetail(null)
    },
    onError: (e: unknown) => {
      setPlan(null)
      setPlanError(e instanceof Error ? e.message : String(e))
      const d = e instanceof ApiError ? e.detail : null
      setPlanDetail(d && typeof d === 'object' ? (d as Record<string, unknown>) : null)
    },
  })

  const createMutation = useMutation({
    mutationFn: () => createStudy(body()),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['studies'] })
      navigate(`/optimize/${data.study_id}`)
    },
    onError: (e: unknown) => setLaunchError(e instanceof Error ? e.message : String(e)),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteStudy(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['studies'] }),
  })

  const updateDim = (i: number, patch: Partial<SpaceDim>) =>
    setSpace((prev) => prev.map((d, j) => (j === i ? { ...d, ...patch } : d)))

  const strategies = strategiesQuery.data?.strategies ?? []
  const maxDims = metaQuery.data?.max_dims ?? 8
  const canLaunch = !!slug && space.length > 0 && !!plan && !createMutation.isPending

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-gray-100">Optimize</h1>
        <p className="mt-1 max-w-3xl text-xs leading-relaxed text-gray-500">
          Search a strategy's parameters over many backtests, evaluated walk-forward with a
          held-out tail the search never sees. This is not a leaderboard for the highest return —
          the best score in a large search is usually the luckiest draw, so the champion is picked
          for stability and reported against a holdout.{' '}
          <Link to="/backtests" className="text-gray-400 hover:text-teal-400">
            Simple 2-lever sweeps still live on the Backtests page.
          </Link>
        </p>
      </div>

      <CollapsiblePanel title="New study" defaultOpen>
        <div className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-xs text-gray-400">Strategy</span>
              <select value={slug} onChange={(e) => setSlug(e.target.value)} className={inputCls}>
                <option value="">select…</option>
                {strategies.map((s) => (
                  <option key={s.slug} value={s.slug}>
                    {s.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-gray-400">Trials</span>
              <input
                type="number"
                min={1}
                max={metaQuery.data?.max_trials ?? 2000}
                value={nTrials}
                onChange={(e) => setNTrials(Number(e.target.value))}
                className={`${inputCls} w-24`}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-gray-400">Sampler</span>
              <select
                value={sampler}
                onChange={(e) => setSampler(e.target.value)}
                className={inputCls}
              >
                {(metaQuery.data?.samplers ?? ['tpe', 'random']).map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-gray-400">Seed</span>
              <input
                type="number"
                value={seed}
                onChange={(e) => setSeed(Number(e.target.value))}
                className={`${inputCls} w-20`}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-gray-400">Folds</span>
              <input
                type="number"
                min={1}
                max={10}
                placeholder="auto"
                value={folds}
                onChange={(e) => setFolds(e.target.value === '' ? '' : Number(e.target.value))}
                className={`${inputCls} w-20`}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-gray-400">Fold mode</span>
              <select value={mode} onChange={(e) => setMode(e.target.value)} className={inputCls}>
                <option value="rolling">rolling</option>
                <option value="anchored">anchored</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-gray-400">Capital</span>
              <input
                type="number"
                value={capital}
                onChange={(e) => setCapital(Number(e.target.value))}
                className={`${inputCls} w-28`}
              />
            </label>
          </div>
          <p className="text-[11px] text-gray-500">
            Capital is held fixed across a study on purpose: percent-of-equity sizing is close to
            scale-invariant, so searching over it burns compute on a flat surface. The seed makes a
            study exactly reproducible.
          </p>

          {/* -- search space -- */}
          <div>
            <div className="mb-1 flex items-baseline gap-3">
              <h3 className="text-sm font-semibold text-gray-200">Search space</h3>
              <span className="text-xs text-gray-500">
                {space.length}/{maxDims} dimensions
                {suggestMutation.isPending && ' · loading suggestions…'}
              </span>
            </div>
            {space.length === 0 ? (
              <p className="text-xs text-gray-500">
                {slug ? 'No tunable parameters found on this strategy.' : 'Pick a strategy first.'}
              </p>
            ) : (
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-gray-500">
                    <th className="py-1 pr-2 font-normal">parameter</th>
                    <th className="py-1 pr-2 font-normal">current</th>
                    <th className="py-1 pr-2 font-normal">type</th>
                    <th className="py-1 pr-2 font-normal">low</th>
                    <th className="py-1 pr-2 font-normal">high</th>
                    <th className="py-1 pr-2 font-normal">log</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {space.map((d, i) => (
                    <tr key={d.path} className="border-t border-white/5">
                      <td className="py-1 pr-2">
                        <span className="text-gray-300">{d.label ?? d.path}</span>
                        <div className="font-mono text-[10px] text-gray-600">{d.path}</div>
                      </td>
                      <td className="py-1 pr-2 font-mono text-gray-500">{fmt(d.current)}</td>
                      <td className="py-1 pr-2 text-gray-400">{d.type}</td>
                      <td className="py-1 pr-2">
                        {d.type === 'categorical' ? (
                          <span className="text-gray-500">{(d.choices ?? []).join(', ')}</span>
                        ) : (
                          <input
                            type="number"
                            value={d.low ?? 0}
                            onChange={(e) => updateDim(i, { low: Number(e.target.value) })}
                            className={`${inputCls} w-24`}
                          />
                        )}
                      </td>
                      <td className="py-1 pr-2">
                        {d.type !== 'categorical' && (
                          <input
                            type="number"
                            value={d.high ?? 0}
                            onChange={(e) => updateDim(i, { high: Number(e.target.value) })}
                            className={`${inputCls} w-24`}
                          />
                        )}
                      </td>
                      <td className="py-1 pr-2">
                        {d.type === 'float' && (
                          <input
                            type="checkbox"
                            checked={!!d.log}
                            onChange={(e) => updateDim(i, { log: e.target.checked })}
                          />
                        )}
                      </td>
                      <td className="py-1 text-right">
                        <button
                          onClick={() => setSpace((p) => p.filter((_, j) => j !== i))}
                          className="text-gray-600 hover:text-red-400"
                          title="remove this dimension"
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* -- objective -- */}
          <div>
            <h3 className="mb-1 text-sm font-semibold text-gray-200">Objective</h3>
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-gray-400">Metric</span>
                <select
                  value={metric}
                  onChange={(e) => setMetric(e.target.value)}
                  className={inputCls}
                >
                  {(metaQuery.data?.metrics ?? ['sharpe']).map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-gray-400">Consistency penalty (λ)</span>
                <input
                  type="number"
                  step={0.1}
                  min={0}
                  max={5}
                  value={lambda}
                  onChange={(e) => setLambda(Number(e.target.value))}
                  className={`${inputCls} w-24`}
                />
              </label>
            </div>
            <p className="mt-1 max-w-3xl text-[11px] leading-relaxed text-gray-500">
              Score = mean(out-of-sample {metric}) − λ × spread across folds. With λ &gt; 0 a
              parameter set that works in every fold beats one that works spectacularly in a single
              fold — which is what you want from something about to run forward into a regime it
              hasn't seen.
            </p>
          </div>

          {/* -- constraints -- */}
          <div>
            <h3 className="mb-1 text-sm font-semibold text-gray-200">
              Constraints{' '}
              <span className="font-normal text-gray-500">
                — trials violating any of these are rejected, not scored
              </span>
            </h3>
            <div className="flex flex-wrap items-end gap-3">
              {(
                [
                  ['min_trades_per_fold', 'Min trades / fold'],
                  ['min_trades_total', 'Min trades total'],
                  ['max_drawdown_pct', 'Max drawdown %'],
                  ['max_losing_streak', 'Max losing streak'],
                ] as const
              ).map(([key, label]) => (
                <label key={key} className="flex flex-col gap-1">
                  <span className="text-xs text-gray-400">{label}</span>
                  <input
                    type="number"
                    value={constraints[key]}
                    onChange={(e) =>
                      setConstraints({ ...constraints, [key]: Number(e.target.value) })
                    }
                    className={`${inputCls} w-28`}
                  />
                </label>
              ))}
              <label className="flex items-center gap-2 pb-1 text-xs text-gray-400">
                <input
                  type="checkbox"
                  checked={constraints.require_all_folds_traded}
                  onChange={(e) =>
                    setConstraints({ ...constraints, require_all_folds_traded: e.target.checked })
                  }
                />
                every fold must trade
              </label>
            </div>
            {/* Anchor the trade floor to the actual fold length. Without this
                the user is picking "30" against an unknown denominator. */}
            {plan && plan.folds.length > 0 && (
              <p className="mt-1 rounded border border-white/10 bg-white/[0.03] px-2 py-1.5 text-[11px] text-gray-400">
                Each fold is scored over{' '}
                <span className="text-gray-200">
                  ~{Math.round(
                    plan.folds.reduce((a, f) => a + f.test_days, 0) / plan.folds.length,
                  )}{' '}
                  trading days
                </span>{' '}
                ({Math.round(plan.folds.reduce((a, f) => a + f.test_bars, 0) / plan.folds.length)}{' '}
                {plan.bar} bars). At your current floor of {constraints.min_trades_per_fold}, a
                parameter set must trade at least once every{' '}
                {(
                  plan.folds.reduce((a, f) => a + f.test_days, 0) /
                  plan.folds.length /
                  Math.max(1, constraints.min_trades_per_fold)
                ).toFixed(1)}{' '}
                trading days to be scored at all.
              </p>
            )}
            <p className="mt-1 max-w-3xl text-[11px] leading-relaxed text-gray-500">
              The minimum trade count is the most important of these. Four trades that happen to win
              produce a Sharpe of 5 and mean nothing; without this floor those parameter sets top
              every leaderboard. Set it against the fold length above — not against the total
              history.
            </p>
          </div>

          {/* -- plan preview + launch -- */}
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-3">
              <button
                onClick={() => planMutation.mutate()}
                disabled={!slug || planMutation.isPending}
                className={btnCls}
              >
                {planMutation.isPending ? 'Checking data…' : 'Preview data plan'}
              </button>
              <button
                onClick={() => {
                  setLaunchError(null)
                  createMutation.mutate()
                }}
                disabled={!canLaunch}
                className={btnCls}
                title={plan ? '' : 'Preview the data plan first'}
              >
                {createMutation.isPending ? 'Starting…' : 'Run study'}
              </button>
              {!plan && (
                <span className="text-xs text-gray-500">
                  Preview the data plan first — it shows exactly which history will be used and what
                  is held back.
                </span>
              )}
            </div>

            {planError && (
              <div className="rounded border border-amber-400/40 bg-amber-400/5 p-3 text-xs text-amber-200">
                <p className="font-semibold">Not enough stored history for an honest study</p>
                <p className="mt-1 leading-relaxed">{planError}</p>
                {/* Per-symbol coverage. Without this a universe capped by ONE
                    recently-listed ticker looks like a data-provider limit
                    rather than a one-edit fix. */}
                {planDetail?.per_symbol != null && (
                  <table className="mt-2 text-[11px] text-amber-200/70">
                    <tbody>
                      {Object.entries(
                        planDetail.per_symbol as Record<
                          string,
                          { bars: number; start: number | null; end: number | null }
                        >,
                      )
                        .sort((a, b) => a[1].bars - b[1].bars)
                        .map(([sym, cov], i) => (
                          <tr key={sym}>
                            <td className="pr-3 font-mono">{sym}</td>
                            <td className="pr-3">{cov.bars} bars</td>
                            <td className="pr-3">
                              {cov.start ? new Date(cov.start).toISOString().slice(0, 10) : '—'} →{' '}
                              {cov.end ? new Date(cov.end).toISOString().slice(0, 10) : '—'}
                            </td>
                            <td>{i === 0 && cov.bars > 0 ? '← limits the whole study' : ''}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                )}
                <p className="mt-2 text-amber-200/70">
                  Either drop the short-history symbol from this strategy's universe, or backfill
                  more history on the{' '}
                  <Link to="/data" className="underline">
                    Data page
                  </Link>
                  . A study never backfills on its own — it would fire a request per trial.
                </p>
              </div>
            )}
            {launchError && (
              <p className="rounded border border-red-400/40 bg-red-400/5 p-2 text-xs text-red-300">
                {launchError}
              </p>
            )}
            {plan && <PlanPreview plan={plan} estimatedBacktests={estimated ?? undefined} />}
          </div>
        </div>
      </CollapsiblePanel>

      {/* -- past studies -- */}
      <div>
        <h2 className="mb-2 text-sm font-semibold text-gray-200">Studies</h2>
        {studiesQuery.isLoading ? (
          <p className="text-sm text-gray-500">Loading…</p>
        ) : (studiesQuery.data?.studies ?? []).length === 0 ? (
          <p className="text-sm text-gray-500">No studies yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500">
                <th className="py-1 pr-3 font-normal">strategy</th>
                <th className="py-1 pr-3 font-normal">status</th>
                <th className="py-1 pr-3 font-normal">metric</th>
                <th className="py-1 pr-3 font-normal">trials</th>
                <th className="py-1 pr-3 font-normal">bar / folds</th>
                <th className="py-1 pr-3 font-normal">champion (OOS)</th>
                <th className="py-1 pr-3 font-normal">holdout</th>
                <th className="py-1 pr-3 font-normal">started</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(studiesQuery.data?.studies ?? []).map((s) => (
                <tr key={s.id} className="border-t border-white/5 hover:bg-white/[0.02]">
                  <td className="py-1.5 pr-3">
                    <Link to={`/optimize/${s.id}`} className="text-teal-400 hover:underline">
                      {s.slug}
                    </Link>
                  </td>
                  <td className="py-1.5 pr-3">
                    <span className={statusChip(s.status)}>{s.status}</span>
                  </td>
                  <td className="py-1.5 pr-3 text-gray-400">{s.objective?.metric}</td>
                  <td className="py-1.5 pr-3 text-gray-400">
                    {s.progress ? `${s.progress.attempted}/${s.n_trials}` : s.n_trials}
                  </td>
                  <td className="py-1.5 pr-3 text-gray-400">
                    {s.bar} / {s.folds}
                  </td>
                  <td className="py-1.5 pr-3 font-mono text-gray-300">
                    {fmt(s.diagnostics?.champion_score)}
                  </td>
                  <td className="py-1.5 pr-3 font-mono text-gray-300">
                    {fmt(s.holdout?.score)}
                  </td>
                  <td className="py-1.5 pr-3 text-xs text-gray-500">
                    {new Date(s.created_at).toLocaleString()}
                  </td>
                  <td className="py-1.5 text-right">
                    <button
                      onClick={() => deleteMutation.mutate(s.id)}
                      className="text-xs text-gray-600 hover:text-red-400"
                    >
                      delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
