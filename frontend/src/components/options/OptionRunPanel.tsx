import type { RunOptionsStatus, RunPosition } from '../../lib/api'
import { AssetTypeBadge, RightBadge, formatExpiry, formatStrike } from '../AssetTypeBadge'

/**
 * Options detail for a live/paper run: portfolio greeks, premium at risk, and
 * a position table that shows the CONTRACT rather than an OCC string.
 *
 * "Premium at risk" is given the most prominent slot on purpose. For a long
 * option it is the exact maximum loss, known at entry — and it is the number
 * that makes the in-process underlying stop acceptable, so a user watching a
 * run should be able to see it without hunting.
 */
export function OptionRunSummary({ options }: { options: RunOptionsStatus }) {
  const pct = options.max_premium_at_risk
    ? (options.premium_at_risk / options.max_premium_at_risk) * 100
    : 0
  return (
    <section className="mb-4 rounded border border-amber-500/25 bg-amber-500/[0.04] p-4">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <AssetTypeBadge type="OPTION" />
        <h2 className="text-sm font-semibold text-amber-200">Options exposure</h2>
      </div>

      <div className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
        <div>
          <div className="text-xs text-gray-500">Premium at risk</div>
          <div className="font-mono text-gray-100">
            ${options.premium_at_risk.toLocaleString()}
            <span className="ml-1 text-xs text-gray-500">
              / ${options.max_premium_at_risk.toLocaleString()}
            </span>
          </div>
          <div className="mt-1 h-1 w-full overflow-hidden rounded bg-white/10">
            <div
              className={`h-full ${pct > 85 ? 'bg-rose-500' : 'bg-amber-500'}`}
              style={{ width: `${Math.min(pct, 100)}%` }}
            />
          </div>
          <div className="mt-0.5 text-[10px] text-gray-600">
            the exact maximum loss on long options
          </div>
        </div>
        <Metric label="Portfolio delta" value={options.portfolio_delta.toLocaleString()}
                hint="delta-shares equivalent" />
        <Metric label="Portfolio vega" value={`$${options.portfolio_vega.toFixed(0)}`}
                hint="per vol point" />
        <div>
          <div className="text-xs text-gray-500">Stop protection</div>
          <div className="text-sm">
            {options.broker_side_protection ? (
              <span className="text-emerald-300">Backstop resting at broker</span>
            ) : (
              <span className="text-amber-300">In-process only</span>
            )}
          </div>
          <div className="mt-0.5 text-[10px] leading-snug text-gray-600">
            {options.stop_basis === 'underlying'
              ? 'underlying stop is monitored by this app'
              : 'premium stop rests at the broker'}
          </div>
        </div>
      </div>

      {options.breaches.length > 0 && (
        <ul className="mt-3 space-y-0.5 text-xs text-rose-300">
          {options.breaches.map((b) => (
            <li key={b}>⚠ {b}</li>
          ))}
        </ul>
      )}

      {Object.keys(options.rejections).length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs text-gray-500 hover:text-gray-300">
            Signals that took no trade (
            {Object.values(options.rejections).reduce((a, b) => a + b, 0)})
          </summary>
          <ul className="mt-1.5 space-y-0.5 text-xs text-gray-500">
            {Object.entries(options.rejections)
              .sort((a, b) => b[1] - a[1])
              .map(([r, n]) => (
                <li key={r}>
                  • <span className="font-mono text-gray-400">{r}</span> — {n}
                </li>
              ))}
          </ul>
        </details>
      )}
    </section>
  )
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <div className="text-xs text-gray-500">{label}</div>
      <div className="font-mono text-gray-100">{value}</div>
      {hint && <div className="mt-0.5 text-[10px] text-gray-600">{hint}</div>}
    </div>
  )
}

/** One option position, rendered as a contract rather than an OCC string. */
export function OptionPositionCells({ p }: { p: RunPosition }) {
  const expiring = (p.dte ?? 99) <= 3
  return (
    <>
      <td className="px-3 py-2">
        <div className="flex flex-wrap items-center gap-1.5">
          {p.right && <RightBadge right={p.right} />}
          <span className="font-mono text-gray-200">
            {p.strike != null ? formatStrike(p.strike) : '—'}
          </span>
          <span className="text-xs text-gray-500">
            {p.expiry ? formatExpiry(p.expiry) : ''}
          </span>
          <span className={`text-xs ${expiring ? 'text-amber-300' : 'text-gray-600'}`}>
            {p.dte != null ? `${p.dte}d` : ''}
          </span>
        </div>
        <div className="mt-0.5 font-mono text-[10px] text-gray-600">{p.contract}</div>
      </td>
      <td className="px-3 py-2 text-gray-300">
        {p.qty} <span className="text-xs text-gray-600">contracts</span>
      </td>
      <td className="px-3 py-2 font-mono text-gray-300">
        {p.avg_price?.toFixed(2)}
        {p.mark != null && (
          <span className="ml-1 text-xs text-gray-500">→ {p.mark.toFixed(2)}</span>
        )}
      </td>
      <td className="px-3 py-2 font-mono text-gray-300">
        ${p.premium_at_risk?.toLocaleString() ?? '—'}
      </td>
      <td className="px-3 py-2 font-mono text-xs text-gray-400">
        {p.delta != null ? `Δ${p.delta.toFixed(2)}` : '—'}
        {p.theta != null && <span className="ml-2">Θ{p.theta.toFixed(2)}</span>}
        {p.iv != null && <span className="ml-2">{(p.iv * 100).toFixed(0)}%</span>}
      </td>
      <td
        className={`px-3 py-2 font-mono ${
          (p.unrealized_pnl ?? 0) >= 0 ? 'text-emerald-300' : 'text-rose-300'
        }`}
      >
        {p.unrealized_pnl != null
          ? `${p.unrealized_pnl >= 0 ? '+' : ''}$${p.unrealized_pnl.toFixed(2)}`
          : '—'}
      </td>
    </>
  )
}
