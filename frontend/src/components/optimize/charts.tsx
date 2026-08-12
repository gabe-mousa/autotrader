// Study charts, drawn as plain inline SVG.
//
// lightweight-charts (already a dependency) is a time-series library — it has
// no notion of a scatter over trial number, a parallel-coordinates plot, or a
// 1-D sensitivity profile, and every chart here is small and static. Hand-
// rolled SVG keeps them dependency-free and lets the IS-vs-OOS pair share one
// coordinate system, which is the whole point of that chart.
import { useMemo, useState } from 'react'
import type { CapitalSensitivity, CapitalVerdict, SpaceDim, Trial } from '../../lib/api'
import { dimLabel, fmt } from './shared'

const W = 720
const H = 220
const PAD = { l: 44, r: 12, t: 12, b: 28 }

function scale(v: number, lo: number, hi: number, a: number, b: number): number {
  if (!Number.isFinite(v) || hi === lo) return (a + b) / 2
  return a + ((v - lo) / (hi - lo)) * (b - a)
}

function extent(values: number[]): [number, number] {
  if (values.length === 0) return [0, 1]
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  if (lo === hi) return [lo - 1, hi + 1]
  const pad = (hi - lo) * 0.08
  return [lo - pad, hi + pad]
}

function Axes({ yLo, yHi, xLabel }: { yLo: number; yHi: number; xLabel: string }) {
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => yLo + t * (yHi - yLo))
  return (
    <>
      {ticks.map((v) => {
        const y = scale(v, yLo, yHi, H - PAD.b, PAD.t)
        return (
          <g key={v}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y} y2={y} stroke="rgba(255,255,255,0.06)" />
            <text x={PAD.l - 6} y={y + 3} textAnchor="end" fontSize="9" fill="#6b7280">
              {v.toFixed(2)}
            </text>
          </g>
        )
      })}
      <text x={(W + PAD.l) / 2} y={H - 4} textAnchor="middle" fontSize="9" fill="#6b7280">
        {xLabel}
      </text>
    </>
  )
}

/** Trial scatter: out-of-sample score vs in-sample score, per trial.
 *
 * The most important chart on the page while a study runs. If the in-sample
 * series climbs while out-of-sample stays flat or falls, the search is eating
 * noise — visible live, before you spend an hour on it. */
export function TrialScatter({ trials, metric }: { trials: Trial[]; metric: string }) {
  const scored = trials.filter((t) => t.status === 'done' && t.score != null)
  const rejected = trials.filter((t) => t.status !== 'done')

  const all = [
    ...scored.map((t) => t.oos_mean ?? 0),
    ...scored.map((t) => t.is_mean).filter((v): v is number => v != null),
  ]
  const [yLo, yHi] = extent(all)
  const xHi = Math.max(1, ...trials.map((t) => t.trial_number))

  if (scored.length === 0) {
    return <p className="text-xs text-gray-500">No scored trials yet.</p>
  }

  // running best of the out-of-sample series
  let best = -Infinity
  const bestPath = scored
    .slice()
    .sort((a, b) => a.trial_number - b.trial_number)
    .map((t) => {
      best = Math.max(best, t.oos_mean ?? -Infinity)
      const x = scale(t.trial_number, 0, xHi, PAD.l, W - PAD.r)
      const y = scale(best, yLo, yHi, H - PAD.b, PAD.t)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')

  return (
    <div className="overflow-x-auto">
      <svg width={W} height={H} className="min-w-[560px]">
        <Axes yLo={yLo} yHi={yHi} xLabel={`trial number — y axis is ${metric}`} />
        {rejected.map((t) => (
          <circle
            key={t.id}
            cx={scale(t.trial_number, 0, xHi, PAD.l, W - PAD.r)}
            cy={H - PAD.b - 3}
            r={2}
            fill="rgba(251,191,36,0.45)"
          >
            <title>{`trial ${t.trial_number}: ${t.reject_reason ?? t.status}`}</title>
          </circle>
        ))}
        {scored.map((t) =>
          t.is_mean == null ? null : (
            <circle
              key={`is-${t.id}`}
              cx={scale(t.trial_number, 0, xHi, PAD.l, W - PAD.r)}
              cy={scale(t.is_mean, yLo, yHi, H - PAD.b, PAD.t)}
              r={2.5}
              fill="rgba(148,163,184,0.5)"
            >
              <title>{`trial ${t.trial_number} in-sample: ${fmt(t.is_mean)}`}</title>
            </circle>
          ),
        )}
        {scored.map((t) => (
          <circle
            key={`oos-${t.id}`}
            cx={scale(t.trial_number, 0, xHi, PAD.l, W - PAD.r)}
            cy={scale(t.oos_mean ?? 0, yLo, yHi, H - PAD.b, PAD.t)}
            r={3}
            fill="rgba(45,212,191,0.8)"
          >
            <title>{`trial ${t.trial_number} out-of-sample: ${fmt(t.oos_mean)}`}</title>
          </circle>
        ))}
        <polyline points={bestPath} fill="none" stroke="rgba(45,212,191,0.35)" strokeWidth={1.5} />
      </svg>
      <div className="mt-1 flex flex-wrap gap-4 text-[11px] text-gray-500">
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-full bg-teal-400/80" />
          out-of-sample (what the objective uses)
        </span>
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-full bg-slate-400/60" />
          in-sample — if this pulls away from the teal points, the search is fitting noise
        </span>
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-full bg-amber-400/50" />
          rejected (hover for the reason)
        </span>
      </div>
    </div>
  )
}

/** Parallel coordinates over the search space, coloured by score. Shows which
 * regions the good trials share — the fastest way to see structure across
 * more than two dimensions at once. */
export function ParallelCoords({ trials, space }: { trials: Trial[]; space: SpaceDim[] }) {
  const scored = trials.filter((t) => t.status === 'done' && t.score != null)
  if (scored.length === 0 || space.length < 2) {
    return <p className="text-xs text-gray-500">Needs at least 2 dimensions and one scored trial.</p>
  }
  const scores = scored.map((t) => t.score as number)
  const [sLo, sHi] = [Math.min(...scores), Math.max(...scores)]

  const axisX = (i: number) => PAD.l + (i / (space.length - 1)) * (W - PAD.l - PAD.r)
  const numeric = (dim: SpaceDim, v: number | string): number =>
    dim.type === 'categorical' ? (dim.choices ?? []).findIndex((c) => c === v) : Number(v)

  const ranges = space.map((dim) => {
    if (dim.type === 'categorical') return [0, Math.max(1, (dim.choices?.length ?? 1) - 1)]
    return [Number(dim.low ?? 0), Number(dim.high ?? 1)]
  })

  return (
    <div className="overflow-x-auto">
      <svg width={W} height={H} className="min-w-[560px]">
        {space.map((dim, i) => (
          <g key={dim.path}>
            <line
              x1={axisX(i)}
              x2={axisX(i)}
              y1={PAD.t}
              y2={H - PAD.b}
              stroke="rgba(255,255,255,0.12)"
            />
            <text
              x={axisX(i)}
              y={H - PAD.b + 12}
              textAnchor="middle"
              fontSize="9"
              fill="#9ca3af"
            >
              {dimLabel(dim)}
            </text>
            <text x={axisX(i)} y={PAD.t - 3} textAnchor="middle" fontSize="8" fill="#6b7280">
              {dim.type === 'categorical' ? '' : fmt(ranges[i][1])}
            </text>
          </g>
        ))}
        {scored.map((t) => {
          const pts = space
            .map((dim, i) => {
              const v = numeric(dim, t.params[dim.path])
              const y = scale(v, ranges[i][0], ranges[i][1], H - PAD.b, PAD.t)
              return `${axisX(i).toFixed(1)},${y.toFixed(1)}`
            })
            .join(' ')
          const norm = sHi === sLo ? 1 : ((t.score as number) - sLo) / (sHi - sLo)
          return (
            <polyline
              key={t.id}
              points={pts}
              fill="none"
              stroke={`hsla(${4 + norm * 164}, 65%, 55%, ${0.15 + norm * 0.6})`}
              strokeWidth={norm > 0.9 ? 2 : 1}
            >
              <title>{`trial ${t.trial_number}: ${fmt(t.score)}`}</title>
            </polyline>
          )
        })}
      </svg>
      <p className="mt-1 text-[11px] text-gray-500">
        Each line is one trial. Brighter/greener = higher score.
      </p>
    </div>
  )
}

export function ImportanceBars({
  importance,
  space,
}: {
  importance: Record<string, number>
  space: SpaceDim[]
}) {
  const rows = space
    .map((d) => ({ dim: d, v: importance[d.path] ?? 0 }))
    .sort((a, b) => b.v - a.v)
  const max = Math.max(0.0001, ...rows.map((r) => r.v))
  return (
    <div className="space-y-1.5">
      {rows.map(({ dim, v }) => (
        <div key={dim.path} className="flex items-center gap-2">
          <span className="w-40 shrink-0 truncate text-xs text-gray-400" title={dim.path}>
            {dimLabel(dim)}
          </span>
          <div className="h-3 flex-1 overflow-hidden rounded bg-white/5">
            <div className="h-3 bg-teal-400/40" style={{ width: `${(v / max) * 100}%` }} />
          </div>
          <span className="w-12 shrink-0 text-right font-mono text-xs text-gray-500">
            {v.toFixed(3)}
          </span>
        </div>
      ))}
      <p className="pt-1 text-[11px] text-gray-500">
        Share of score variance explained by each parameter alone. Read these relatively — a
        near-zero bar means that knob barely mattered over the ranges searched.
      </p>
    </div>
  )
}

/** 1-D sensitivity profile around the champion.
 *
 * The most decision-relevant chart here: it answers "is this a plateau or a
 * spike?". Every scored trial is plotted against one parameter; if the good
 * scores form a broad hill, the edge survives being slightly wrong about that
 * parameter. If the champion is a lone dot above a field of bad ones, it is an
 * artifact of this particular price history. */
export function SensitivitySlice({
  trials,
  space,
  champion,
}: {
  trials: Trial[]
  space: SpaceDim[]
  champion: Trial | null
}) {
  const [sel, setSel] = useState(0)
  const dim = space[sel]
  const scored = useMemo(
    () => trials.filter((t) => t.status === 'done' && t.score != null),
    [trials],
  )
  if (!dim || scored.length === 0) {
    return <p className="text-xs text-gray-500">No scored trials yet.</p>
  }

  const numeric = (v: number | string): number =>
    dim.type === 'categorical' ? (dim.choices ?? []).findIndex((c) => c === v) : Number(v)
  const xLo = dim.type === 'categorical' ? 0 : Number(dim.low ?? 0)
  const xHi = dim.type === 'categorical' ? Math.max(1, (dim.choices?.length ?? 1) - 1) : Number(dim.high ?? 1)
  const [yLo, yHi] = extent(scored.map((t) => t.score as number))

  return (
    <div>
      <div className="mb-2 flex flex-wrap gap-1">
        {space.map((d, i) => (
          <button
            key={d.path}
            onClick={() => setSel(i)}
            className={`rounded px-2 py-0.5 text-xs ${
              i === sel ? 'bg-teal-400/20 text-teal-300' : 'bg-white/5 text-gray-400 hover:bg-white/10'
            }`}
          >
            {dimLabel(d)}
          </button>
        ))}
      </div>
      <div className="overflow-x-auto">
        <svg width={W} height={H} className="min-w-[560px]">
          <Axes yLo={yLo} yHi={yHi} xLabel={`${dimLabel(dim)} (${fmt(xLo)} → ${fmt(xHi)})`} />
          {scored.map((t) => (
            <circle
              key={t.id}
              cx={scale(numeric(t.params[dim.path]), xLo, xHi, PAD.l, W - PAD.r)}
              cy={scale(t.score as number, yLo, yHi, H - PAD.b, PAD.t)}
              r={2.5}
              fill="rgba(45,212,191,0.55)"
            >
              <title>{`${dimLabel(dim)}=${t.params[dim.path]} → ${fmt(t.score)}`}</title>
            </circle>
          ))}
          {champion && champion.params[dim.path] != null && (
            <circle
              cx={scale(numeric(champion.params[dim.path]), xLo, xHi, PAD.l, W - PAD.r)}
              cy={scale(champion.score ?? 0, yLo, yHi, H - PAD.b, PAD.t)}
              r={5}
              fill="none"
              stroke="#fbbf24"
              strokeWidth={2}
            >
              <title>champion</title>
            </circle>
          )}
        </svg>
      </div>
      <p className="mt-1 text-[11px] text-gray-500">
        The champion is ringed in amber. A broad hill of good scores around it means the edge
        survives being slightly wrong about this parameter; a lone high dot over bad neighbours is
        a curve fit, not an edge.
      </p>
    </div>
  )
}

/** The champion's return at each rung of the capital ladder.
 *
 * Bars, not a line: five discrete account sizes are an ordered *category*, and
 * the question ("how big is the number here versus at 1×") is a magnitude
 * comparison against a zero baseline, which a bar answers and a line only
 * implies. Horizontal so each rung can carry its dollar amount as a real label
 * instead of a rotated tick.
 *
 * Colour encodes polarity only — teal for a gain, red for a loss — because
 * "the gain became a loss at 2× capital" is the single finding this chart
 * exists to make unmissable. The study's own capital level is ringed in amber,
 * the same way the champion is ringed in the sensitivity slice, so the eye has
 * a fixed reference point to compare the others to. */
export function CapitalLadder({ sens }: { sens: CapitalSensitivity }) {
  const levels = sens.levels ?? []
  const rets = levels.map((lv) => Number(lv.metrics?.total_return_pct ?? NaN))
  const finite = rets.filter((v) => Number.isFinite(v))
  if (finite.length === 0) {
    return <p className="text-xs text-gray-500">No capital level produced a result.</p>
  }

  const rowH = 30
  const labelW = 128
  const valueW = 92
  const height = levels.length * rowH + 26
  // Always include zero: a bar chart of returns whose axis starts above zero
  // makes a 1% gain look like a 20% one.
  const lo = Math.min(0, ...finite)
  const hi = Math.max(0, ...finite)
  const span = hi - lo || 1
  const plotLo = labelW
  const plotHi = W - valueW
  const x = (v: number) => plotLo + ((v - lo) / span) * (plotHi - plotLo)
  const zero = x(0)

  return (
    <div className="overflow-x-auto">
      <svg width={W} height={height} className="min-w-[560px]">
        {/* zero baseline — the only reference line this chart needs */}
        <line
          x1={zero}
          x2={zero}
          y1={4}
          y2={levels.length * rowH + 2}
          stroke="rgba(255,255,255,0.22)"
        />
        {levels.map((lv, i) => {
          const v = Number(lv.metrics?.total_return_pct ?? NaN)
          const y = i * rowH + 6
          const isBase = lv.multiplier === 1
          const ok = Number.isFinite(v)
          const bx = ok ? Math.min(zero, x(v)) : zero
          const bw = ok ? Math.abs(x(v) - zero) : 0
          const fill = !ok
            ? 'rgba(148,163,184,0.25)'
            : v < 0
              ? 'rgba(248,113,113,0.55)'
              : 'rgba(45,212,191,0.55)'
          const trades = lv.metrics?.trade_count
          const tip =
            `${lv.multiplier}× — $${lv.capital.toLocaleString()}: ` +
            (ok ? `${v.toFixed(2)}% return` : (lv.error ?? 'no result')) +
            (trades != null ? `, ${trades} trades` : '') +
            (lv.retention != null ? `, ${(lv.retention * 100).toFixed(0)}% of the 1× return` : '')
          return (
            <g key={lv.multiplier}>
              <title>{tip}</title>
              {/* full-width hit target, bigger than the mark itself */}
              <rect x={0} y={y - 4} width={W} height={rowH - 2} fill="transparent" />
              <text
                x={labelW - 10}
                y={y + 13}
                textAnchor="end"
                fontSize="11"
                fill={isBase ? '#fbbf24' : '#9ca3af'}
              >
                {lv.multiplier}× · ${lv.capital.toLocaleString()}
              </text>
              <rect
                x={bx}
                y={y}
                width={Math.max(bw, ok ? 1.5 : 0)}
                height={rowH - 12}
                rx={3}
                fill={fill}
                stroke={isBase ? '#fbbf24' : 'none'}
                strokeWidth={isBase ? 1.5 : 0}
              />
              <text
                x={plotHi + 8}
                y={y + 13}
                fontSize="11"
                fill={isBase ? '#e5e7eb' : '#9ca3af'}
                fontFamily="ui-monospace, monospace"
              >
                {ok ? `${v >= 0 ? '+' : ''}${v.toFixed(1)}%` : '—'}
              </text>
            </g>
          )
        })}
        <text x={labelW} y={height - 4} fontSize="9" fill="#6b7280">
          total return % over the search span — the study ran at the amber level
        </text>
      </svg>
    </div>
  )
}

/** The capital-sensitivity verdict (docs/plan/14-optimizer.md §1).
 *
 * Same contract as `HoldoutCard`, and for the same reason: a champion's return
 * can be an artifact of the starting-capital number rather than a property of
 * the strategy, and that is invisible in a table of five similar-looking
 * percentages until you divide them. So the verdict is a sentence, stated
 * first, in a colour that matches how bad it is — never buried under the
 * chart.
 */
export function CapitalCard({ sens }: { sens: CapitalSensitivity }) {
  const tone: Record<CapitalVerdict, string> = {
    robust: 'border-teal-400/40 bg-teal-400/5 text-teal-200',
    'extremes-only': 'border-white/15 bg-white/[0.03] text-gray-200',
    fragile: 'border-amber-400/40 bg-amber-400/5 text-amber-200',
    broken: 'border-red-400/40 bg-red-400/5 text-red-200',
    inconclusive: 'border-white/15 bg-white/[0.03] text-gray-300',
    unknown: 'border-white/10 bg-white/[0.03] text-gray-400',
  }
  const label: Record<CapitalVerdict, string> = {
    robust: 'holds at 0.5× and 2×',
    'extremes-only': 'holds nearby, thins at the extremes',
    fragile: 'breaks down at 0.5× or 2×',
    broken: 'only works at this exact capital',
    inconclusive: 'nothing to test',
    unknown: 'could not be measured',
  }
  const worstPct =
    sens.worst_retention != null ? `${(sens.worst_retention * 100).toFixed(0)}%` : '—'

  return (
    <div className={`rounded border p-3 ${tone[sens.verdict] ?? tone.unknown}`}>
      <div className="mb-1 flex flex-wrap items-baseline gap-3">
        <span className="text-sm font-semibold">Capital sensitivity</span>
        <span className="font-mono text-lg">{label[sens.verdict] ?? sens.verdict}</span>
        <span className="text-xs opacity-70">
          studied at ${sens.base_capital.toLocaleString()} · worst of 0.5×/2× keeps {worstPct} of
          the 1× return
        </span>
      </div>
      <p className="text-xs leading-relaxed opacity-90">{sens.headline}</p>

      <div className="mt-3">
        <CapitalLadder sens={sens} />
      </div>

      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-left opacity-60">
              <th className="py-1 pr-3 font-normal">capital</th>
              <th className="py-1 pr-3 font-normal">return %</th>
              <th className="py-1 pr-3 font-normal">kept vs 1×</th>
              <th className="py-1 pr-3 font-normal">trades</th>
              <th className="py-1 pr-3 font-normal">sharpe</th>
              <th className="py-1 pr-3 font-normal">max DD %</th>
              <th className="py-1 pr-3 font-normal">exposure %</th>
            </tr>
          </thead>
          <tbody>
            {sens.levels.map((lv) => (
              <tr
                key={lv.multiplier}
                className={`border-t border-white/5 ${lv.multiplier === 1 ? 'text-amber-200/90' : 'opacity-80'}`}
              >
                <td className="py-1 pr-3 font-mono">
                  {lv.multiplier}× · ${lv.capital.toLocaleString()}
                </td>
                <td className="py-1 pr-3 font-mono">{fmt(lv.metrics?.total_return_pct)}</td>
                <td className="py-1 pr-3 font-mono">
                  {lv.retention != null ? `${(lv.retention * 100).toFixed(0)}%` : '—'}
                </td>
                <td className="py-1 pr-3 font-mono">{lv.metrics?.trade_count ?? '—'}</td>
                <td className="py-1 pr-3 font-mono">{fmt(lv.metrics?.sharpe)}</td>
                <td className="py-1 pr-3 font-mono">{fmt(lv.metrics?.max_drawdown_pct)}</td>
                <td className="py-1 pr-3 font-mono">{fmt(lv.metrics?.exposure_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* WHY, not just how much. A retention number says the champion fell
          apart; these say which lever did it, which is the difference between
          an alarming chart and an actionable one. */}
      {sens.levels.some((lv) => lv.notes?.length) && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs opacity-70 hover:opacity-100">
            Why capital changes this strategy — the levers that don't scale
          </summary>
          <div className="mt-2 space-y-2">
            {sens.levels
              .filter((lv) => lv.notes?.length)
              .map((lv) => (
                <div key={lv.multiplier}>
                  <div className="text-[11px] font-semibold opacity-80">
                    {lv.multiplier}× — ${lv.capital.toLocaleString()}
                  </div>
                  <ul className="mt-0.5 space-y-1">
                    {lv.notes.map((n) => (
                      <li key={n} className="text-[11px] leading-relaxed opacity-70">
                        · {n}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
          </div>
        </details>
      )}

      <p className="mt-2 text-[11px] opacity-60">
        The same champion, unchanged, re-run over the search span with nothing different but
        `initial_capital`. The holdout is not used here — it gets exactly one look, and the card
        above already spent it.
        {sens.reference_price != null && (
          <> Static notes assume a reference price of ${sens.reference_price.toFixed(2)} (the median
          close over that window).</>
        )}
      </p>
    </div>
  )
}
