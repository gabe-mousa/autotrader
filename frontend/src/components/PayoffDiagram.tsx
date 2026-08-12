/**
 * Payoff at expiry for a single long option.
 *
 * Inline SVG rather than a charting dependency: it is one polyline, two
 * reference lines and a fill, and the bundle is already 690 kB.
 *
 * The shape it communicates is the whole point of the feature — loss is FLAT
 * and CAPPED below the strike (you cannot lose more than the premium), and
 * unbounded above the breakeven. That asymmetry is what makes a long option
 * defined-risk, which is what the entire live-safety argument rests on.
 */
export default function PayoffDiagram({
  spot,
  strike,
  right,
  premium,
  multiplier = 100,
  qty = 1,
  width = 560,
  height = 180,
}: {
  spot: number
  strike: number
  right: 'C' | 'P'
  premium: number
  multiplier?: number
  qty?: number
  width?: number
  height?: number
}) {
  const isCall = right === 'C'
  const breakeven = isCall ? strike + premium : strike - premium
  const maxLoss = -premium * multiplier * qty

  // Domain: wide enough to show the hockey stick, centred so both the strike
  // and the current spot are always visible.
  const span = Math.max(Math.abs(spot - strike) * 2, spot * 0.25, premium * 4)
  const lo = Math.max(spot - span, 0.01)
  const hi = spot + span

  const points: Array<[number, number]> = []
  const STEPS = 96
  for (let i = 0; i <= STEPS; i++) {
    const s = lo + ((hi - lo) * i) / STEPS
    const intrinsic = isCall ? Math.max(s - strike, 0) : Math.max(strike - s, 0)
    points.push([s, (intrinsic - premium) * multiplier * qty])
  }

  const pnls = points.map((p) => p[1])
  const yMin = Math.min(...pnls, maxLoss)
  const yMax = Math.max(...pnls, Math.abs(maxLoss) * 0.6)
  const pad = { l: 44, r: 10, t: 10, b: 22 }
  const iw = width - pad.l - pad.r
  const ih = height - pad.t - pad.b

  const x = (v: number) => pad.l + ((v - lo) / (hi - lo)) * iw
  const y = (v: number) => pad.t + ih - ((v - yMin) / (yMax - yMin || 1)) * ih

  const line = points.map(([s, p]) => `${x(s).toFixed(1)},${y(p).toFixed(1)}`).join(' ')
  const zeroY = y(0)
  const areaDown =
    `${line} ${x(hi).toFixed(1)},${zeroY.toFixed(1)} ${x(lo).toFixed(1)},${zeroY.toFixed(1)}`

  return (
    <div className="overflow-x-auto">
      <svg width={width} height={height} role="img"
           aria-label={`Payoff at expiry for a ${isCall ? 'call' : 'put'} struck at ${strike}`}>
        {/* zero line */}
        <line x1={pad.l} x2={width - pad.r} y1={zeroY} y2={zeroY}
              stroke="rgba(255,255,255,0.18)" strokeWidth={1} />
        {/* the capped-loss region, which is the point of the picture */}
        <polygon points={areaDown} fill="rgba(244,63,94,0.10)" />
        <polyline points={line} fill="none" stroke="#fbbf24" strokeWidth={2} />

        {/* strike */}
        <line x1={x(strike)} x2={x(strike)} y1={pad.t} y2={height - pad.b}
              stroke="rgba(251,191,36,0.35)" strokeDasharray="3 3" strokeWidth={1} />
        <text x={x(strike)} y={height - 8} textAnchor="middle"
              className="fill-amber-300/80" fontSize={9}>
          K {round(strike)}
        </text>

        {/* breakeven */}
        <line x1={x(breakeven)} x2={x(breakeven)} y1={pad.t} y2={height - pad.b}
              stroke="rgba(148,163,184,0.4)" strokeDasharray="2 4" strokeWidth={1} />
        <text x={x(breakeven)} y={pad.t + 9} textAnchor="middle"
              className="fill-gray-400" fontSize={9}>
          BE {round(breakeven)}
        </text>

        {/* spot */}
        <line x1={x(spot)} x2={x(spot)} y1={pad.t} y2={height - pad.b}
              stroke="rgba(52,211,153,0.55)" strokeWidth={1} />
        <text x={x(spot)} y={height - 8} textAnchor="middle"
              className="fill-emerald-300/80" fontSize={9}>
          spot {round(spot)}
        </text>

        {/* max loss */}
        <text x={pad.l - 6} y={y(maxLoss) + 3} textAnchor="end"
              className="fill-rose-300/80" fontSize={9}>
          {money(maxLoss)}
        </text>
        <text x={pad.l - 6} y={zeroY + 3} textAnchor="end"
              className="fill-gray-500" fontSize={9}>
          0
        </text>
      </svg>
      <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
        At expiry. Maximum loss is{' '}
        <span className="text-rose-300">{money(maxLoss)}</span> — the premium paid,
        and it cannot exceed that no matter how far the underlying moves against you.
        Breakeven <span className="text-gray-300">{round(breakeven)}</span>.
      </p>
    </div>
  )
}

function round(v: number): string {
  return v % 1 === 0 ? String(v) : v.toFixed(2)
}

function money(v: number): string {
  const s = Math.abs(v) >= 1000 ? Math.round(Math.abs(v)).toLocaleString()
                                : Math.abs(v).toFixed(2)
  return `${v < 0 ? '-' : ''}$${s}`
}
