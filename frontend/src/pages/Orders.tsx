import { AssetTypeBadge } from '../components/AssetTypeBadge'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  ApiError,
  cancelOpenOrder,
  getOpenOrders,
  getOrderTimeline,
  getOrders,
  ordersQueryString,
  type OpenOrder,
  type OrderRow,
  type OrdersFilter,
} from '../lib/api'
import { inputCls, selectCls } from '../components/editor/fields'

function openOrderSymbolSide(o: OpenOrder): { symbol: string; side: string; qty: number } {
  const leg = o.orderLegCollection?.[0]
  return {
    symbol: leg?.instrument.symbol ?? '—',
    side: leg?.instruction ?? '—',
    qty: leg?.quantity ?? o.quantity,
  }
}

function OpenOrdersPanel() {
  const queryClient = useQueryClient()
  const [confirmingId, setConfirmingId] = useState<string | null>(null)

  const openOrdersQuery = useQuery({
    queryKey: ['open-orders'],
    queryFn: getOpenOrders,
    refetchInterval: 10000,
    retry: false,
  })

  const cancelMutation = useMutation({
    mutationFn: ({ orderId, accountHash }: { orderId: string | number; accountHash: string }) =>
      cancelOpenOrder(orderId, accountHash),
    onSuccess: () => {
      setConfirmingId(null)
      queryClient.invalidateQueries({ queryKey: ['open-orders'] })
    },
  })

  const orders = openOrdersQuery.data?.orders ?? []

  return (
    <section className="mb-6 rounded border border-white/10 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-200">
          Open orders <span className="text-gray-500">(live, direct from Schwab)</span>
        </h2>
        {openOrdersQuery.isFetching && <span className="text-xs text-gray-600">Refreshing…</span>}
      </div>

      {openOrdersQuery.isError && (
        <p className="text-sm text-red-400">
          {openOrdersQuery.error instanceof ApiError
            ? openOrdersQuery.error.message
            : 'Could not load open orders — is Schwab connected?'}
        </p>
      )}
      {!openOrdersQuery.isError && !openOrdersQuery.isLoading && orders.length === 0 && (
        <p className="text-sm text-gray-600">No open orders.</p>
      )}

      {orders.length > 0 && (
        <div className="overflow-x-auto rounded border border-white/10">
          <table className="w-full text-left text-sm">
            <thead className="bg-white/5 text-gray-400">
              <tr>
                <th className="px-3 py-2 font-medium">Entered</th>
                <th className="px-3 py-2 font-medium">Symbol</th>
                <th className="px-3 py-2 font-medium">Side</th>
                <th className="px-3 py-2 font-medium">Qty</th>
                <th className="px-3 py-2 font-medium">Filled</th>
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Order ID</th>
                <th className="px-3 py-2 font-medium">Action</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => {
                const { symbol, side, qty } = openOrderSymbolSide(o)
                const key = `${o.account_hash}-${o.orderId}`
                return (
                  <tr key={key} className="border-t border-white/10">
                    <td className="px-3 py-2 whitespace-nowrap text-gray-300">
                      {o.enteredTime ? new Date(o.enteredTime).toLocaleString() : '—'}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{symbol}</td>
                    <td className="px-3 py-2 text-gray-300">{side}</td>
                    <td className="px-3 py-2 text-gray-300">{qty}</td>
                    <td className="px-3 py-2 text-gray-300">{o.filledQuantity}</td>
                    <td className="px-3 py-2 text-gray-300">{o.orderType}</td>
                    <td className="px-3 py-2 font-medium text-amber-400">{o.status}</td>
                    <td className="px-3 py-2 font-mono text-xs text-gray-500">{o.orderId}</td>
                    <td className="px-3 py-2">
                      {confirmingId === key ? (
                        <div className="flex items-center gap-1.5">
                          <button
                            onClick={() =>
                              cancelMutation.mutate({ orderId: o.orderId, accountHash: o.account_hash })
                            }
                            disabled={cancelMutation.isPending}
                            className="rounded bg-red-600 px-2 py-1 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-50"
                          >
                            {cancelMutation.isPending ? 'Canceling…' : 'Confirm cancel'}
                          </button>
                          <button
                            onClick={() => setConfirmingId(null)}
                            className="rounded border border-white/10 px-2 py-1 text-xs text-gray-400 hover:bg-white/5"
                          >
                            Back
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => setConfirmingId(key)}
                          className="rounded border border-red-500/30 px-2 py-1 text-xs text-red-400 hover:bg-red-500/10"
                        >
                          Cancel
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {cancelMutation.isError && (
        <p className="mt-3 text-sm text-red-400">
          {cancelMutation.error instanceof ApiError
            ? cancelMutation.error.message
            : 'Failed to cancel order.'}
        </p>
      )}
    </section>
  )
}

function statusColor(status: string): string {
  switch (status) {
    case 'FILLED':
      return 'text-teal-400'
    case 'REJECTED':
    case 'REJECTED_LOCAL':
      return 'text-red-400'
    case 'CANCELED':
      return 'text-amber-400'
    default:
      return 'text-gray-300'
  }
}

function fmtTime(ts: number | null): string {
  return ts ? new Date(ts).toLocaleString() : '—'
}

function fmtPx(px: number | null): string {
  return px == null ? '—' : px.toFixed(2)
}

export default function Orders() {
  const [mode, setMode] = useState('')
  const [status, setStatus] = useState('')
  const [symbol, setSymbol] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const filter: OrdersFilter = {
    mode: mode || undefined,
    status: status || undefined,
    symbol: symbol || undefined,
    start: start ? new Date(start).getTime() : undefined,
    end: end ? new Date(end).getTime() : undefined,
  }

  const ordersQuery = useQuery({
    queryKey: ['orders', filter],
    queryFn: () => getOrders(filter),
    retry: false,
  })

  const timelineQuery = useQuery({
    queryKey: ['order-timeline', expandedId],
    queryFn: () => getOrderTimeline(expandedId as string),
    enabled: !!expandedId,
    retry: false,
  })

  const orders = ordersQuery.data?.orders ?? []
  const csvHref = `/api/orders/export.csv${(() => {
    const qs = ordersQueryString(filter)
    return qs ? `?${qs}` : ''
  })()}`

  function toggleExpand(row: OrderRow) {
    setExpandedId((cur) => (cur === row.intent_id ? null : row.intent_id))
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-gray-100">Orders & Transaction History</h1>
        <div className="flex items-center gap-3">
          <Link to="/live-probe" className="text-xs text-gray-500 hover:text-gray-300">
            Live probe reconciliation →
          </Link>
          <a
            href={csvHref}
            className="rounded border border-white/10 px-3 py-1.5 text-xs text-gray-300 hover:bg-white/5"
          >
            Export CSV
          </a>
        </div>
      </div>

      <OpenOrdersPanel />

      <section className="mb-4 rounded border border-white/10 p-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Mode
            <select value={mode} onChange={(e) => setMode(e.target.value)} className={selectCls('w-32')}>
              <option value="">All</option>
              <option value="paper">Paper</option>
              <option value="live">Live</option>
            </select>
          </label>

          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Status
            <select value={status} onChange={(e) => setStatus(e.target.value)} className={selectCls('w-40')}>
              <option value="">All</option>
              <option value="PENDING_SUBMIT">Pending submit</option>
              <option value="SUBMITTED">Submitted</option>
              <option value="FILLED">Filled</option>
              <option value="CANCELED">Canceled</option>
              <option value="REJECTED">Rejected</option>
              <option value="REJECTED_LOCAL">Rejected (local)</option>
              <option value="UNKNOWN">Unknown</option>
            </select>
          </label>

          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Symbol
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              placeholder="e.g. SPY"
              className={inputCls('w-28')}
            />
          </label>

          <label className="flex flex-col gap-1 text-xs text-gray-400">
            Start
            <input
              type="date"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              className={inputCls('w-40')}
            />
          </label>

          <label className="flex flex-col gap-1 text-xs text-gray-400">
            End
            <input
              type="date"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              className={inputCls('w-40')}
            />
          </label>
        </div>
      </section>

      {ordersQuery.isLoading && <p className="text-sm text-gray-500">Loading…</p>}
      {ordersQuery.isError && (
        <p className="text-sm text-red-400">
          {ordersQuery.error instanceof ApiError ? ordersQuery.error.message : 'Could not load orders.'}
        </p>
      )}
      {!ordersQuery.isLoading && orders.length === 0 && (
        <p className="text-sm text-gray-600">No orders match these filters.</p>
      )}

      {orders.length > 0 && (
        <div className="overflow-x-auto rounded border border-white/10">
          <table className="w-full text-left text-sm">
            <thead className="bg-white/5 text-gray-400">
              <tr>
                <th className="px-3 py-2 font-medium">Time</th>
                <th className="px-3 py-2 font-medium">Strategy</th>
                <th className="px-3 py-2 font-medium">Mode</th>
                <th className="px-3 py-2 font-medium">Symbol</th>
                <th className="px-3 py-2 font-medium">Side</th>
                <th className="px-3 py-2 font-medium">Qty</th>
                <th className="px-3 py-2 font-medium">Type</th>
                <th className="px-3 py-2 font-medium">Limit/Stop</th>
                <th className="px-3 py-2 font-medium">Purpose</th>
                <th className="px-3 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <>
                  <tr
                    key={o.intent_id}
                    onClick={() => toggleExpand(o)}
                    className="cursor-pointer border-t border-white/10 hover:bg-white/5"
                  >
                    <td className="px-3 py-2 text-gray-300">{fmtTime(o.ts)}</td>
                    <td className="px-3 py-2 text-gray-300">
                      {o.is_live_probe ? (
                        <span className="text-gray-500">(live probe)</span>
                      ) : o.name ? (
                        <>
                          {o.name} <span className="text-gray-500">({o.slug})</span>
                        </>
                      ) : (
                        <span className="text-gray-500">{o.run_id}</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{o.mode ?? '—'}</td>
                    <td className="px-3 py-2 text-gray-300">
                      {o.asset_type === 'OPTION' ? (
                        // An OCC string is unreadable at a glance and its
                        // embedded spaces render badly, so the contract is
                        // shown decomposed with the underlying leading — that
                        // is what a user scans for.
                        <div className="flex flex-col gap-0.5">
                          <div className="flex items-center gap-1.5">
                            <span className="text-gray-200">{o.underlying}</span>
                            <AssetTypeBadge type="OPTION" size="xs" />
                          </div>
                          <span className="font-mono text-[11px] text-amber-200/80">
                            {o.contract_label ?? o.symbol}
                          </span>
                        </div>
                      ) : (
                        o.symbol
                      )}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{o.side}</td>
                    <td className="px-3 py-2 text-gray-300">{o.qty}</td>
                    <td className="px-3 py-2 text-gray-300">{o.order_type}</td>
                    <td className="px-3 py-2 text-gray-300">
                      {fmtPx(o.limit_px)} / {fmtPx(o.stop_px)}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{o.purpose}</td>
                    <td className={`px-3 py-2 font-medium ${statusColor(o.status)}`}>{o.status}</td>
                  </tr>
                  {expandedId === o.intent_id && (
                    <tr key={`${o.intent_id}-detail`} className="border-t border-white/10 bg-white/[0.03]">
                      <td colSpan={10} className="px-3 py-3">
                        {timelineQuery.isLoading && (
                          <p className="text-xs text-gray-500">Loading timeline…</p>
                        )}
                        {timelineQuery.isError && (
                          <p className="text-xs text-red-400">Could not load timeline.</p>
                        )}
                        {timelineQuery.data && (
                          <div className="grid grid-cols-1 gap-4 text-xs md:grid-cols-3">
                            <div>
                              <h3 className="mb-1 font-semibold text-gray-300">Gateway decision</h3>
                              {timelineQuery.data.intent.gateway_decision.length === 0 ? (
                                <p className="text-gray-600">No checks recorded.</p>
                              ) : (
                                <ul className="space-y-1">
                                  {timelineQuery.data.intent.gateway_decision.map((c, i) => (
                                    <li key={i} className="rounded border border-white/10 px-2 py-1 text-gray-400">
                                      {JSON.stringify(c)}
                                    </li>
                                  ))}
                                </ul>
                              )}
                            </div>
                            <div>
                              <h3 className="mb-1 font-semibold text-gray-300">Order record</h3>
                              {timelineQuery.data.order ? (
                                <ul className="space-y-1 text-gray-400">
                                  <li>schwab_order_id: {timelineQuery.data.order.schwab_order_id ?? '—'}</li>
                                  <li>status: {timelineQuery.data.order.status}</li>
                                  <li>submitted_at: {fmtTime(timelineQuery.data.order.submitted_at)}</li>
                                  <li>closed_at: {fmtTime(timelineQuery.data.order.closed_at)}</li>
                                </ul>
                              ) : (
                                <p className="text-gray-600">
                                  No orders row — rejected before reaching the broker.
                                </p>
                              )}
                            </div>
                            <div>
                              <h3 className="mb-1 font-semibold text-gray-300">Matched fill/order events</h3>
                              {timelineQuery.data.matched_events.length === 0 ? (
                                <p className="text-gray-600">No correlated run events found.</p>
                              ) : (
                                <ul className="space-y-1">
                                  {timelineQuery.data.matched_events.map((e, i) => (
                                    <li key={i} className="rounded border border-white/10 px-2 py-1 text-gray-400">
                                      {fmtTime(e.ts)} · {e.kind} · {JSON.stringify(e.payload)}
                                    </li>
                                  ))}
                                </ul>
                              )}
                            </div>
                          </div>
                        )}
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
