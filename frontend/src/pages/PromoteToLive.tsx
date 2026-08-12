import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ApiError,
  getLiveAccounts,
  getLiveEligibility,
  getRiskLimits,
  getStrategy,
  startLiveRun,
} from '../lib/api'
import { GREEN_GOOD, METRIC_LABELS, PCT_KEYS } from '../lib/metrics'
import { inputCls } from '../components/editor/fields'

function EligibilityTile({ label, ok, detail }: { label: string; ok: boolean; detail: string }) {
  return (
    <div
      className={`rounded border p-3 ${ok ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-red-500/30 bg-red-500/5'}`}
    >
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`text-sm font-medium ${ok ? 'text-emerald-400' : 'text-red-400'}`}>{detail}</div>
    </div>
  )
}

export default function PromoteToLive() {
  const { slug } = useParams<{ slug: string }>()
  const navigate = useNavigate()

  const [accountHash, setAccountHash] = useState('')
  const [overrideGate, setOverrideGate] = useState(false)
  const [confirmText, setConfirmText] = useState('')

  const eligibilityQuery = useQuery({
    queryKey: ['live-eligibility', slug],
    queryFn: () => getLiveEligibility(slug as string),
    enabled: !!slug,
    retry: false,
  })
  const accountsQuery = useQuery({
    queryKey: ['live-accounts'],
    queryFn: getLiveAccounts,
    retry: false,
  })
  const strategyQuery = useQuery({
    queryKey: ['strategy', slug],
    queryFn: () => getStrategy(slug as string),
    enabled: !!slug,
    retry: false,
  })
  const riskLimitsQuery = useQuery({
    queryKey: ['risk-limits'],
    queryFn: getRiskLimits,
    retry: false,
  })

  const promoteMutation = useMutation({
    mutationFn: () => startLiveRun(slug as string, accountHash, overrideGate),
  })

  if (eligibilityQuery.isLoading) return <p className="text-sm text-gray-500">Loading…</p>
  if (eligibilityQuery.isError || !eligibilityQuery.data || !slug) {
    return <p className="text-sm text-red-400">Could not load promotion eligibility.</p>
  }
  const elig = eligibilityQuery.data

  if (promoteMutation.isSuccess) {
    return (
      <div className="max-w-xl">
        <h1 className="mb-4 text-xl font-semibold text-gray-100">Promoted to live</h1>
        <div className="rounded border border-emerald-500/30 bg-emerald-500/5 p-4 text-sm text-emerald-300">
          Run <code className="rounded bg-white/10 px-1">{promoteMutation.data.run_id}</code> is
          armed and PAUSED — it will not place any orders until you separately activate it from
          the run's page.
        </div>
        <button
          onClick={() => navigate(`/running/${promoteMutation.data.run_id}`)}
          className="mt-4 rounded bg-teal-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-teal-500"
        >
          Go to run →
        </button>
      </div>
    )
  }

  // override_gate can skip every promotion-readiness check, including no
  // backtest and/or no paper run at all — an explicit, journaled choice.
  const canArm = elig.eligible || overrideGate
  const accounts = accountsQuery.data?.accounts ?? []
  const selectedAccount = accounts.find((a) => a.hash === accountHash)
  const confirmPhrase =
    selectedAccount != null
      ? `PROMOTE ${slug.toUpperCase()} TO LIVE ON ${selectedAccount.account_number_masked}`
      : ''
  const canConfirm = confirmPhrase.length > 0 && confirmText === confirmPhrase
  const canSubmit = canArm && !!accountHash && canConfirm && !promoteMutation.isPending

  return (
    <div className="max-w-3xl">
      <h1 className="mb-2 text-xl font-semibold text-gray-100">Promote to live</h1>
      <p className="mb-6 text-sm text-gray-500">
        Strategy <code className="rounded bg-white/10 px-1">{slug}</code>. This wizard arms a
        live run — it always starts PAUSED and needs a second, separate confirmation (from the
        run's detail page) before it can place any real order.
      </p>

      <section className="mb-6 rounded border border-white/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">1. Eligibility</h2>
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <EligibilityTile
            label="Backtest"
            ok={elig.has_backtest}
            detail={elig.has_backtest ? 'completed' : 'none yet'}
          />
          <EligibilityTile
            label="Paper days"
            ok={elig.paper_elapsed_days >= elig.min_paper_days}
            detail={`${elig.paper_elapsed_days.toFixed(1)} / ${elig.min_paper_days}`}
          />
          <EligibilityTile
            label="Round trips"
            ok={elig.paper_round_trips >= elig.min_round_trips}
            detail={`${elig.paper_round_trips} / ${elig.min_round_trips}`}
          />
          <EligibilityTile
            label="Overall"
            ok={elig.eligible}
            detail={elig.eligible ? 'eligible' : 'not yet'}
          />
        </div>

        {elig.backtest_metrics != null && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {Object.entries(METRIC_LABELS).map(([key, label]) => {
              const value = elig.backtest_metrics?.[key]
              if (value == null) return null
              const num = typeof value === 'number' ? value : Number(value)
              if (Number.isNaN(num)) return null
              const colored = GREEN_GOOD.has(key)
              const color = colored ? (num >= 0 ? 'text-teal-400' : 'text-red-400') : 'text-gray-100'
              const suffix = PCT_KEYS.has(key) ? '%' : ''
              return (
                <div key={key} className="rounded border border-white/10 p-3">
                  <div className="text-xs text-gray-500">{label}</div>
                  <div className={`text-lg font-medium ${color}`}>
                    {num.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                    {suffix}
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {!elig.eligible && (
          <div className="mt-4 rounded border border-amber-500/30 bg-amber-500/5 p-3 text-sm text-amber-300">
            {!elig.has_backtest && <p>No completed backtest for this strategy version.</p>}
            {!elig.has_paper_run && <p>This strategy has never been paper-traded.</p>}
            {elig.has_backtest && elig.has_paper_run && (
              <p>Paper-trading evidence is below the configured minimums.</p>
            )}
            <label className="mt-2 flex items-start gap-2">
              <input
                type="checkbox"
                checked={overrideGate}
                onChange={(e) => setOverrideGate(e.target.checked)}
                className="mt-0.5"
              />
              <span>
                Promote anyway (override_gate) — journaled.
                {(!elig.has_backtest || !elig.has_paper_run) && (
                  <strong className="text-red-300">
                    {' '}
                    This skips{' '}
                    {!elig.has_backtest && !elig.has_paper_run
                      ? 'both the backtest AND paper-trading requirements'
                      : !elig.has_backtest
                        ? 'the backtest requirement'
                        : 'the paper-trading requirement'}{' '}
                    entirely — this strategy has never been validated that way before going live.
                  </strong>
                )}
              </span>
            </label>
          </div>
        )}
      </section>

      <section className="mb-6 rounded border border-white/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">2. Account &amp; risk limits</h2>
        <label className="mb-4 flex flex-col gap-1 text-xs text-gray-400">
          Schwab account
          <select
            value={accountHash}
            onChange={(e) => setAccountHash(e.target.value)}
            className={inputCls('w-72')}
          >
            <option value="">Select an account…</option>
            {accounts.map((a) => (
              <option key={a.hash} value={a.hash}>
                {a.type} {a.account_number_masked}
              </option>
            ))}
          </select>
        </label>
        {accountsQuery.data && accounts.length === 0 && (
          <p className="mb-4 text-sm text-gray-500">
            No accounts returned — check Schwab connection status in Settings.
          </p>
        )}

        <div className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4">
          <div>
            <div className="text-xs text-gray-500">Max open positions</div>
            <div className="text-gray-200">
              {strategyQuery.data?.document.risk?.max_open_positions ?? '—'}
            </div>
          </div>
          <div>
            <div className="text-xs text-gray-500">Max daily loss</div>
            <div className="text-gray-200">
              {strategyQuery.data?.document.risk?.max_daily_loss ?? '—'}
            </div>
          </div>
          <div>
            <div className="text-xs text-gray-500">Max daily orders</div>
            <div className="text-gray-200">
              {strategyQuery.data?.document.risk?.max_daily_orders ?? '—'}
            </div>
          </div>
          <div>
            <div className="text-xs text-gray-500">Global max total notional</div>
            <div className="text-gray-200">{riskLimitsQuery.data?.max_total_notional ?? '—'}</div>
          </div>
        </div>
        <p className="mt-3 text-xs text-gray-500">
          To change these, edit the strategy or global risk limits before promoting — editing the
          strategy creates a new version and resets this gate.
        </p>
      </section>

      <section className="rounded border border-red-500/40 bg-red-500/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">3. Confirm &amp; arm</h2>
        <p className="mb-3 text-sm text-red-300">
          <strong>This will place REAL orders against your real Schwab account once activated.</strong>{' '}
          The run starts PAUSED and never trades until you separately activate it from the run's
          page.
        </p>
        <label className="mb-2 flex flex-col gap-1 text-xs text-gray-400">
          Type{' '}
          <code className="rounded bg-white/10 px-1 text-red-300">
            {confirmPhrase || '(select an account first)'}
          </code>{' '}
          to enable
          <input
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            disabled={!confirmPhrase}
            className={inputCls('w-full')}
          />
        </label>
        <button
          onClick={() => promoteMutation.mutate()}
          disabled={!canSubmit}
          className="rounded bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
        >
          {promoteMutation.isPending ? 'Arming…' : 'Arm live run'}
        </button>
        {promoteMutation.isError && (
          <p className="mt-2 text-sm text-red-400">
            {promoteMutation.error instanceof ApiError
              ? promoteMutation.error.message
              : 'Failed to promote.'}
          </p>
        )}
      </section>
    </div>
  )
}
