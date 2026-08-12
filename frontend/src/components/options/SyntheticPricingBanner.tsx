import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AssetTypeBadge } from '../AssetTypeBadge'

/**
 * The honesty surface for options backtests (docs/plan/16 decision D5).
 *
 * Schwab sells no historical option data, so a backtest over any period before
 * this app started recording prices its contracts with a MODEL. Those results
 * are model output, not observed outcomes, and the product must never let a
 * user forget it — so this banner is NOT dismissible and sits above the
 * metrics rather than below them.
 *
 * It also pulls the measured model error for the traded underlying, which is
 * what turns "approximate" into a number: "median 8% price error at 21-45 DTE"
 * is something a user can apply as a haircut, or use to conclude that a
 * strategy living in a badly-modelled bucket should not be trusted at all.
 */
export interface OptionsAssumptions {
  pricing_source?: string
  pct_bars_recorded?: number
  model?: string
  vol_source_mix?: Record<string, number>
  vrp_mult?: number
  fill_aggression?: number
  commission_per_contract?: number
  intrabar_iv?: string
  liquidity_NOT_enforced?: string[]
  assignment?: string
  expiry_policy?: string
  expired_unclosed_count?: number
  missing_quote_count?: number
  selection_rejections?: Record<string, number>
  skew_note?: string
  vrp_note?: string
  liquidity_note?: string
}

export default function SyntheticPricingBanner({
  assumptions,
  underlying,
}: {
  assumptions: OptionsAssumptions
  underlying?: string
}) {
  const source = assumptions.pricing_source ?? 'synthetic'
  const recorded = assumptions.pct_bars_recorded ?? 0

  const calibration = useQuery({
    queryKey: ['optionCalibration', underlying],
    queryFn: async () => {
      const r = await fetch(
        `/api/options/calibration?symbol=${encodeURIComponent(underlying!)}`,
      )
      if (!r.ok) throw new Error('calibration unavailable')
      return (await r.json()) as {
        n_observations: number
        overall?: { median_abs_price_error_pct?: number | null }
      }
    },
    enabled: !!underlying,
    retry: false,
  })

  const measured = calibration.data?.n_observations
    ? calibration.data.overall?.median_abs_price_error_pct
    : null

  return (
    <section className="mb-4 rounded border border-amber-500/40 bg-amber-500/10 p-4">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <AssetTypeBadge type="OPTION" />
        <h2 className="text-sm font-semibold text-amber-200">
          {source === 'recorded'
            ? 'Priced from recorded option quotes'
            : source === 'mixed'
              ? `Partly modelled prices — ${recorded.toFixed(0)}% from recorded quotes`
              : 'These results use MODELLED option prices'}
        </h2>
      </div>

      {source !== 'recorded' && (
        <p className="text-xs leading-relaxed text-gray-300">
          Schwab serves <strong>no historical option data</strong>, so contracts in this
          backtest were priced with Black-Scholes over a modelled volatility surface rather
          than from quotes that actually existed. Treat these numbers as{' '}
          <strong className="text-amber-200">model output, not observed outcomes</strong>.
          {measured != null && (
            <>
              {' '}Measured model error for {underlying}:{' '}
              <strong className="text-amber-200">
                {measured.toFixed(1)}% median price error
              </strong>{' '}
              against recorded chains.
            </>
          )}
          {calibration.data?.n_observations === 0 && (
            <>
              {' '}No recorded chains exist for {underlying} yet, so the model error has not
              been measured at all —{' '}
              <Link to="/data" className="underline hover:text-amber-100">
                start recording
              </Link>{' '}
              to find out.
            </>
          )}
        </p>
      )}

      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-4">
        <Item label="Volatility source" value={topSource(assumptions.vol_source_mix)} />
        <Item label="Variance risk premium" value={fmt(assumptions.vrp_mult)} />
        <Item label="Fill" value={
          assumptions.fill_aggression === 1
            ? 'full spread crossed'
            : `aggression ${fmt(assumptions.fill_aggression)}`
        } />
        <Item label="Commission" value={`$${fmt(assumptions.commission_per_contract)}/contract`} />
      </dl>

      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-amber-300/80 hover:text-amber-200">
          What else this model does and does not capture
        </summary>
        <ul className="mt-2 space-y-1 text-xs leading-relaxed text-gray-400">
          {assumptions.model && <li>• {assumptions.model}</li>}
          {assumptions.intrabar_iv && <li>• Intrabar volatility: {assumptions.intrabar_iv}</li>}
          {assumptions.liquidity_note && <li>• {assumptions.liquidity_note}</li>}
          {assumptions.vrp_note && <li>• Variance risk premium: {assumptions.vrp_note}</li>}
          {assumptions.skew_note && <li>• Skew: {assumptions.skew_note}</li>}
          {assumptions.assignment && <li>• Assignment: {assumptions.assignment}</li>}
          {assumptions.expiry_policy && <li>• Expiry: {assumptions.expiry_policy}</li>}
          {!!assumptions.expired_unclosed_count && (
            <li className="text-amber-300">
              • {assumptions.expired_unclosed_count} position(s) reached expiration
              unclosed — that should be zero and is worth investigating.
            </li>
          )}
          {!!assumptions.missing_quote_count && (
            <li>• {assumptions.missing_quote_count} bar(s) had no computable option quote.</li>
          )}
        </ul>
      </details>

      {assumptions.selection_rejections &&
        Object.keys(assumptions.selection_rejections).length > 0 && (
          <details className="mt-2">
            <summary className="cursor-pointer text-xs text-amber-300/80 hover:text-amber-200">
              Why some signals took no trade (
              {Object.values(assumptions.selection_rejections).reduce((a, b) => a + b, 0)}{' '}
              rejections)
            </summary>
            <ul className="mt-2 space-y-0.5 text-xs text-gray-400">
              {Object.entries(assumptions.selection_rejections)
                .sort((a, b) => b[1] - a[1])
                .map(([reason, n]) => (
                  <li key={reason}>
                    • <span className="font-mono text-gray-300">{reason}</span> — {n}
                  </li>
                ))}
            </ul>
          </details>
        )}
    </section>
  )
}

function Item({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-gray-500">{label}</dt>
      <dd className="font-mono text-gray-200">{value}</dd>
    </div>
  )
}

function topSource(mix?: Record<string, number>): string {
  if (!mix || Object.keys(mix).length === 0) return '—'
  const [name, pct] = Object.entries(mix).sort((a, b) => b[1] - a[1])[0]
  const pretty: Record<string, string> = {
    vol_index_anchor: 'VIX-family index',
    realized_ewma_x_vrp: 'realized × VRP',
    recorded: 'recorded chains',
    fallback_flat: 'flat fallback',
  }
  return `${pretty[name] ?? name} (${pct.toFixed(0)}%)`
}

function fmt(v: number | undefined): string {
  return v == null ? '—' : String(v)
}
