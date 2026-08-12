import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ApiError,
  getLiveProbeActivityLog,
  getLiveProbeOrders,
  getLiveProbeReconcile,
  getLiveProbeStatus,
  postLiveProbeCancel,
  postLiveProbeOrder,
  postLiveProbePreview,
  putLiveProbeReplace,
  type LiveProbeOrderFields,
} from '../lib/api'
import { inputCls } from '../components/editor/fields'

const CONFIRM_PHRASE = 'PLACE REAL ORDER'

export default function LiveProbe() {
  const queryClient = useQueryClient()

  const statusQuery = useQuery({ queryKey: ['live-probe-status'], queryFn: getLiveProbeStatus, retry: false })
  const ordersQuery = useQuery({
    queryKey: ['live-probe-orders'],
    queryFn: getLiveProbeOrders,
    enabled: !!statusQuery.data?.enabled,
    refetchInterval: 5000,
    retry: false,
  })
  const activityQuery = useQuery({
    queryKey: ['live-probe-activity'],
    queryFn: getLiveProbeActivityLog,
    enabled: !!statusQuery.data?.enabled,
    refetchInterval: 5000,
    retry: false,
  })

  const [accountHash, setAccountHash] = useState('')
  const [symbol, setSymbol] = useState('')
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [qty, setQty] = useState(1)
  const [orderType, setOrderType] = useState<LiveProbeOrderFields['order_type']>('market')
  const [limitPx, setLimitPx] = useState('')
  const [stopPx, setStopPx] = useState('')
  const [confirmText, setConfirmText] = useState('')
  const [previewResult, setPreviewResult] = useState<unknown>(null)
  const [cancelOrderId, setCancelOrderId] = useState('')
  const [reconcileHash, setReconcileHash] = useState('')

  const reconcileQuery = useQuery({
    queryKey: ['live-probe-reconcile', reconcileHash],
    queryFn: () => getLiveProbeReconcile(reconcileHash),
    enabled: reconcileHash.length > 0,
    retry: false,
  })

  function fields(): LiveProbeOrderFields {
    return {
      account_hash: accountHash,
      symbol: symbol.toUpperCase(),
      side,
      qty,
      order_type: orderType,
      limit_px: limitPx ? Number(limitPx) : null,
      stop_px: stopPx ? Number(stopPx) : null,
    }
  }

  const previewMutation = useMutation({
    mutationFn: () => postLiveProbePreview(fields()),
    onSuccess: (r) => setPreviewResult(r),
  })

  const placeMutation = useMutation({
    mutationFn: () => postLiveProbeOrder(fields(), true),
    onSuccess: () => {
      setConfirmText('')
      queryClient.invalidateQueries({ queryKey: ['live-probe-orders'] })
    },
  })

  const cancelMutation = useMutation({
    mutationFn: () => postLiveProbeCancel(cancelOrderId, accountHash, true),
    onSuccess: () => {
      setCancelOrderId('')
      queryClient.invalidateQueries({ queryKey: ['live-probe-orders'] })
    },
  })

  const replaceMutation = useMutation({
    mutationFn: () => putLiveProbeReplace(cancelOrderId, fields(), true),
    onSuccess: () => {
      setCancelOrderId('')
      setConfirmText('')
      queryClient.invalidateQueries({ queryKey: ['live-probe-orders'] })
    },
  })

  const canConfirm = confirmText === CONFIRM_PHRASE

  if (statusQuery.isLoading) return <p className="text-sm text-gray-500">Loading…</p>
  if (statusQuery.isError || !statusQuery.data) {
    return <p className="text-sm text-red-400">Could not load live-probe status.</p>
  }

  if (!statusQuery.data.enabled) {
    return (
      <div>
        <h1 className="mb-4 text-xl font-semibold text-gray-100">Live probe</h1>
        <div className="rounded border border-amber-500/30 bg-amber-500/5 p-4 text-sm text-amber-300">
          Disabled. Set <code className="rounded bg-white/10 px-1">live_probe_enabled=true</code> in
          your <code className="rounded bg-white/10 px-1">.env</code> and restart the backend to use
          this screen. This is the ONLY path in the app that can place a real order — it stays off
          unless you deliberately turn it on.
        </div>
      </div>
    )
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-gray-100">Live probe</h1>
        <p className="mt-1 max-w-3xl text-xs leading-relaxed text-gray-500">
          Places real orders on your brokerage account.
        </p>
      </div>

      <div className="mb-6 grid grid-cols-1 gap-4 xl:grid-cols-3">
      <section className="rounded border border-white/10 p-4 xl:col-span-1">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">Account</h2>
        <select
          value={accountHash}
          onChange={(e) => setAccountHash(e.target.value)}
          className={inputCls('w-full')}
        >
          <option value="">Select an account…</option>
          {statusQuery.data.accounts.map((a) => (
            <option key={a.hash} value={a.hash}>
              {a.type} {a.account_number_masked}
            </option>
          ))}
        </select>
        {statusQuery.data.accounts.length === 0 && (
          <p className="mt-2 text-sm text-gray-500">
            No accounts returned — check Schwab connection status in Settings.
          </p>
        )}
      </section>

      <section className="rounded border border-white/10 p-4 xl:col-span-2">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">Order</h2>
        <div className="mb-3 flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Symbol
            <input value={symbol} onChange={(e) => setSymbol(e.target.value)} className={inputCls('w-24')} />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Side
            <select value={side} onChange={(e) => setSide(e.target.value as 'buy' | 'sell')} className={inputCls('w-24')}>
              <option value="buy">Buy</option>
              <option value="sell">Sell</option>
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Qty
            <input
              type="number"
              value={qty}
              onChange={(e) => setQty(Number(e.target.value))}
              className={inputCls('w-20')}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Order type
            <select
              value={orderType}
              onChange={(e) => setOrderType(e.target.value as LiveProbeOrderFields['order_type'])}
              className={inputCls('w-32')}
            >
              <option value="market">Market</option>
              <option value="limit">Limit</option>
              <option value="stop">Stop</option>
              <option value="stop_limit">Stop limit</option>
            </select>
          </label>
          {(orderType === 'limit' || orderType === 'stop_limit') && (
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Limit px
              <input value={limitPx} onChange={(e) => setLimitPx(e.target.value)} className={inputCls('w-24')} />
            </label>
          )}
          {(orderType === 'stop' || orderType === 'stop_limit') && (
            <label className="flex flex-col gap-1 text-xs text-gray-400">
              Stop px
              <input value={stopPx} onChange={(e) => setStopPx(e.target.value)} className={inputCls('w-24')} />
            </label>
          )}
        </div>

        <button
          onClick={() => previewMutation.mutate()}
          disabled={!accountHash || !symbol || previewMutation.isPending}
          className="mr-2 rounded border border-white/10 px-3 py-1.5 text-sm text-gray-300 hover:bg-white/5 disabled:opacity-50"
        >
          {previewMutation.isPending ? 'Previewing…' : 'Preview (no order placed)'}
        </button>

        {previewResult != null && (
          <pre className="mt-3 max-h-48 overflow-auto rounded bg-white/5 p-3 text-xs text-gray-400">
            {JSON.stringify(previewResult, null, 2)}
          </pre>
        )}
        {previewMutation.isError && (
          <p className="mt-2 text-sm text-red-400">
            {previewMutation.error instanceof ApiError ? previewMutation.error.message : 'Preview failed.'}
          </p>
        )}

        <div className="mt-4 border-t border-white/10 pt-4">
          <label className="mb-2 flex flex-col gap-1 text-xs text-gray-400">
            Type <code className="rounded bg-white/10 px-1 text-red-300">{CONFIRM_PHRASE}</code> to
            enable placement
            <input
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              className={inputCls('w-64')}
            />
          </label>
          <button
            onClick={() => placeMutation.mutate()}
            disabled={!canConfirm || !accountHash || !symbol || placeMutation.isPending}
            className="rounded bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
          >
            {placeMutation.isPending ? 'Placing…' : 'Place REAL order'}
          </button>
          {placeMutation.isError && (
            <p className="mt-2 text-sm text-red-400">
              {placeMutation.error instanceof ApiError ? placeMutation.error.message : 'Placement failed.'}
            </p>
          )}
          {placeMutation.isSuccess && (
            <p className="mt-2 text-sm text-emerald-400">
              Placed: broker order {placeMutation.data.broker_order_id} ({placeMutation.data.status})
            </p>
          )}
        </div>
      </section>
      </div>

      <div className="mb-6 grid grid-cols-1 gap-4 xl:grid-cols-2">
      <section className="rounded border border-white/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">Cancel / replace an order</h2>
        <label className="mb-3 flex flex-col gap-1 text-xs text-gray-400">
          Broker order id
          <input
            value={cancelOrderId}
            onChange={(e) => setCancelOrderId(e.target.value)}
            className={inputCls('w-48')}
          />
        </label>
        <div className="flex gap-2">
          <button
            onClick={() => cancelMutation.mutate()}
            disabled={!canConfirm || !cancelOrderId || !accountHash || cancelMutation.isPending}
            className="rounded border border-amber-500/30 px-3 py-1.5 text-sm text-amber-400 hover:bg-amber-500/10 disabled:opacity-50"
          >
            Cancel order
          </button>
          <button
            onClick={() => replaceMutation.mutate()}
            disabled={!canConfirm || !cancelOrderId || !accountHash || !symbol || replaceMutation.isPending}
            className="rounded border border-amber-500/30 px-3 py-1.5 text-sm text-amber-400 hover:bg-amber-500/10 disabled:opacity-50"
          >
            Replace with the order form above
          </button>
        </div>
        <p className="mt-2 text-xs text-gray-500">
          Both also require the confirm phrase above to be typed exactly.
        </p>
        {(cancelMutation.isError || replaceMutation.isError) && (
          <p className="mt-2 text-sm text-red-400">
            {(cancelMutation.error ?? replaceMutation.error) instanceof ApiError
              ? ((cancelMutation.error ?? replaceMutation.error) as ApiError).message
              : 'Action failed.'}
          </p>
        )}
      </section>

      <section className="rounded border border-white/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">Reconcile</h2>
        <div className="mb-3 flex items-end gap-2">
          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Account hash to reconcile
            <input
              value={reconcileHash}
              onChange={(e) => setReconcileHash(e.target.value)}
              placeholder="paste the account hash from above"
              className={inputCls('w-full')}
            />
          </label>
        </div>
        {reconcileQuery.data && (
          <pre className="max-h-64 overflow-auto rounded bg-white/5 p-3 text-xs text-gray-400">
            {JSON.stringify(reconcileQuery.data, null, 2)}
          </pre>
        )}
        {reconcileQuery.isError && <p className="text-sm text-red-400">Could not reconcile.</p>}
      </section>
      </div>

      <section className="mb-6 rounded border border-white/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">Local order journal</h2>
        {(ordersQuery.data?.orders.length ?? 0) === 0 ? (
          <p className="text-sm text-gray-600">No probe orders yet.</p>
        ) : (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Time</th>
                  <th className="px-3 py-2 font-medium">Symbol</th>
                  <th className="px-3 py-2 font-medium">Side</th>
                  <th className="px-3 py-2 font-medium">Qty</th>
                  <th className="px-3 py-2 font-medium">Type</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {ordersQuery.data?.orders.map((o) => (
                  <tr key={o.id} className="border-t border-white/10">
                    <td className="px-3 py-2 text-gray-300">{new Date(o.ts).toLocaleTimeString()}</td>
                    <td className="px-3 py-2 text-gray-300">{o.symbol}</td>
                    <td className="px-3 py-2 text-gray-300">{o.side}</td>
                    <td className="px-3 py-2 text-gray-300">{o.qty}</td>
                    <td className="px-3 py-2 text-gray-300">{o.order_type}</td>
                    <td className="px-3 py-2 text-gray-300">{o.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="rounded border border-white/10 p-4">
        <h2 className="mb-3 text-sm font-semibold text-gray-200">Raw ACCT_ACTIVITY capture</h2>
        <p className="mb-2 text-xs text-gray-500">
          Schwab doesn't publish the message-type-specific JSON shapes for this stream — every raw
          message is captured here so a real parser can be built from real examples once an order
          fills.
        </p>
        {(activityQuery.data?.messages.length ?? 0) === 0 ? (
          <p className="text-sm text-gray-600">Nothing captured yet.</p>
        ) : (
          <div className="max-h-64 overflow-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Received</th>
                  <th className="px-3 py-2 font-medium">Seq</th>
                  <th className="px-3 py-2 font-medium">Raw</th>
                </tr>
              </thead>
              <tbody>
                {activityQuery.data?.messages.map((m) => (
                  <tr key={m.id} className="border-t border-white/10">
                    <td className="px-3 py-2 text-gray-300">{new Date(m.received_at).toLocaleTimeString()}</td>
                    <td className="px-3 py-2 text-gray-300">{m.seq ?? '—'}</td>
                    <td className="px-3 py-2 font-mono text-xs text-gray-400">{m.raw_json}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
