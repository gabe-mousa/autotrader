import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  getOptionChain,
  getOptionExpirations,
  getOptionHistory,
  getOptionTickUniverse,
  type OptionContractRow,
} from '../lib/api'
import { AssetTypeBadge, formatExpiry, formatStrike } from '../components/AssetTypeBadge'
import PayoffDiagram from '../components/PayoffDiagram'

/**
 * Option chain browser.
 *
 * Laid out the way traders read a chain: calls on the left, puts on the right,
 * strikes down the middle, at-the-money marked. That convention is worth
 * following exactly — a user scanning for a strike should not have to learn
 * this screen.
 *
 * Everything on this page is amber-accented, because everything on it is an
 * option. The equity surfaces stay emerald (components/AssetTypeBadge).
 */
export default function Chains() {
  const [input, setInput] = useState('SPY')
  const [symbol, setSymbol] = useState('SPY')
  const [expiry, setExpiry] = useState<string | null>(null)
  const [selected, setSelected] = useState<OptionContractRow | null>(null)

  const expirations = useQuery({
    queryKey: ['optionExpirations', symbol],
    queryFn: () => getOptionExpirations(symbol),
    retry: false,
  })

  const chain = useQuery({
    queryKey: ['optionChain', symbol],
    queryFn: () => getOptionChain({ symbol, maxDte: 400, strikeCount: 40 }),
    retry: false,
  })

  // Which contracts are being tick-recorded. A strike outside this band gets a
  // 30-minute snapshot and nothing more, so a user picking strikes to study
  // should see that BEFORE spending a month collecting the wrong ones.
  const universe = useQuery({
    queryKey: ['optionTickUniverse', symbol],
    queryFn: () => getOptionTickUniverse(symbol),
    retry: false,
  })
  const recorded = useMemo(
    () => new Set(universe.data?.symbols ?? []),
    [universe.data],
  )

  const byExpiry = useMemo(() => {
    const map = new Map<string, OptionContractRow[]>()
    for (const c of chain.data?.contracts ?? []) {
      const list = map.get(c.expiry) ?? []
      list.push(c)
      map.set(c.expiry, list)
    }
    return map
  }, [chain.data])

  // Per-expiry shape: how many distinct strikes, and how many of them are
  // being tick-recorded. Both are things the chain itself knows and the page
  // previously threw away.
  const expiryStats = useMemo(() => {
    const m = new Map<string, { strikes: number; contracts: number; recorded: number }>()
    for (const [exp, list] of byExpiry) {
      m.set(exp, {
        strikes: new Set(list.map((c) => c.strike)).size,
        contracts: list.length,
        recorded: list.filter((c) => recorded.has(c.symbol)).length,
      })
    }
    return m
  }, [byExpiry, recorded])

  // The selector is driven by the CHAIN, not by /expirations. The chain always
  // carries its own expiries, so a failure or an empty answer from that
  // separate endpoint must never be able to hide the selector — which is
  // exactly what happened while it was silently returning [] for every symbol.
  // /expirations is used only to ENRICH: type (weekly/monthly) and settlement.
  const expiryMeta = useMemo(() => {
    const m = new Map<string, { dte: number; type: string | null; standard: boolean }>()
    for (const e of expirations.data?.expirations ?? []) {
      m.set(e.expiry, { dte: e.dte, type: e.expiration_type, standard: e.standard })
    }
    return m
  }, [expirations.data])

  const chainExpiries = useMemo(() => [...byExpiry.keys()].sort(), [byExpiry])

  const activeExpiry = expiry && byExpiry.has(expiry) ? expiry : chainExpiries[0]
  const rows = activeExpiry ? byExpiry.get(activeExpiry) ?? [] : []
  const spot = chain.data?.underlying_px ?? null

  // strike -> {call, put}, which is how the ladder is rendered
  const ladder = useMemo(() => {
    const map = new Map<number, { call?: OptionContractRow; put?: OptionContractRow }>()
    for (const c of rows) {
      const e = map.get(c.strike) ?? {}
      if (c.right === 'C') e.call = c
      else e.put = c
      map.set(c.strike, e)
    }
    return [...map.entries()].sort((a, b) => a[0] - b[0])
  }, [rows])

  // the strike nearest spot — the row a user's eye goes to first
  const atmStrike = useMemo(() => {
    if (spot == null || ladder.length === 0) return null
    return ladder.reduce((best, [k]) =>
      Math.abs(k - spot) < Math.abs(best - spot) ? k : best, ladder[0][0])
  }, [ladder, spot])

  // Recorded depth for the selected contract, at BOTH resolutions. What a user
  // needs before trusting a backtest over this contract: snapshots are the
  // coarse whole-chain series, ticks are every quote change.
  const history = useQuery({
    queryKey: ['optionHistory', selected?.symbol],
    queryFn: () => getOptionHistory(selected!.symbol, 90),
    enabled: !!selected,
    retry: false,
  })

  function search() {
    const t = input.trim().toUpperCase()
    if (!t) return
    setExpiry(null)
    setSelected(null)
    if (t === symbol) {
      // Same symbol already loaded — changing state wouldn't re-trigger the
      // query, and the button would silently look like it did nothing.
      // Explicitly refetch so "Load chain" always does something on click.
      chain.refetch()
      expirations.refetch()
      universe.refetch()
    } else {
      setSymbol(t)
    }
  }

  return (
    <div className="max-w-6xl">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-xl font-semibold text-gray-100">Option chains</h1>
        <AssetTypeBadge type="OPTION" />
      </div>

      <div className="mb-4 flex flex-wrap items-center gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value.toUpperCase())}
          onKeyDown={(e) => e.key === 'Enter' && search()}
          placeholder="Underlying"
          className="w-32 rounded border border-white/10 bg-white/5 px-3 py-1.5 text-sm text-gray-200 placeholder:text-gray-600 focus:border-amber-500 focus:outline-none"
        />
        <button
          onClick={search}
          className="rounded bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-500"
        >
          Load chain
        </button>
        {spot != null && (
          <span className="ml-2 text-sm text-gray-400">
            {symbol} <span className="font-mono text-gray-200">{spot.toFixed(2)}</span>
          </span>
        )}
        {chain.data && chain.data.filtered > 0 && (
          <span
            className="text-xs text-gray-500"
            title={Object.entries(chain.data.filter_reasons)
              .map(([k, v]) => `${k}: ${v}`)
              .join('\n')}
          >
            showing {chain.data.returned} of {chain.data.requested} ·{' '}
            {chain.data.filtered} filtered (hover)
          </span>
        )}
      </div>

      {chain.data && ladder.length > 0 && (
        <div className="mb-4 grid grid-cols-2 gap-3 text-xs sm:grid-cols-5">
          <Tile label="Expirations" value={String(byExpiry.size)} />
          <Tile
            label="Strikes (this expiry)"
            value={String(ladder.length)}
            sub={activeExpiry ? formatExpiry(activeExpiry) : undefined}
          />
          <Tile label="Contracts shown" value={String(chain.data.returned)}
                sub={`${chain.data.filtered} filtered`} />
          <Tile
            label="Tick-recorded"
            value={recorded.size > 0 ? String(recorded.size) : '—'}
            sub={recorded.size > 0 ? 'contracts streaming' : 'not in the band'}
            tone={recorded.size > 0 ? 'good' : undefined}
          />
          <Tile
            label="Spot"
            value={spot != null ? spot.toFixed(2) : '—'}
            sub={atmStrike != null ? `ATM ${formatStrike(atmStrike)}` : undefined}
          />
        </div>
      )}

      {chain.isLoading && <p className="text-sm text-gray-500">Loading chain…</p>}
      {chain.isError && (
        <p className="text-sm text-red-400">
          Could not load {symbol}'s chain.{' '}
          {chain.error instanceof Error ? chain.error.message : ''}
        </p>
      )}

      {chainExpiries.length > 0 && (
        <div className="mb-4">
          <div className="mb-1.5 flex flex-wrap items-center gap-2 text-xs text-gray-500">
            <span className="text-gray-400">
              Expirations
              <span className="ml-1 text-gray-600">({chainExpiries.length})</span>
            </span>
            <span className="text-gray-600">
              · <span className="text-gray-500">Nk</span> = distinct strikes
            </span>
            {recorded.size > 0 && (
              <span className="text-gray-600">
                · <span className="text-emerald-400">●</span> tick-recorded
              </span>
            )}
            {expirations.data?.source === 'generated' && (
              <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-amber-300">
                dates modelled — not confirmed listed
              </span>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {chainExpiries.map((exp) => {
              const active = exp === activeExpiry
              const st = expiryStats.get(exp)
              const meta = expiryMeta.get(exp)
              const dte = meta?.dte ?? dteFrom(exp)
              return (
                <button
                  key={exp}
                  onClick={() => setExpiry(exp)}
                  className={`rounded border px-2 py-1 text-xs transition-colors ${
                    active
                      ? 'border-amber-500 bg-amber-500/15 text-amber-200'
                      : 'border-white/10 text-gray-300 hover:bg-white/5'
                  }`}
                  title={
                    st
                      ? `${st.strikes} strikes · ${st.contracts} contracts` +
                        (st.recorded ? ` · ${st.recorded} tick-recorded` : '') +
                        (meta?.type ? ` · type ${meta.type}` : '')
                      : undefined
                  }
                >
                  {formatExpiry(exp)}
                  <span className="ml-1 text-gray-500">{dte}d</span>
                  {st && <span className="ml-1 text-gray-600">· {st.strikes}k</span>}
                  {meta?.standard === false && (
                    <span className="ml-1 text-amber-400/70" title="non-standard">*</span>
                  )}
                  {st && st.recorded > 0 && (
                    <span
                      className="ml-1 text-emerald-400"
                      title={`${st.recorded} of ${st.contracts} contracts are tick-recorded`}
                    >
                      ●
                    </span>
                  )}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {ladder.length > 0 && (
        <div className="overflow-x-auto rounded border border-white/10">
          <table className="w-full text-right text-xs">
            <thead className="bg-white/5 text-gray-400">
              <tr>
                <th colSpan={7} className="border-r border-white/10 px-2 py-1.5 text-center font-medium text-green-300">
                  Calls
                </th>
                <th className="px-2 py-1.5 text-center font-medium text-gray-300">Strike</th>
                <th colSpan={7} className="border-l border-white/10 px-2 py-1.5 text-center font-medium text-rose-300">
                  Puts
                </th>
              </tr>
              <tr className="text-[10px] uppercase tracking-wide">
                {['OI', 'Vol', 'IV', 'Δ', 'Bid', 'Ask', 'Spr'].map((h) => (
                  <th key={`c-${h}`} className="px-2 py-1 font-medium">{h}</th>
                ))}
                <th className="border-x border-white/10 px-2 py-1" />
                {['Bid', 'Ask', 'Spr', 'Δ', 'IV', 'Vol', 'OI'].map((h) => (
                  <th key={`p-${h}`} className="px-2 py-1 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ladder.map(([strike, { call, put }]) => {
                const isAtm = strike === atmStrike
                const callItm = spot != null && spot > strike
                const putItm = spot != null && spot < strike
                return (
                  <tr
                    key={strike}
                    className={`border-t border-white/10 ${
                      isAtm ? 'bg-amber-500/10' : 'hover:bg-white/[0.03]'
                    }`}
                  >
                    <Cells row={call} itm={callItm} onSelect={setSelected} side="call" />
                    <td
                      className={`border-x border-white/10 px-2 py-1 text-center font-mono ${
                        isAtm ? 'font-semibold text-amber-200' : 'text-gray-200'
                      }`}
                    >
                      {formatStrike(strike)}
                      {(call && recorded.has(call.symbol)) ||
                      (put && recorded.has(put.symbol)) ? (
                        <span
                          className="ml-1 text-emerald-400"
                          title="tick-recorded: every quote change is being stored"
                        >
                          ●
                        </span>
                      ) : null}
                    </td>
                    <Cells row={put} itm={putItm} onSelect={setSelected} side="put" />
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {chain.data && ladder.length === 0 && !chain.isLoading && (
        <p className="text-sm text-gray-600">
          No contracts returned for this expiration.
        </p>
      )}

      {selected && (
        <section className="mt-5 rounded border border-amber-500/25 bg-amber-500/[0.03] p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <AssetTypeBadge type="OPTION" size="xs" />
              <span className="font-mono text-sm text-gray-100">{selected.describe}</span>
              <span className="text-xs text-gray-500">{selected.dte} DTE</span>
            </div>
            <button
              onClick={() => setSelected(null)}
              className="text-xs text-gray-500 hover:text-gray-300"
            >
              close
            </button>
          </div>

          <div className="mb-4 grid grid-cols-2 gap-x-6 gap-y-1.5 text-xs sm:grid-cols-4">
            <Stat label="Bid / Ask" value={`${fmt(selected.bid)} / ${fmt(selected.ask)}`} />
            <Stat
              label="Spread"
              value={
                selected.spread_pct != null
                  ? `${fmt(selected.spread)} (${selected.spread_pct.toFixed(1)}%)`
                  : fmt(selected.spread)
              }
              warn={(selected.spread_pct ?? 0) > 10}
            />
            <Stat label="IV" value={selected.iv != null ? `${(selected.iv * 100).toFixed(1)}%` : '—'} />
            <Stat label="Open interest" value={selected.open_interest?.toLocaleString() ?? '—'} />
            <Stat label="Delta" value={fmt(selected.delta, 3)} />
            <Stat label="Gamma" value={fmt(selected.gamma, 4)} />
            <Stat label="Theta / day" value={fmt(selected.theta, 3)} />
            <Stat label="Vega" value={fmt(selected.vega, 3)} />
          </div>

          {spot != null && selected.mark != null && (
            <PayoffDiagram
              spot={spot}
              strike={selected.strike}
              right={selected.right}
              premium={selected.mark}
              multiplier={selected.multiplier}
            />
          )}

          <div className="mt-4 rounded border border-white/10 bg-black/20 p-3">
            <div className="mb-2 flex items-center gap-2">
              <span className="text-xs font-medium text-gray-300">Recorded history</span>
              {history.data?.streaming && (
                <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-[10px] text-emerald-300">
                  streaming now
                </span>
              )}
            </div>
            {history.isLoading && <p className="text-xs text-gray-500">Loading…</p>}
            {history.data && (
              <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-xs sm:grid-cols-3">
                <Stat
                  label="Chain snapshots"
                  value={history.data.recorded.toLocaleString()}
                />
                <Stat
                  label="Streamed ticks"
                  value={history.data.ticks.count.toLocaleString()}
                />
                <Stat
                  label="Tick range"
                  value={
                    history.data.ticks.first_ts
                      ? `${fmtDay(history.data.ticks.first_ts)} → ${fmtDay(
                          history.data.ticks.last_ts!,
                        )}`
                      : '—'
                  }
                />
              </div>
            )}
            {history.data &&
              history.data.recorded === 0 &&
              history.data.ticks.count === 0 && (
                <p className="mt-2 text-xs text-gray-500">
                  Nothing recorded for this contract. Schwab serves no option history, so a
                  backtest over it prices from the model — not from observed quotes.
                </p>
              )}
          </div>

          <p className="mt-3 font-mono text-[10px] text-gray-600">{selected.symbol}</p>
        </section>
      )}
    </div>
  )
}

function Cells({
  row,
  itm,
  side,
  onSelect,
}: {
  row?: OptionContractRow
  itm: boolean
  side: 'call' | 'put'
  onSelect: (r: OptionContractRow) => void
}) {
  if (!row) {
    return (
      <>
        {Array.from({ length: 7 }).map((_, i) => (
          <td key={i} className="px-2 py-1 text-gray-700">
            —
          </td>
        ))}
      </>
    )
  }
  // In-the-money contracts are tinted, the way every options screen does it —
  // it is how a trader locates the money without reading strikes.
  const tint = itm ? 'bg-white/[0.04]' : ''
  const cells = [
    row.open_interest?.toLocaleString() ?? '—',
    row.volume?.toLocaleString() ?? '—',
    row.iv != null ? `${(row.iv * 100).toFixed(1)}` : '—',
    fmt(row.delta, 2),
    fmt(row.bid),
    fmt(row.ask),
    row.spread_pct != null ? `${row.spread_pct.toFixed(1)}%` : '—',
  ]
  const ordered = side === 'call' ? cells : [...cells].reverse()
  return (
    <>
      {ordered.map((v, i) => (
        <td
          key={i}
          onClick={() => onSelect(row)}
          className={`cursor-pointer px-2 py-1 tabular-nums text-gray-300 ${tint}`}
          title="click for detail and payoff"
        >
          {v}
        </td>
      ))}
    </>
  )
}

function Tile({
  label, value, sub, tone,
}: {
  label: string
  value: string
  sub?: string
  tone?: 'good'
}) {
  return (
    <div className="rounded border border-white/10 bg-white/[0.02] px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`font-mono text-base ${tone === 'good' ? 'text-emerald-300' : 'text-gray-100'}`}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[11px] text-gray-500">{sub}</div>}
    </div>
  )
}

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div>
      <div className="text-gray-500">{label}</div>
      <div className={`font-mono ${warn ? 'text-amber-300' : 'text-gray-200'}`}>{value}</div>
    </div>
  )
}

/** Days to expiry from an ISO date, for when /expirations has no entry. */
function dteFrom(iso: string): number {
  const ms = new Date(`${iso}T00:00:00`).getTime() - Date.now()
  return Math.max(0, Math.ceil(ms / 86_400_000))
}

function fmtDay(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`
}

function fmt(v: number | null | undefined, dp = 2): string {
  return v == null ? '—' : v.toFixed(dp)
}
