// Optimize study detail — live progress, diagnostics, leaderboard, holdout
// verdict (docs/plan/14-optimizer.md §11).
//
// Structured so the honest numbers come FIRST and the exciting ones second:
// the holdout verdict and the capital-sensitivity verdict both sit above the
// leaderboard, and the leaderboard is sorted by stability-adjusted score with
// the raw score shown beside it.
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  cancelStudy,
  getStudy,
  getTrials,
  promoteChampion,
  type PromoteMode,
  type Trial,
} from '../lib/api'
import CollapsiblePanel from '../components/editor/CollapsiblePanel'
import {
  CapitalCard,
  ImportanceBars,
  ParallelCoords,
  SensitivitySlice,
  TrialScatter,
} from '../components/optimize/charts'
import {
  HoldoutCard,
  PlanPreview,
  btnCls,
  dimLabel,
  fmt,
  fmtDuration,
  statusChip,
} from '../components/optimize/shared'

export default function OptimizeStudy() {
  const { studyId } = useParams<{ studyId: string }>()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [showRejected, setShowRejected] = useState(false)
  const [showSaveNew, setShowSaveNew] = useState(false)
  const [newName, setNewName] = useState('')
  const [promoteMsg, setPromoteMsg] = useState<string | null>(null)

  const studyQuery = useQuery({
    queryKey: ['study', studyId],
    queryFn: () => getStudy(studyId as string),
    enabled: !!studyId,
    retry: false,
    // poll only while the study is actually running
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 1500 : false),
  })
  const study = studyQuery.data

  const trialsQuery = useQuery({
    queryKey: ['study-trials', studyId],
    queryFn: () => getTrials(studyId as string),
    enabled: !!studyId,
    refetchInterval: study?.status === 'running' ? 2500 : false,
  })

  const cancelMutation = useMutation({
    mutationFn: () => cancelStudy(studyId as string),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['study', studyId] }),
  })

  // Every promote mode also runs the champion as a real backtest, so the
  // useful destination afterwards is always that backtest's page — the user
  // asked to land there rather than be told an id they then have to hunt for.
  // The saved-strategy note is stashed first so it can be shown on arrival.
  const promoteMutation = useMutation({
    mutationFn: ({ mode, name }: { mode: PromoteMode; name?: string }) =>
      promoteChampion(studyId as string, mode, name),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['strategies'] })
      const note = data.new_slug
        ? `Saved as a new strategy "${data.new_slug}".`
        : data.saved_version
          ? `Saved as version ${data.saved_version} of ${study?.slug}.`
          : undefined
      navigate(`/backtests/${data.backtest.id}`, {
        state: { fromStudy: studyId, note },
      })
    },
    onError: (e: unknown) => setPromoteMsg(e instanceof Error ? e.message : String(e)),
  })

  if (studyQuery.isLoading) return <p className="text-sm text-gray-500">Loading study…</p>
  if (studyQuery.isError || !study)
    return <p className="text-sm text-red-400">Could not load this study.</p>

  const trials: Trial[] = trialsQuery.data?.trials ?? []
  const scored = trials.filter((t) => t.status === 'done')
  const rejected = trials.filter((t) => t.status !== 'done')
  const p = study.progress
  const pct = p ? Math.min(100, (p.attempted / Math.max(1, p.total)) * 100) : 100

  // Stability-adjusted ordering: the champion is the best PLATEAU, not the
  // single luckiest draw. Raw score stays visible alongside so the difference
  // is inspectable rather than hidden.
  const leaderboard = scored
    .slice()
    .sort((a, b) => (b.stability ?? b.score ?? 0) - (a.stability ?? a.score ?? 0))
    .slice(0, 25)

  const rejectionReasons = rejected.reduce<Record<string, number>>((acc, t) => {
    const key = (t.reject_reason ?? 'unknown').split('(')[0].replace(/[\d.]+/g, 'N').trim()
    acc[key] = (acc[key] ?? 0) + 1
    return acc
  }, {})

  const capital = study.diagnostics?.capital_sensitivity ?? null

  const gap =
    study.champion?.is_mean != null && study.champion?.oos_mean != null
      ? study.champion.is_mean - study.champion.oos_mean
      : null

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-gray-100">Study</h1>
        <span className={statusChip(study.status)}>{study.status}</span>
        <Link
          to={`/strategies/${study.slug}`}
          className="rounded-full border border-teal-400/40 bg-teal-400/10 px-3 py-1 text-xs text-teal-300 hover:bg-teal-400/20"
        >
          {study.slug} ↗
        </Link>
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 font-mono text-xs text-gray-400">
          {study.study_id.slice(0, 8)}
        </span>
        <span className="text-xs text-gray-500">
          {study.sampler} · seed {study.seed} · {study.objective.metric} (λ={study.objective.lambda})
        </span>
        <Link to="/optimize" className="text-xs text-gray-500 hover:text-teal-400">
          ← all studies
        </Link>
      </div>

      {study.error && (
        <p className="rounded border border-red-400/40 bg-red-400/5 p-2 text-sm text-red-300">
          {study.error}
        </p>
      )}
      {study.status === 'interrupted' && (
        <p className="rounded border border-amber-400/40 bg-amber-400/5 p-2 text-xs text-amber-200">
          This study was interrupted by a server restart. Completed trials are kept and shown below;
          it is not resumed automatically, because the code or the strategy may have changed since.
        </p>
      )}

      {/* progress */}
      {p && (
        <div className="rounded border border-white/10 bg-white/[0.03] p-3">
          <div className="mb-2 flex flex-wrap items-center gap-4 text-xs text-gray-400">
            <span className="text-sm text-gray-200">
              {p.attempted}/{p.total} trials
            </span>
            <span>{p.done} scored</span>
            <span className="text-amber-300/80">{p.rejected} rejected</span>
            {p.errored > 0 && <span className="text-red-300/80">{p.errored} errored</span>}
            <span>elapsed {fmtDuration(p.elapsed_sec)}</span>
            {study.status === 'running' && p.eta_sec != null && (
              <span>eta {fmtDuration(p.eta_sec)}</span>
            )}
            <span>phase: {p.phase}</span>
            {study.status === 'running' && (
              <button
                onClick={() => cancelMutation.mutate()}
                className="rounded border border-amber-400/40 bg-amber-400/10 px-2 py-0.5 text-amber-300 hover:bg-amber-400/20"
              >
                Cancel
              </button>
            )}
          </div>
          <div className="h-2 w-full overflow-hidden rounded bg-white/5">
            <div className="h-2 bg-teal-400/50" style={{ width: `${pct}%` }} />
          </div>
        </div>
      )}

      {/* the honest numbers, first. Both of these are always-visible on a
          finished study on purpose: the holdout answers "did this survive data
          the search never saw", and the capital ladder answers "is this a
          strategy or an artifact of the number I typed into initial_capital".
          Either one can invalidate everything below, so neither is behind a
          collapsible panel. */}
      {(study.status === 'done' || study.status === 'cancelled') && <HoldoutCard study={study} />}

      {(study.status === 'done' || study.status === 'cancelled') &&
        study.champion &&
        (capital ? (
          <CapitalCard sens={capital} />
        ) : (
          <div className="rounded border border-white/10 bg-white/[0.03] p-3 text-sm text-gray-400">
            No capital-sensitivity check for this study — it predates the check, or the champion
            could not be re-run. Without it, the returns above are only known to hold at $
            {Number(study.config.initial_capital ?? 0).toLocaleString()}.
          </div>
        ))}

      <PlanPreview plan={study.plan} />

      {/* champion */}
      {study.champion && (
        <div className="rounded border border-white/10 bg-white/[0.03] p-3">
          <h2 className="mb-2 text-sm font-semibold text-gray-200">Champion</h2>
          <div className="mb-2 flex flex-wrap gap-x-6 gap-y-1 text-xs">
            {Object.entries(study.champion.params).map(([path, v]) => (
              <span key={path} className="text-gray-300">
                <span className="text-gray-500">
                  {dimLabel(study.space.find((d) => d.path === path) ?? { path, type: 'float' })}
                </span>{' '}
                = <span className="font-mono">{String(v)}</span>
              </span>
            ))}
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-400">
            <span>
              stability-adjusted <span className="font-mono text-gray-200">{fmt(study.champion.stability)}</span>
            </span>
            <span>
              raw score <span className="font-mono text-gray-200">{fmt(study.champion.score)}</span>
            </span>
            <span>
              OOS mean <span className="font-mono text-gray-200">{fmt(study.champion.oos_mean)}</span> ± {fmt(study.champion.oos_std)}
            </span>
            {gap != null && (
              <span>
                in-sample gap <span className="font-mono text-gray-200">{fmt(gap)}</span>
              </span>
            )}
            <span>{study.champion.metrics?.trade_count ?? '—'} trades across folds</span>
          </div>
          {study.diagnostics?.champion_is_best_raw === false && (
            <p className="mt-2 text-[11px] leading-relaxed text-gray-500">
              A different trial scored higher on raw score ({fmt(study.diagnostics.best_raw_score)}),
              but sat on a narrower peak. This champion was chosen because its neighbours score well
              too — the edge survives being slightly wrong about the parameters, which you always
              are.
            </p>
          )}
          {gap != null && gap > 0 && study.champion.oos_mean != null && (
            <p className="mt-1 text-[11px] text-gray-500">
              In-sample scored {fmt(gap)} higher than out-of-sample. Some gap is normal; a large one
              means the parameters are describing the training window rather than the market.
            </p>
          )}

          {(study.status === 'done' || study.status === 'cancelled') && (
            <div className="mt-3 space-y-2">
              <div className="flex flex-wrap items-center gap-3">
                <button
                  onClick={() => promoteMutation.mutate({ mode: 'backtest_only' })}
                  disabled={promoteMutation.isPending}
                  className={btnCls}
                >
                  Run champion as a backtest
                </button>
                <button
                  onClick={() => promoteMutation.mutate({ mode: 'new_version' })}
                  disabled={promoteMutation.isPending}
                  className={btnCls}
                >
                  Save as a new version of {study.slug}
                </button>
                <button
                  onClick={() => setShowSaveNew((v) => !v)}
                  disabled={promoteMutation.isPending}
                  className={btnCls}
                >
                  Save as a new strategy…
                </button>
                {promoteMutation.isPending && (
                  <span className="text-xs text-gray-500">
                    Running the champion over the full history…
                  </span>
                )}
              </div>

              {showSaveNew && (
                <div className="flex flex-wrap items-center gap-2 rounded border border-white/10 bg-white/[0.03] p-2">
                  <input
                    autoFocus
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    placeholder={`${study.slug} (optimized)`}
                    className="w-72 rounded border border-white/10 bg-white/5 px-2 py-1 text-sm text-gray-200 focus:border-teal-400/50 focus:outline-none"
                  />
                  <button
                    onClick={() =>
                      promoteMutation.mutate({
                        mode: 'new_strategy',
                        name: newName.trim() || undefined,
                      })
                    }
                    disabled={promoteMutation.isPending}
                    className={btnCls}
                  >
                    Create strategy
                  </button>
                  <span className="text-[11px] text-gray-500">
                    Creates a separate strategy at version 1. {study.slug} is left completely
                    untouched — no new version, no edits.
                  </span>
                </div>
              )}

              <p className="text-[11px] text-gray-500">
                Every option also re-runs the champion over the full history as an ordinary
                backtest and takes you to it. Nothing here touches live or paper trading.
              </p>
            </div>
          )}
          {promoteMsg && <p className="mt-2 text-xs text-teal-300">{promoteMsg}</p>}
        </div>
      )}

      {/* charts */}
      <CollapsiblePanel title="Search progress (in-sample vs out-of-sample)" defaultOpen>
        <TrialScatter trials={trials} metric={study.objective.metric} />
      </CollapsiblePanel>

      <CollapsiblePanel title="Sensitivity around the champion — plateau or spike?" defaultOpen>
        <SensitivitySlice trials={trials} space={study.space} champion={study.champion} />
      </CollapsiblePanel>

      <CollapsiblePanel title="Parameter space (parallel coordinates)" defaultOpen={false}>
        <ParallelCoords trials={trials} space={study.space} />
      </CollapsiblePanel>

      {study.diagnostics?.importance && (
        <CollapsiblePanel title="Which parameters mattered" defaultOpen={false}>
          <ImportanceBars importance={study.diagnostics.importance} space={study.space} />
        </CollapsiblePanel>
      )}

      {/* leaderboard */}
      <div>
        <h2 className="mb-1 text-sm font-semibold text-gray-200">
          Leaderboard{' '}
          <span className="font-normal text-xs text-gray-500">
            — sorted by stability-adjusted score, not raw score
          </span>
        </h2>
        {leaderboard.length === 0 ? (
          <p className="text-sm text-gray-500">
            No trial has passed the constraints yet.
            {rejected.length > 0 && ' See the rejection breakdown below — the search space may be centered somewhere this strategy barely trades.'}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500">
                  <th className="py-1 pr-3 font-normal">#</th>
                  {study.space.map((d) => (
                    <th key={d.path} className="py-1 pr-3 font-normal">
                      {dimLabel(d)}
                    </th>
                  ))}
                  <th className="py-1 pr-3 font-normal">stability</th>
                  <th className="py-1 pr-3 font-normal">score</th>
                  <th className="py-1 pr-3 font-normal">OOS ± spread</th>
                  <th className="py-1 pr-3 font-normal">in-sample</th>
                  <th className="py-1 pr-3 font-normal">trades</th>
                  <th className="py-1 pr-3 font-normal">return %</th>
                  <th className="py-1 pr-3 font-normal">max DD %</th>
                </tr>
              </thead>
              <tbody>
                {leaderboard.map((t) => (
                  <tr
                    key={t.id}
                    className={`border-t border-white/5 ${
                      t.id === study.champion?.id ? 'bg-teal-400/5' : ''
                    }`}
                  >
                    <td className="py-1.5 pr-3 text-gray-500">{t.trial_number}</td>
                    {study.space.map((d) => (
                      <td key={d.path} className="py-1.5 pr-3 font-mono text-xs text-gray-300">
                        {String(t.params[d.path] ?? '—')}
                      </td>
                    ))}
                    <td className="py-1.5 pr-3 font-mono text-teal-300">{fmt(t.stability)}</td>
                    <td className="py-1.5 pr-3 font-mono text-gray-300">{fmt(t.score)}</td>
                    <td className="py-1.5 pr-3 font-mono text-xs text-gray-400">
                      {fmt(t.oos_mean)} ± {fmt(t.oos_std)}
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-xs text-gray-500">
                      {fmt(t.is_mean)}
                    </td>
                    <td className="py-1.5 pr-3 text-xs text-gray-400">
                      {t.metrics?.trade_count ?? '—'}
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-xs text-gray-400">
                      {fmt(t.metrics?.total_return_pct)}
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-xs text-gray-400">
                      {fmt(t.metrics?.max_drawdown_pct)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* rejections — diagnostic, not noise */}
      {rejected.length > 0 && (
        <div>
          <button
            onClick={() => setShowRejected((v) => !v)}
            className="mb-1 text-sm font-semibold text-gray-200 hover:text-teal-400"
          >
            {showRejected ? '▾' : '▸'} Rejected trials ({rejected.length})
          </button>
          <div className="mb-2 flex flex-wrap gap-2">
            {Object.entries(rejectionReasons)
              .sort((a, b) => b[1] - a[1])
              .map(([reason, n]) => (
                <span
                  key={reason}
                  className="rounded border border-amber-400/20 bg-amber-400/5 px-2 py-0.5 text-[11px] text-amber-200/80"
                >
                  {n}× {reason}
                </span>
              ))}
          </div>
          <p className="mb-2 max-w-3xl text-[11px] leading-relaxed text-gray-500">
            Rejected trials are kept on purpose. If most rejections are "too few trades", the search
            space is centered somewhere this strategy barely trades — widen the entry conditions or
            move the ranges rather than lowering the constraint.
          </p>
          {showRejected && (
            <div className="max-h-80 overflow-auto rounded border border-white/5">
              <table className="w-full text-xs">
                <tbody>
                  {rejected.map((t) => (
                    <tr key={t.id} className="border-t border-white/5">
                      <td className="py-1 pl-2 pr-3 text-gray-600">{t.trial_number}</td>
                      <td className="py-1 pr-3 font-mono text-gray-400">
                        {Object.entries(t.params)
                          .map(([k, v]) => `${k.split('.').slice(-2).join('.')}=${v}`)
                          .join('  ')}
                      </td>
                      <td className="py-1 pr-2 text-amber-200/70">{t.reject_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      <p className="max-w-3xl pb-4 text-[11px] leading-relaxed text-gray-600">
        A study searched {study.counts.n} parameter sets over one instrument's stored history. That
        is evidence, not proof: the more combinations searched, the more the best score is inflated
        by luck, which is exactly why the champion is chosen for stability and reported against a
        holdout it never saw. Nothing on this page can start a live run — the promotion gate remains
        the only path to real money.
      </p>
    </div>
  )
}
