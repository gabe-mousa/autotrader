// Small shared pieces for the two Optimize pages (docs/plan/14-optimizer.md).
// Kept separate from components/sweep/* on purpose: a sweep heatmap and a
// study leaderboard answer different questions and shouldn't be forced to
// share a rendering path.
import type { OptimizePlan, SpaceDim, StudyDetail } from '../../lib/api'

export function fmt(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return v.toFixed(digits)
}

export function fmtDate(ms: number | null | undefined): string {
  if (!ms) return '—'
  return new Date(ms).toISOString().slice(0, 10)
}

export function fmtDuration(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec)) return '—'
  if (sec < 60) return `${Math.round(sec)}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`
}

/** The last two path segments — what distinguishes indicators.0.params.period
 * from indicators.1.params.period without printing the whole dotted path. */
export function dimLabel(dim: SpaceDim): string {
  // The server resolves labels against the strategy document at study-creation
  // time (space.label_for) — "ema200.period" rather than a third identical
  // "params.period". Only fall back when a dimension predates that.
  if (dim.label) return dim.label
  const parts = dim.path.split('.')
  return parts.slice(-2).join('.')
}

export function statusChip(status: string): string {
  const map: Record<string, string> = {
    running: 'border-teal-400/40 bg-teal-400/10 text-teal-300',
    done: 'border-white/15 bg-white/5 text-gray-300',
    cancelled: 'border-amber-400/40 bg-amber-400/10 text-amber-300',
    interrupted: 'border-amber-400/40 bg-amber-400/10 text-amber-300',
    error: 'border-red-400/40 bg-red-400/10 text-red-300',
    rejected: 'border-amber-400/30 bg-amber-400/5 text-amber-300/80',
  }
  return `rounded-full border px-2 py-0.5 text-xs ${map[status] ?? map.done}`
}

export const inputCls =
  'rounded border border-white/10 bg-white/5 px-2 py-1 text-sm text-gray-200 ' +
  'focus:border-teal-400/50 focus:outline-none'

export const btnCls =
  'rounded border border-teal-400/40 bg-teal-400/10 px-3 py-1.5 text-sm text-teal-300 ' +
  'hover:bg-teal-400/20 disabled:cursor-not-allowed disabled:opacity-40'

/** The data-plan preview: what history exists, how it's split, what's held
 * back. Rendered BEFORE a study is launched (on the launcher) and again on
 * the study page, because "which data did this actually use" is the first
 * question to ask of any optimization result. */
export function PlanPreview({
  plan,
  estimatedBacktests,
}: {
  plan: OptimizePlan
  estimatedBacktests?: number
}) {
  const span = plan.search_end - plan.search_start || 1
  const holdoutPct = ((plan.holdout_end - plan.holdout_start) / (plan.coverage_end - plan.coverage_start)) * 100

  return (
    <div className="rounded border border-white/10 bg-white/[0.03] p-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs text-gray-400">
        <span className="text-sm font-semibold text-gray-200">Data plan</span>
        <span>
          {plan.symbols.join(', ')} · {plan.bar}
        </span>
        <span>
          {fmtDate(plan.coverage_start)} → {fmtDate(plan.coverage_end)} (~{plan.total_bars} bars
          stored)
        </span>
        <span>
          {plan.folds.length} walk-forward fold{plan.folds.length === 1 ? '' : 's'} ({plan.mode})
        </span>
        {estimatedBacktests != null && <span>≈{estimatedBacktests.toLocaleString()} backtests</span>}
      </div>

      {/* Visual split: each fold's out-of-sample leg, then the holdout. */}
      <div className="relative mb-1 h-6 w-full overflow-hidden rounded bg-white/5">
        {plan.folds.map((f) => (
          <div
            key={f.index}
            title={`Fold ${f.index + 1} out-of-sample: ${fmtDate(f.test_start)} → ${fmtDate(f.test_end)}`}
            className="absolute top-0 h-6 border-r border-[#1d1a17] bg-teal-400/25"
            style={{
              left: `${((f.test_start - plan.search_start) / (plan.holdout_end - plan.search_start)) * 100}%`,
              width: `${((f.test_end - f.test_start) / (plan.holdout_end - plan.search_start)) * 100}%`,
            }}
          />
        ))}
        <div
          title={`Holdout (never seen by the search): ${fmtDate(plan.holdout_start)} → ${fmtDate(plan.holdout_end)}`}
          className="absolute top-0 h-6 bg-amber-400/25"
          style={{
            left: `${((plan.holdout_start - plan.search_start) / (plan.holdout_end - plan.search_start)) * 100}%`,
            width: `${((plan.holdout_end - plan.holdout_start) / (plan.holdout_end - plan.search_start)) * 100}%`,
          }}
        />
      </div>
      <div className="flex flex-wrap gap-4 text-[11px] text-gray-500">
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-sm bg-teal-400/50" />
          out-of-sample folds — what the objective is computed from
        </span>
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-sm bg-amber-400/50" />
          holdout ({holdoutPct.toFixed(0)}%, {plan.holdout_bars} bars) — never seen by the search,
          scored once at the end
        </span>
        <span className="text-gray-600">search span {(span / 86_400_000).toFixed(0)}d</span>
      </div>

      {/* Per-fold sizing in TRADING DAYS. This is the table to read before
          setting "min trades / fold": a 30-trade floor is trivial over 300
          sessions and impossible over 12, and there's no way to eyeball that
          from a bar count. */}
      <details className="mt-3 group">
        <summary className="cursor-pointer text-xs text-gray-400 hover:text-teal-400">
          Fold sizes — how long each out-of-sample leg actually is
        </summary>
        <table className="mt-2 w-full text-[11px]">
          <thead>
            <tr className="text-left text-gray-500">
              <th className="py-1 pr-3 font-normal">fold</th>
              <th className="py-1 pr-3 font-normal">trains on</th>
              <th className="py-1 pr-3 font-normal">tested on (out-of-sample)</th>
              <th className="py-1 pr-3 font-normal">test bars</th>
              <th className="py-1 pr-3 font-normal">test trading days</th>
            </tr>
          </thead>
          <tbody>
            {plan.folds.map((f) => (
              <tr key={f.index} className="border-t border-white/5 text-gray-400">
                <td className="py-1 pr-3">{f.index + 1}</td>
                <td className="py-1 pr-3">
                  {f.train_days} days ({f.train_bars} bars)
                </td>
                <td className="py-1 pr-3">
                  {fmtDate(f.test_start)} → {fmtDate(f.test_end)}
                </td>
                <td className="py-1 pr-3 font-mono">{f.test_bars}</td>
                <td className="py-1 pr-3 font-mono text-gray-300">{f.test_days}</td>
              </tr>
            ))}
            <tr className="border-t border-white/5 text-amber-200/70">
              <td className="py-1 pr-3">holdout</td>
              <td className="py-1 pr-3">—</td>
              <td className="py-1 pr-3">
                {fmtDate(plan.holdout_start)} → {fmtDate(plan.holdout_end)}
              </td>
              <td className="py-1 pr-3 font-mono">{plan.holdout_bars}</td>
              <td className="py-1 pr-3 font-mono">{plan.holdout_days}</td>
            </tr>
          </tbody>
        </table>
        <p className="mt-2 max-w-3xl text-[11px] leading-relaxed text-gray-500">
          {plan.total_days} trading days of {plan.bar} data in total, ~
          {plan.bars_per_day.toFixed(1)} bars per session. Each fold is scored on the window in
          the third column — so "min trades / fold" is a floor over{' '}
          <span className="text-gray-300">
            ~{Math.round(plan.folds.reduce((a, f) => a + f.test_days, 0) / Math.max(1, plan.folds.length))}{' '}
            trading days
          </span>
          , not over the whole history. A strategy taking one trade a week would produce roughly{' '}
          {Math.round(
            plan.folds.reduce((a, f) => a + f.test_days, 0) / Math.max(1, plan.folds.length) / 5,
          )}{' '}
          trades per fold.
        </p>
      </details>

      {plan.warnings.length > 0 && (
        <ul className="mt-2 space-y-1">
          {plan.warnings.map((w) => (
            <li key={w} className="text-xs text-amber-300/90">
              ⚠ {w}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** The holdout verdict. Deliberately always rendered on a finished study, and
 * deliberately blunt: this is the number that decides whether the rest of the
 * page means anything, so it must not be something a user can scroll past. */
export function HoldoutCard({ study }: { study: StudyDetail }) {
  const h = study.holdout
  if (!h) {
    return (
      <div className="rounded border border-white/10 bg-white/[0.03] p-3 text-sm text-gray-400">
        No holdout score yet — it is computed once, on the champion, when the search finishes.
      </div>
    )
  }
  if (h.error) {
    return (
      <div className="rounded border border-red-400/30 bg-red-400/5 p-3 text-sm text-red-300">
        The holdout run failed: {h.error}
      </div>
    )
  }

  const oos = study.champion?.oos_mean ?? null
  const hold = h.score ?? null
  let verdict = 'The holdout could not be scored (the metric was undefined over that window).'
  let tone = 'border-white/10 bg-white/[0.03] text-gray-300'

  if (oos != null && hold != null) {
    const drop = oos === 0 ? 0 : ((oos - hold) / Math.abs(oos)) * 100
    if (hold <= 0 && oos > 0) {
      verdict =
        `In-study out-of-sample ${h.metric} was ${fmt(oos)}, but the holdout came out at ` +
        `${fmt(hold)}. This did not survive data the search never saw — treat it as overfit, ` +
        `not as a strategy.`
      tone = 'border-red-400/40 bg-red-400/5 text-red-200'
    } else if (drop > 50) {
      verdict =
        `The holdout ${h.metric} (${fmt(hold)}) is ${drop.toFixed(0)}% below the in-study ` +
        `out-of-sample score (${fmt(oos)}). Some of what the search found was fitted to its own ` +
        `window. Size any expectations off the holdout number, not the leaderboard.`
      tone = 'border-amber-400/40 bg-amber-400/5 text-amber-200'
    } else if (drop > 20) {
      verdict =
        `The holdout ${h.metric} (${fmt(hold)}) is somewhat below the in-study out-of-sample ` +
        `score (${fmt(oos)}) — a normal amount of shrinkage. The holdout is the honest estimate.`
      tone = 'border-white/15 bg-white/[0.03] text-gray-200'
    } else {
      verdict =
        `The holdout ${h.metric} (${fmt(hold)}) held up against the in-study out-of-sample score ` +
        `(${fmt(oos)}). That is the best evidence this study can produce — it is still one ` +
        `window of one instrument's history.`
      tone = 'border-teal-400/40 bg-teal-400/5 text-teal-200'
    }
  }

  return (
    <div className={`rounded border p-3 ${tone}`}>
      <div className="mb-1 flex flex-wrap items-baseline gap-3">
        <span className="text-sm font-semibold">Holdout verdict</span>
        <span className="font-mono text-lg">{fmt(hold)}</span>
        <span className="text-xs opacity-70">
          {h.metric} over {fmtDate(h.start)} → {fmtDate(h.end)} ({h.bars} bars)
        </span>
      </div>
      <p className="text-xs leading-relaxed opacity-90">{verdict}</p>
      <p className="mt-2 text-[11px] opacity-60">
        This window was never shown to the search. Re-running the study to improve this number
        would turn it into just another in-sample score — if you need a new read, backfill more
        history instead.
      </p>
    </div>
  )
}
