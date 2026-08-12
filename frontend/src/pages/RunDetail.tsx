import { Fragment, useState } from 'react'
import {
  OptionPositionCells,
  OptionRunSummary,
} from '../components/options/OptionRunPanel'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ApiError,
  activateLiveRun,
  cancelOpenOrder,
  flattenRun,
  getOrderStatus,
  getRun,
  pauseRun,
  recoverRun,
  resumeRun,
  startRun,
  stopRun,
  type RunEvent,
} from '../lib/api'
import { inputCls } from '../components/editor/fields'
import { StateDot, pnlColor } from './Running'

const ACTIVATE_PHRASE = 'ACTIVATE LIVE TRADING'

const LEVEL_COLOR: Record<string, string> = {
  info: 'text-gray-300',
  warning: 'text-amber-400',
  error: 'text-red-400',
}

function parsePayload(e: RunEvent): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(e.payload_json)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

function renderPayload(e: RunEvent): string {
  const parsed = parsePayload(e)
  if (parsed) {
    return Object.entries(parsed)
      .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
      .join('  ')
  }
  try {
    return String(JSON.parse(e.payload_json))
  } catch {
    return e.payload_json
  }
}

const OPEN_ORDER_STATUSES = new Set([
  'AWAITING_PARENT_ORDER', 'AWAITING_CONDITION', 'AWAITING_STOP_CONDITION',
  'AWAITING_MANUAL_REVIEW', 'ACCEPTED', 'PENDING_ACTIVATION', 'QUEUED', 'WORKING',
])

function OrderEventDetail({ orderId, accountHash }: { orderId: string; accountHash: string }) {
  const queryClient = useQueryClient()
  const [confirmingCancel, setConfirmingCancel] = useState(false)

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['order-status', orderId, accountHash],
    queryFn: () => getOrderStatus(orderId, accountHash),
  })

  const cancelMutation = useMutation({
    mutationFn: () => cancelOpenOrder(orderId, accountHash),
    onSuccess: () => {
      setConfirmingCancel(false)
      queryClient.invalidateQueries({ queryKey: ['order-status', orderId, accountHash] })
      queryClient.invalidateQueries({ queryKey: ['open-orders'] })
    },
  })

  if (isLoading) {
    return <p className="text-xs text-gray-500">Loading order status…</p>
  }
  if (isError || !data) {
    return (
      <p className="text-xs text-red-400">
        Could not load order status
        {error instanceof ApiError ? `: ${error.message}` : '.'}
      </p>
    )
  }

  const cancelable = OPEN_ORDER_STATUSES.has(data.status)

  return (
    <div className="flex flex-wrap items-center gap-3 text-xs">
      <span className="text-gray-400">
        Broker order <span className="font-mono text-gray-300">{orderId}</span>
      </span>
      <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-gray-300">
        {data.status}
      </span>
      {data.filledQuantity != null && (
        <span className="text-gray-400">
          Filled {data.filledQuantity} / {data.quantity}
        </span>
      )}
      {cancelable && (
        <>
          {!confirmingCancel ? (
            <button
              onClick={() => setConfirmingCancel(true)}
              className="rounded border border-red-500/30 px-2 py-1 text-red-400 hover:bg-red-500/10"
            >
              Cancel order
            </button>
          ) : (
            <span className="flex items-center gap-1.5">
              <button
                onClick={() => cancelMutation.mutate()}
                disabled={cancelMutation.isPending}
                className="rounded bg-red-600 px-2 py-1 text-white hover:bg-red-500 disabled:opacity-50"
              >
                {cancelMutation.isPending ? 'Canceling…' : 'Confirm cancel'}
              </button>
              <button
                onClick={() => setConfirmingCancel(false)}
                className="rounded border border-white/10 px-2 py-1 text-gray-400 hover:bg-white/5"
              >
                Back
              </button>
            </span>
          )}
        </>
      )}
      {cancelMutation.isError && (
        <span className="text-red-400">
          {cancelMutation.error instanceof ApiError ? cancelMutation.error.message : 'Cancel failed.'}
        </span>
      )}
    </div>
  )
}

export default function RunDetail() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [activateConfirmText, setActivateConfirmText] = useState('')
  const [expandedEventIdx, setExpandedEventIdx] = useState<number | null>(null)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['run', runId],
    queryFn: () => getRun(runId as string),
    enabled: !!runId,
    retry: false,
    refetchInterval: (query) => {
      const state = query.state.data?.state
      return state === 'RUNNING' || state === 'PAUSED' || state === 'STARTING' ? 3000 : false
    },
  })

  const actionMutation = useMutation({
    mutationFn: (action: 'pause' | 'resume' | 'flatten' | 'stop') => {
      if (!runId) return Promise.reject(new Error('missing run id'))
      if (action === 'pause') return pauseRun(runId)
      if (action === 'resume') return resumeRun(runId)
      if (action === 'flatten') return flattenRun(runId)
      return stopRun(runId)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  const activateMutation = useMutation({
    mutationFn: () => {
      if (!runId) return Promise.reject(new Error('missing run id'))
      return activateLiveRun(runId)
    },
    onSuccess: () => {
      setActivateConfirmText('')
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  // "Resume" a stopped run == start a brand new run of the same strategy —
  // the old run's in-process state (positions, broker connection) is gone
  // for good once stopped_at is set, there's nothing to actually continue.
  // Paper can go straight through (same one-click flow as the Running
  // page's "Start" form); live intentionally does NOT skip the promotion
  // wizard's account picker + type-to-confirm gate (see PromoteToLive.tsx)
  // — no shortcut around that for real money, resuming just saves
  // re-finding the strategy.
  const recoverMutation = useMutation({
    mutationFn: () => {
      if (!runId) return Promise.reject(new Error('missing run id'))
      return recoverRun(runId)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['run', runId] })
      queryClient.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  const resumePaperMutation = useMutation({
    mutationFn: () => {
      if (!data) return Promise.reject(new Error('run not loaded'))
      return startRun(data.slug, data.starting_equity ?? undefined)
    },
    onSuccess: (r) => {
      queryClient.invalidateQueries({ queryKey: ['runs'] })
      navigate(`/running/${r.run_id}`)
    },
  })

  if (isLoading) {
    return <p className="text-sm text-gray-500">Loading run…</p>
  }
  if (isError || !data) {
    return <p className="text-sm text-red-400">Could not load run.</p>
  }

  const live = data.live
  const positions = live ? Object.entries(live.positions) : []
  const optionsStatus = live?.options ?? null
  const isOptionsRun = !!optionsStatus

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-gray-100">
          {data.name} <span className="text-gray-500">({data.slug})</span>
        </h1>
        <span className="flex items-center gap-1.5 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-gray-400">
          <StateDot state={data.state} />
          {data.state}
        </span>
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-gray-400">
          {data.mode}
        </span>
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 font-mono text-xs text-gray-400">
          {data.strategy_version_hash}
        </span>
      </div>

      <p className="mb-4 text-sm text-gray-400">
        Started {data.started_at ? new Date(data.started_at).toLocaleString() : '—'}
        {' · '}
        Stopped {data.stopped_at ? new Date(data.stopped_at).toLocaleString() : '—'}
        {' · '}
        Starting equity {data.starting_equity != null ? data.starting_equity.toLocaleString() : '—'}
        {' · '}
        Resume policy {data.resume_policy}
      </p>

      {/* Realized P&L: today's figure only exists while the run is active (it
          lives on the in-process runner), but the lifetime figure is
          reconstructed from the fill journal and is always available. */}
      <div className="mb-6 flex flex-wrap gap-3">
        <div className="rounded border border-white/10 bg-white/5 px-4 py-2">
          <div className="text-xs text-gray-500">Realized P&L today</div>
          <div className={`text-lg ${live ? pnlColor(live.realized_pnl_today) : 'text-gray-300'}`}>
            {live ? live.realized_pnl_today.toFixed(2) : '—'}
          </div>
        </div>
        <div className="rounded border border-white/10 bg-white/5 px-4 py-2">
          <div className="text-xs text-gray-500">Total P&L since start</div>
          <div className={`text-lg ${pnlColor(data.realized_pnl_total)}`}>
            {data.realized_pnl_total >= 0 ? '+' : ''}
            {data.realized_pnl_total.toFixed(2)}
          </div>
        </div>
        <div className="rounded border border-white/10 bg-white/5 px-4 py-2">
          <div className="text-xs text-gray-500">Total P&L %</div>
          <div
            className={`text-lg ${
              data.realized_pnl_total_pct != null
                ? pnlColor(data.realized_pnl_total_pct)
                : 'text-gray-300'
            }`}
          >
            {data.realized_pnl_total_pct != null
              ? `${data.realized_pnl_total_pct >= 0 ? '+' : ''}${data.realized_pnl_total_pct.toFixed(2)}%`
              : '—'}
          </div>
        </div>
      </div>

      {live && (
        <div className="mb-6 flex flex-wrap gap-2">
          {data.state === 'RUNNING' && (
            <button
              onClick={() => actionMutation.mutate('pause')}
              disabled={actionMutation.isPending}
              className="rounded border border-white/10 px-3 py-1.5 text-sm text-gray-300 hover:bg-white/5 disabled:opacity-50"
            >
              Pause
            </button>
          )}
          {data.state === 'PAUSED' && data.mode !== 'live' && (
            <button
              onClick={() => actionMutation.mutate('resume')}
              disabled={actionMutation.isPending}
              className="rounded border border-white/10 px-3 py-1.5 text-sm text-gray-300 hover:bg-white/5 disabled:opacity-50"
            >
              Resume
            </button>
          )}
          <button
            onClick={() => actionMutation.mutate('flatten')}
            disabled={actionMutation.isPending}
            className="rounded border border-amber-500/30 px-3 py-1.5 text-sm text-amber-400 hover:bg-amber-500/10 disabled:opacity-50"
          >
            Flatten
          </button>
          <button
            onClick={() => actionMutation.mutate('stop')}
            disabled={actionMutation.isPending}
            className="rounded border border-red-500/30 px-3 py-1.5 text-sm text-red-400 hover:bg-red-500/10 disabled:opacity-50"
          >
            Stop
          </button>
        </div>
      )}

      {live && data.mode === 'live' && data.state === 'PAUSED' && (
        <div className="mb-6 rounded border border-red-500/40 bg-red-500/10 p-4">
          <p className="mb-3 text-sm text-red-300">
            <strong>This run is armed but PAUSED — it will place REAL orders once activated.</strong>{' '}
            It will not trade on its own; a second, separate confirmation is required here.
          </p>
          <label className="mb-2 flex flex-col gap-1 text-xs text-gray-400">
            Type <code className="rounded bg-white/10 px-1 text-red-300">{ACTIVATE_PHRASE}</code> to
            enable
            <input
              value={activateConfirmText}
              onChange={(e) => setActivateConfirmText(e.target.value)}
              className={inputCls('w-72')}
            />
          </label>
          <button
            onClick={() => activateMutation.mutate()}
            disabled={activateConfirmText !== ACTIVATE_PHRASE || activateMutation.isPending}
            className="rounded bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
          >
            {activateMutation.isPending ? 'Activating…' : 'Activate live trading'}
          </button>
          {activateMutation.isError && (
            <p className="mt-2 text-sm text-red-400">
              {activateMutation.error instanceof ApiError
                ? activateMutation.error.message
                : 'Failed to activate.'}
            </p>
          )}
        </div>
      )}

      {actionMutation.isError && (
        <p className="mb-4 text-sm text-red-400">
          {actionMutation.error instanceof ApiError
            ? actionMutation.error.message
            : 'Action failed.'}
        </p>
      )}

      {actionMutation.data?.forced && (
        <p className="mb-4 text-sm text-amber-400">
          Closed the local record only — this run had no active connection to the app, so
          {actionMutation.data.mode === 'live'
            ? ' any resting orders or positions on Schwab were NOT touched. Check your Schwab account directly.'
            : ' nothing on the broker side was touched.'}
        </p>
      )}

      {!live && data.stopped_at == null && (
        <div className="mb-6 rounded border border-red-500/40 bg-red-500/10 p-4">
          <p className="mb-3 text-sm text-red-300">
            <strong>This run has no active connection to the app</strong> — most likely because
            the backend restarted (or auth to Schwab was lost) while it was running. It cannot be
            paused, resumed, or flattened until it is reconnected. Stopping it here only closes
            this record — it does {data.mode === 'live' ? 'NOT' : 'not'} cancel or verify anything
            on {data.mode === 'live' ? "Schwab's" : 'the broker'} side.
            {data.mode === 'live' && ' Check your Schwab account directly for any resting orders or open positions.'}
          </p>
          <p className="mb-3 text-sm text-red-300">
            {data.mode === 'live' ? (
              <>
                Reconnecting reconciles this run against Schwab — it adopts the positions Schwab
                actually reports (not what this app last wrote down), re-adopts protective
                stop/target orders that are still resting there instead of placing duplicates,
                places a fresh one for any position whose protection died, and settles the orders
                the restart interrupted using Schwab's own answer for each. It comes back{' '}
                <strong>PAUSED</strong>: you get control back — pause, flatten, inspect — and it
                places no new order until you activate live trading again. The run keeps its
                history; it does not start over.
              </>
            ) : (
              <>
                Reconnecting rebuilds this run in the app from its saved positions, cash and
                today's risk counters, re-places the protective stop/target orders for every open
                position, and resumes it per its <code>{data.resume_policy}</code> policy — the same
                recovery the backend runs at startup. The run keeps its history; it does not start over.
              </>
            )}
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={() => recoverMutation.mutate()}
              disabled={recoverMutation.isPending}
              className="rounded bg-teal-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-teal-500 disabled:opacity-50"
            >
              {recoverMutation.isPending ? 'Reconnecting…' : 'Reconnect run'}
            </button>
            <button
              onClick={() => actionMutation.mutate('stop')}
              disabled={actionMutation.isPending || recoverMutation.isPending}
              className="rounded bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
            >
              {actionMutation.isPending ? 'Stopping…' : 'Force stop'}
            </button>
          </div>
          {recoverMutation.isError && (
            <p className="mt-2 text-sm text-red-400">
              {recoverMutation.error instanceof ApiError
                ? `Reconnect failed: ${recoverMutation.error.message}`
                : 'Reconnect failed.'}
            </p>
          )}
          {recoverMutation.data && (
            <div className="mt-2 space-y-1 text-sm text-teal-300">
              <p>
                Reconnected with {recoverMutation.data.positions} open position
                {recoverMutation.data.positions === 1 ? '' : 's'}
                {recoverMutation.data.reconstructed_cash != null &&
                  ` and $${recoverMutation.data.reconstructed_cash.toFixed(2)} cash`}
                {recoverMutation.data.started_paused
                  ? ' — PAUSED. It places no order until you resume it.'
                  : '.'}
              </p>
              {/* Live: everything reconciling against Schwab changed. Shown
                  because each line is a real difference between what this app
                  believed and what the broker holds — not a detail to bury. */}
              {recoverMutation.data.adopted_protective_orders != null &&
                recoverMutation.data.adopted_protective_orders > 0 && (
                  <p>
                    Adopted {recoverMutation.data.adopted_protective_orders} protective order
                    {recoverMutation.data.adopted_protective_orders === 1 ? '' : 's'} still resting
                    at Schwab.
                  </p>
                )}
              {recoverMutation.data.qty_corrected?.map((c) => (
                <p key={`q-${c.symbol}`} className="text-amber-300">
                  {c.symbol}: quantity corrected to Schwab's {c.broker_qty} (this app had{' '}
                  {c.journal_qty}).
                </p>
              ))}
              {recoverMutation.data.adopted_from_broker?.map((a) => (
                <p key={`a-${a.symbol}`} className="text-amber-300">
                  {a.symbol}: adopted {a.qty} shares Schwab holds that this run had no record of,
                  at its average price {a.avg_price}.
                </p>
              ))}
              {recoverMutation.data.closed_while_down?.map((c) => (
                <p key={`c-${c.symbol}`} className="text-amber-300">
                  {c.symbol}: Schwab no longer holds this position — it closed while the app was
                  down, so it was dropped from this run.
                </p>
              ))}
              {recoverMutation.data.outside_universe?.map((o) => (
                <p key={`o-${o.symbol}`} className="text-red-400">
                  {o.symbol}: held ({o.broker_qty} at Schwab) but no longer in this strategy's
                  universe, so this run cannot manage it — the app has no bars or subscription for
                  it. Close it yourself on Schwab, or revert the strategy's symbol list.
                </p>
              ))}
              {recoverMutation.data.settled != null && recoverMutation.data.settled.length > 0 && (
                <p className="text-amber-300">
                  Settled {recoverMutation.data.settled.length} interrupted order
                  {recoverMutation.data.settled.length === 1 ? '' : 's'} from Schwab's answer:{' '}
                  {recoverMutation.data.settled
                    .map((s) => `${s.symbol} ${s.status}`)
                    .join(', ')}
                  . P&L for a fill that landed while the app was down is not included in this run's
                  totals — it cannot be derived after the fact.
                </p>
              )}
              {recoverMutation.data.unknown != null && recoverMutation.data.unknown.length > 0 && (
                <p className="text-red-400">
                  {recoverMutation.data.unknown.length} order
                  {recoverMutation.data.unknown.length === 1 ? '' : 's'} Schwab cannot account for
                  at all, left UNKNOWN ({recoverMutation.data.unknown.map((u) => u.symbol).join(', ')}) —
                  resolve these by hand on the Orders page.
                </p>
              )}
            </div>
          )}
        </div>
      )}

      {!live && data.stopped_at != null && (
        <div className="mb-6 flex flex-wrap items-center gap-3">
          <p className="text-sm text-gray-600">This run is not currently active.</p>
          {data.mode === 'paper' && (
            <button
              onClick={() => resumePaperMutation.mutate()}
              disabled={resumePaperMutation.isPending}
              title="Starts a brand new run of this strategy — the old run's positions/state are gone, this doesn't continue them"
              className="rounded bg-teal-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-teal-500 disabled:opacity-50"
            >
              {resumePaperMutation.isPending ? 'Starting…' : 'Resume (start new run)'}
            </button>
          )}
          {data.mode === 'live' && (
            <button
              onClick={() => navigate(`/strategies/${data.slug}/promote`)}
              className="rounded bg-teal-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-teal-500"
            >
              Resume (re-arm live)
            </button>
          )}
        </div>
      )}
      {resumePaperMutation.isError && (
        <p className="mb-4 text-sm text-red-400">
          {resumePaperMutation.error instanceof ApiError
            ? resumePaperMutation.error.message
            : 'Failed to start new run.'}
        </p>
      )}

      {live && live.pending_entries.length > 0 && (
        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-gray-200">Pending entries</h2>
          <div className="flex flex-wrap gap-1.5">
            {live.pending_entries.map((sym) => (
              <span
                key={sym}
                className="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-xs text-gray-300"
              >
                {sym}
              </span>
            ))}
          </div>
        </section>
      )}

      {optionsStatus && <OptionRunSummary options={optionsStatus} />}

      {live && (
        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-gray-200">Positions</h2>
          {positions.length === 0 ? (
            <p className="text-sm text-gray-600">No open positions.</p>
          ) : (
            <div className="overflow-x-auto rounded border border-white/10">
              <table className="w-full text-left text-sm">
                <thead className="bg-white/5 text-gray-400">
                  {/* Options and shares need genuinely different columns — a
                      contract has a strike, an expiry and greeks; a share
                      position has none of those. Showing one header for both
                      would mean padding one of them with blanks. */}
                  {isOptionsRun ? (
                    <tr>
                      <th className="px-3 py-2 font-medium">Contract</th>
                      <th className="px-3 py-2 font-medium">Qty</th>
                      <th className="px-3 py-2 font-medium">Entry → mark</th>
                      <th className="px-3 py-2 font-medium">At risk</th>
                      <th className="px-3 py-2 font-medium">Greeks</th>
                      <th className="px-3 py-2 font-medium">Unrealized</th>
                    </tr>
                  ) : (
                    <tr>
                      <th className="px-3 py-2 font-medium">Symbol</th>
                      <th className="px-3 py-2 font-medium">Qty</th>
                      <th className="px-3 py-2 font-medium">Avg price</th>
                      <th className="px-3 py-2 font-medium">Bars held</th>
                    </tr>
                  )}
                </thead>
                <tbody>
                  {positions.map(([symbol, p]) =>
                    p.asset_type === 'OPTION' ? (
                      <tr key={symbol} className="border-t border-white/10">
                        <OptionPositionCells p={p} />
                      </tr>
                    ) : (
                      <tr key={symbol} className="border-t border-white/10">
                        <td className="px-3 py-2 text-gray-300">{symbol}</td>
                        <td className="px-3 py-2 text-gray-300">{p.qty}</td>
                        <td className="px-3 py-2 text-gray-300">{p.avg_price.toFixed(2)}</td>
                        <td className="px-3 py-2 text-gray-300">{p.bars_held}</td>
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      <section>
        <h2 className="mb-2 text-sm font-semibold text-gray-200">Recent events</h2>
        {data.recent_events.length === 0 ? (
          <p className="text-sm text-gray-600">No events yet.</p>
        ) : (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Time</th>
                  <th className="px-3 py-2 font-medium">Level</th>
                  <th className="px-3 py-2 font-medium">Kind</th>
                  <th className="px-3 py-2 font-medium">Payload</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_events.map((e, i) => {
                  const payload = parsePayload(e)
                  const brokerOrderId = e.kind === 'order' ? (payload?.broker_order_id as string | undefined) : undefined
                  const clickable = !!brokerOrderId && !!data.account_hash
                  const expanded = expandedEventIdx === i
                  return (
                    <Fragment key={i}>
                      <tr
                        onClick={clickable ? () => setExpandedEventIdx(expanded ? null : i) : undefined}
                        className={`border-t border-white/10 ${clickable ? 'cursor-pointer hover:bg-white/5' : ''}`}
                      >
                        <td className="px-3 py-2 whitespace-nowrap text-gray-300">
                          {new Date(e.ts).toLocaleTimeString()}
                        </td>
                        <td className={`px-3 py-2 ${LEVEL_COLOR[e.level] ?? 'text-gray-300'}`}>
                          {e.level}
                        </td>
                        <td className="px-3 py-2 text-gray-300">
                          {e.kind}
                          {clickable && <span className="ml-1 text-gray-500">{expanded ? '▾' : '▸'}</span>}
                        </td>
                        <td className="px-3 py-2 font-mono text-xs text-gray-400">
                          {renderPayload(e)}
                        </td>
                      </tr>
                      {expanded && brokerOrderId && data.account_hash && (
                        <tr className="border-t border-white/5 bg-white/[0.02]">
                          <td colSpan={4} className="px-3 py-2">
                            <OrderEventDetail orderId={brokerOrderId} accountHash={data.account_hash} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
