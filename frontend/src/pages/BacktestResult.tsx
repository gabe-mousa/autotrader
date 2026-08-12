import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation, useParams } from 'react-router-dom'
import {
  createChart,
  createSeriesMarkers,
  AreaSeries,
  CandlestickSeries,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { Link } from 'react-router-dom'
import { getBacktest, postBacktestWhatIf, getCandles, type Trade } from '../lib/api'
import { useTheme, chartTheme } from '../lib/theme'
import { GREEN_GOOD, METRIC_LABELS, PCT_KEYS } from '../lib/metrics'
import CollapsiblePanel from '../components/editor/CollapsiblePanel'
import SyntheticPricingBanner, {
  type OptionsAssumptions,
} from '../components/options/SyntheticPricingBanner'

type TradeSortKey = 'entry_ts' | 'entry_px' | 'exit_ts' | 'exit_px' | 'pnl' | 'exit_reason'

const ALL_SYMBOLS = '__all__'
const ALL_REASONS = '__all__'

/** Above this many trades on one chart, entry/exit markers lose their text
 * labels — see the marker effect. */
const MAX_LABELLED_MARKERS = 25

interface TradeStats {
  count: number
  wins: number
  losses: number
  winRate: number
  totalPnl: number
  avgPnl: number
  avgPnlPct: number
  bestPnl: number
  worstPnl: number
  /** gross wins / gross losses. Infinity when there are no losers (and at
   * least one winner) — rendered as "∞" rather than a misleading number. */
  profitFactor: number
  avgBarsHeld: number
}

function statsFor(trades: Trade[]): TradeStats {
  const count = trades.length
  const wins = trades.filter((t) => t.pnl >= 0)
  const losses = trades.filter((t) => t.pnl < 0)
  const totalPnl = trades.reduce((s, t) => s + t.pnl, 0)
  const grossWin = wins.reduce((s, t) => s + t.pnl, 0)
  const grossLoss = Math.abs(losses.reduce((s, t) => s + t.pnl, 0))
  return {
    count,
    wins: wins.length,
    losses: losses.length,
    winRate: count ? (wins.length / count) * 100 : 0,
    totalPnl,
    avgPnl: count ? totalPnl / count : 0,
    avgPnlPct: count ? trades.reduce((s, t) => s + t.pnl_pct, 0) / count : 0,
    bestPnl: count ? Math.max(...trades.map((t) => t.pnl)) : 0,
    worstPnl: count ? Math.min(...trades.map((t) => t.pnl)) : 0,
    profitFactor: grossLoss > 0 ? grossWin / grossLoss : grossWin > 0 ? Infinity : 0,
    avgBarsHeld: count ? trades.reduce((s, t) => s + t.bars_held, 0) / count : 0,
  }
}

/** Per-symbol stats, biggest total P&L contribution first. */
function symbolStats(trades: Trade[]): { symbol: string; stats: TradeStats }[] {
  const bySymbol = new Map<string, Trade[]>()
  for (const t of trades) {
    const arr = bySymbol.get(t.symbol)
    if (arr) arr.push(t)
    else bySymbol.set(t.symbol, [t])
  }
  return Array.from(bySymbol, ([symbol, ts]) => ({ symbol, stats: statsFor(ts) })).sort(
    (a, b) => b.stats.totalPnl - a.stats.totalPnl,
  )
}

function fmtPf(pf: number): string {
  if (pf === Infinity) return '∞'
  return pf.toFixed(2)
}

/** Compound a month-keyed return table into a year-keyed one, for runs stored
 * before the backend started emitting yearly_returns. Returns are multiplicative,
 * so the months chain rather than sum. */
function foldMonthsToYears(monthly: Record<string, number>): Record<string, number> {
  const growth = new Map<string, number>()
  for (const [month, pct] of Object.entries(monthly)) {
    const year = month.slice(0, 4)
    growth.set(year, (growth.get(year) ?? 1) * (1 + pct / 100))
  }
  const out: Record<string, number> = {}
  for (const [year, g] of growth) {
    out[year] = Math.round((g - 1) * 100 * 100) / 100
  }
  return out
}

const CSV_COLUMNS: (keyof Trade)[] = [
  'symbol', 'side', 'entry_ts', 'entry_px', 'exit_ts', 'exit_px', 'qty',
  'pnl', 'pnl_pct', 'mae', 'mfe', 'bars_held', 'exit_reason',
]

/** Export exactly what's on screen (current filter + sort), so a CSV always
 * matches the table it was taken from. Timestamps go out as ISO strings —
 * Excel mangles bare epoch seconds into 1970 dates. */
function tradesToCsv(trades: Trade[]): string {
  const head = CSV_COLUMNS.join(',')
  const rows = trades.map((t) =>
    CSV_COLUMNS.map((c) => {
      const v = t[c]
      if (c === 'entry_ts' || c === 'exit_ts') return new Date((v as number) * 1000).toISOString()
      return typeof v === 'string' && (v.includes(',') || v.includes('"'))
        ? `"${v.replace(/"/g, '""')}"`
        : String(v)
    }).join(','),
  )
  return [head, ...rows].join('\n')
}

function downloadCsv(filename: string, csv: string): void {
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export default function BacktestResult() {
  const { theme } = useTheme()
  const { id } = useParams<{ id: string }>()
  // Set when the optimizer's "promote champion" flow navigated here, so the
  // user lands on the actual result instead of being handed an id to hunt
  // for. Purely a display hint — nothing here depends on it.
  const { state } = useLocation() as {
    state: { fromStudy?: string; note?: string } | null
  }

  const { data: stored, isLoading, isError } = useQuery({
    queryKey: ['backtest', id],
    queryFn: () => getBacktest(id as string),
    enabled: !!id,
    retry: false,
  })

  // ---- temporarily excluded trades --------------------------------------
  // Purely local: kept in component state and recomputed server-side on
  // demand, never persisted. Reloading the page restores the full result.
  const [hidden, setHidden] = useState<Set<number>>(() => new Set())
  const hiddenIds = useMemo(() => [...hidden].sort((a, b) => a - b), [hidden])

  const whatIf = useQuery({
    queryKey: ['backtest-what-if', id, hiddenIds],
    queryFn: () => postBacktestWhatIf(id as string, hiddenIds),
    enabled: !!id && hiddenIds.length > 0,
    retry: false,
  })

  // Everything below renders from `data`: the stored result normally, or the
  // recomputed one while trades are hidden. Swapping at this single seam is
  // what makes every downstream stat, chart and export adjust together —
  // there is no second code path to keep in sync.
  const data = useMemo(() => {
    if (!stored) return stored
    if (hiddenIds.length === 0 || !whatIf.data) return stored
    return {
      ...stored,
      metrics: whatIf.data.metrics,
      equity: whatIf.data.equity,
      trades: whatIf.data.trades,
    }
  }, [stored, hiddenIds.length, whatIf.data])

  const hiddenTrades = useMemo(
    () => (stored?.trades ?? []).filter((t) => hidden.has(t.id)),
    [stored?.trades, hidden],
  )

  function hideTrade(tradeId: number) {
    setHidden((prev) => new Set(prev).add(tradeId))
  }
  function restoreTrade(tradeId: number) {
    setHidden((prev) => {
      const next = new Set(prev)
      next.delete(tradeId)
      return next
    })
  }

  const equityRef = useRef<HTMLDivElement | null>(null)
  const ddRef = useRef<HTMLDivElement | null>(null)
  const equityChartRef = useRef<IChartApi | null>(null)
  const ddChartRef = useRef<IChartApi | null>(null)
  const equitySeriesRef = useRef<ISeriesApi<'Area'> | null>(null)
  const ddSeriesRef = useRef<ISeriesApi<'Area'> | null>(null)

  // ---- price chart with entry/exit trade markers (feature 1) -------------
  const priceRef = useRef<HTMLDivElement | null>(null)
  const priceChartRef = useRef<IChartApi | null>(null)
  const priceSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const priceMarkersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)

  const bar = data?.bar ?? null
  const symbols = useMemo(() => data?.symbols ?? [], [data?.symbols])

  // Which universe symbol the price & trades chart is showing. Null until
  // the backtest loads; falls back to the first symbol whenever the current
  // selection isn't in the list (e.g. navigating between backtests).
  const [selected, setSelected] = useState<string | null>(null)

  // ---- trade log filtering (declared up here because the price chart's
  // marker effect below reads exitReason) --------------------------------
  // ALL_SYMBOLS is the default; the symbol filter also drives the exit-reason
  // list, so you never see a reason that can't match under the current symbol.
  const [tradeSymbol, setTradeSymbol] = useState<string>(ALL_SYMBOLS)
  const [exitReason, setExitReason] = useState<string>(ALL_REASONS)
  const symbol = selected && symbols.includes(selected) ? selected : (symbols[0] ?? null)

  const candlesQuery = useQuery({
    queryKey: ['backtest-candles', symbol, bar, data?.config.start, data?.config.end],
    queryFn: () => {
      const startMs = new Date(`${data!.config.start}T00:00:00Z`).getTime()
      const endMs = new Date(`${data!.config.end}T23:59:59Z`).getTime()
      return getCandles(symbol!, bar!, startMs, endMs)
    },
    enabled: !!symbol && !!bar && !!data,
    retry: false,
  })

  const hasPriceChart = !!symbol && !!bar

  useEffect(() => {
    const container = priceRef.current
    if (!container) return

    const chart = createChart(container, {
      ...chartTheme(theme),
      width: container.clientWidth,
      height: container.clientHeight,
      timeScale: { timeVisible: true, secondsVisible: false },
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a',
      downColor: '#ef5350',
      borderUpColor: '#26a69a',
      borderDownColor: '#ef5350',
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350',
    })
    const markers = createSeriesMarkers<Time>(series, [])

    priceChartRef.current = chart
    priceSeriesRef.current = series
    priceMarkersRef.current = markers

    const resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry) return
      const { width, height } = entry.contentRect
      chart.resize(width, height)
    })
    resizeObserver.observe(container)

    return () => {
      resizeObserver.disconnect()
      chart.remove()
      priceChartRef.current = null
      priceSeriesRef.current = null
      priceMarkersRef.current = null
    }
    // priceRef only mounts once `symbol && bar` are known (see the JSX guard
    // below) — the very first render is always the `isLoading` early-return
    // above, so a `[]`-only effect would see a null container forever and
    // never create the chart. Re-run when the container actually appears.
    // Keyed on the section's mount condition (not `symbol` itself) so
    // switching symbols swaps the data instead of rebuilding the chart.
  }, [hasPriceChart])

  useEffect(() => {
    priceChartRef.current?.applyOptions(chartTheme(theme))
    equityChartRef.current?.applyOptions(chartTheme(theme))
    ddChartRef.current?.applyOptions(chartTheme(theme))
  }, [theme])

  useEffect(() => {
    if (!candlesQuery.data || !priceSeriesRef.current || !priceMarkersRef.current) return
    const candleData = candlesQuery.data.candles.map((c) => ({
      time: c.time as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }))
    priceSeriesRef.current.setData(candleData)

    // Only the charted symbol's trades — otherwise a multi-symbol run would
    // stamp every other symbol's entries onto this price series — and honour
    // the trade log's exit-reason filter, so narrowing to e.g. `stop_loss`
    // isolates those exits on the chart too.
    const charted = (data?.trades ?? []).filter(
      (t) =>
        t.symbol === symbol && (exitReason === ALL_REASONS || t.exit_reason === exitReason),
    )
    // Per-marker price/P&L labels collide into an unreadable smear once a
    // symbol has more than a few dozen round trips (a 30m strategy easily has
    // 90+). Past that, draw arrows only — the trade log below carries the
    // numbers, and filtering by exit reason brings the labels back.
    const withLabels = charted.length <= MAX_LABELLED_MARKERS
    const markers: SeriesMarker<Time>[] = []
    for (const t of charted) {
      markers.push({
        time: t.entry_ts as UTCTimestamp,
        position: 'belowBar',
        shape: 'arrowUp',
        color: '#2dd4bf',
        ...(withLabels ? { text: `entry ${t.entry_px.toFixed(2)}` } : {}),
      })
      const won = t.pnl >= 0
      markers.push({
        time: t.exit_ts as UTCTimestamp,
        position: 'aboveBar',
        shape: 'arrowDown',
        color: won ? '#2dd4bf' : '#ef5350',
        ...(withLabels ? { text: `${won ? '+' : ''}${t.pnl.toFixed(2)}` } : {}),
      })
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number))
    priceMarkersRef.current.setMarkers(markers)
    priceChartRef.current?.timeScale().fitContent()
  }, [candlesQuery.data, data?.trades, symbol, exitReason])

  useEffect(() => {
    const container = equityRef.current
    const ddContainer = ddRef.current
    if (!container || !ddContainer) return

    const chart = createChart(container, {
      ...chartTheme(theme),
      width: container.clientWidth,
      height: container.clientHeight,
      timeScale: { timeVisible: true, secondsVisible: false },
    })
    const equitySeries = chart.addSeries(AreaSeries, {
      lineColor: '#2dd4bf',
      topColor: 'rgba(45, 212, 191, 0.3)',
      bottomColor: 'rgba(45, 212, 191, 0)',
    })

    const ddChart = createChart(ddContainer, {
      ...chartTheme(theme),
      width: ddContainer.clientWidth,
      height: ddContainer.clientHeight,
      timeScale: { timeVisible: true, secondsVisible: false },
    })
    const ddSeries = ddChart.addSeries(AreaSeries, {
      lineColor: '#ef5350',
      topColor: 'rgba(239, 83, 80, 0)',
      bottomColor: 'rgba(239, 83, 80, 0.3)',
    })

    equityChartRef.current = chart
    ddChartRef.current = ddChart
    equitySeriesRef.current = equitySeries
    ddSeriesRef.current = ddSeries

    const resizeObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect
        if (entry.target === container) chart.resize(width, height)
        if (entry.target === ddContainer) ddChart.resize(width, height)
      }
    })
    resizeObserver.observe(container)
    resizeObserver.observe(ddContainer)

    return () => {
      resizeObserver.disconnect()
      chart.remove()
      ddChart.remove()
      equityChartRef.current = null
      ddChartRef.current = null
      equitySeriesRef.current = null
      ddSeriesRef.current = null
    }
    // Same reasoning as the price-chart effect above: these containers only
    // exist once the `isLoading` early-return above has stopped firing, so
    // key this off `isLoading` rather than `[]` or it never creates the chart.
  }, [isLoading])

  useEffect(() => {
    if (!data || !equitySeriesRef.current || !ddSeriesRef.current) return
    const equityData = data.equity.map((p) => ({
      time: p.time as UTCTimestamp,
      value: p.equity,
    }))
    const ddData = data.equity.map((p) => ({
      time: p.time as UTCTimestamp,
      value: p.drawdown * 100,
    }))
    equitySeriesRef.current.setData(equityData)
    ddSeriesRef.current.setData(ddData)
    equityChartRef.current?.timeScale().fitContent()
    ddChartRef.current?.timeScale().fitContent()
  }, [data])

  const exitReasons = useMemo(() => {
    const rs = new Set(
      (data?.trades ?? [])
        .filter((t) => tradeSymbol === ALL_SYMBOLS || t.symbol === tradeSymbol)
        .map((t) => t.exit_reason),
    )
    return Array.from(rs).sort()
  }, [data?.trades, tradeSymbol])

  const filteredTrades = useMemo<Trade[]>(() => {
    return (data?.trades ?? []).filter(
      (t) =>
        (tradeSymbol === ALL_SYMBOLS || t.symbol === tradeSymbol) &&
        (exitReason === ALL_REASONS || t.exit_reason === exitReason),
    )
  }, [data?.trades, tradeSymbol, exitReason])

  const perSymbol = useMemo(() => symbolStats(data?.trades ?? []), [data?.trades])
  // long/short split — only meaningful when the strategy actually traded both
  const sideSplit = useMemo(() => {
    const all = data?.trades ?? []
    const longs = all.filter((t) => t.side !== 'short')
    const shorts = all.filter((t) => t.side === 'short')
    return longs.length && shorts.length
      ? [
          { side: 'Long', stats: statsFor(longs) },
          { side: 'Short', stats: statsFor(shorts) },
        ]
      : null
  }, [data?.trades])
  const filteredStats = useMemo(() => statsFor(filteredTrades), [filteredTrades])

  const [returnsBy, setReturnsBy] = useState<'month' | 'year'>('month')

  const [sortKey, setSortKey] = useState<TradeSortKey>('pnl')
  const [sortDesc, setSortDesc] = useState(true)
  const sortedTrades = useMemo<Trade[]>(() => {
    const arr = [...filteredTrades]
    arr.sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      const cmp =
        typeof av === 'string' && typeof bv === 'string' ? av.localeCompare(bv) : av > bv ? 1 : av < bv ? -1 : 0
      return sortDesc ? -cmp : cmp
    })
    return arr
  }, [filteredTrades, sortKey, sortDesc])

  function toggleSort(key: TradeSortKey) {
    if (key === sortKey) {
      setSortDesc((v) => !v)
    } else {
      setSortKey(key)
      setSortDesc(true)
    }
  }

  function sortableTh(key: TradeSortKey, label: string) {
    return (
      <th
        className="cursor-pointer select-none px-3 py-2 font-medium"
        onClick={() => toggleSort(key)}
      >
        {label} {sortKey === key ? (sortDesc ? '↓' : '↑') : ''}
      </th>
    )
  }

  if (isLoading) {
    return <p className="text-sm text-gray-500">Loading backtest…</p>
  }
  if (isError || !data) {
    return <p className="text-sm text-red-400">Could not load backtest.</p>
  }

  const metrics = data.metrics ?? {}
  // present only while trades are excluded — the same run recomputed with none
  // excluded, so metric deltas are like-for-like (see WhatIfResponse)
  const baselineMetrics =
    hiddenIds.length > 0 && whatIf.data ? whatIf.data.baseline_metrics : null
  const monthlyReturns = (metrics.monthly_returns as Record<string, number> | undefined) ?? null
  // older runs predate yearly_returns — fall back to folding the monthly table
  const yearlyReturns =
    (metrics.yearly_returns as Record<string, number> | undefined) ??
    (monthlyReturns ? foldMonthsToYears(monthlyReturns) : null)
  const periodReturns = returnsBy === 'year' ? yearlyReturns : monthlyReturns
  // Present only for an options backtest. Its presence is what switches this
  // page into "these prices were modelled" mode (docs/plan/16 D5).
  const optionsAssumptions =
    (data.assumptions?.options as OptionsAssumptions | undefined) ?? null
  const distributionWarning = metrics.return_distribution_warning as string | undefined

  return (
    <div>
      {state?.fromStudy && (
        <div className="mb-3 flex flex-wrap items-center gap-3 rounded border border-teal-400/30 bg-teal-400/5 px-3 py-2 text-xs text-teal-200">
          <span>
            This is the optimizer champion, re-run over the full history.
            {state.note ? ` ${state.note}` : ''}
          </span>
          <Link
            to={`/optimize/${state.fromStudy}`}
            className="underline hover:text-teal-100"
          >
            ← back to the study
          </Link>
        </div>
      )}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold text-gray-100">Backtest result</h1>
        {data.strategy_slug && (
          <Link
            to={`/strategies/${data.strategy_slug}`}
            title="Open this strategy in the editor"
            className="rounded-full border border-teal-400/40 bg-teal-400/10 px-3 py-1 text-xs text-teal-300 hover:bg-teal-400/20"
          >
            {data.strategy_name ?? data.strategy_slug} ↗
          </Link>
        )}
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 font-mono text-xs text-gray-400">
          {data.strategy_version_hash}
        </span>
        <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-gray-400">
          {data.status}
        </span>
      </div>

      <p className="mb-4 text-sm text-gray-400">
        {data.config.start} → {data.config.end} · initial capital{' '}
        {data.config.initial_capital.toLocaleString()} · slippage {data.config.slippage.value}bps ·
        commission ${data.config.commission.per_order}/order
      </p>

      {optionsAssumptions && (
        <SyntheticPricingBanner
          assumptions={optionsAssumptions}
          underlying={data.trades?.[0]?.symbol}
        />
      )}

      <CollapsiblePanel title="Assumptions" defaultOpen={false}>
        <pre className="overflow-x-auto rounded bg-white/5 p-3 text-xs text-gray-400">
          {JSON.stringify(data.assumptions, null, 2)}
        </pre>
      </CollapsiblePanel>

      {hiddenIds.length > 0 && (
        <section className="mb-4 rounded border border-amber-500/40 bg-amber-500/10 p-4">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-amber-200">
              {hiddenIds.length} trade{hiddenIds.length === 1 ? '' : 's'} temporarily excluded
              {whatIf.isFetching && ' — recalculating…'}
            </h2>
            <button
              onClick={() => setHidden(new Set())}
              className="rounded border border-amber-400/40 px-2 py-1 text-xs text-amber-200 hover:bg-amber-400/20"
            >
              Restore all
            </button>
          </div>
          <p className="mb-3 text-xs text-amber-200/80">
            Every metric, chart and export below is recalculated without{' '}
            {hiddenIds.length === 1 ? 'this trade' : 'these trades'}. Nothing is saved — reload
            the page to get the full result back. Approximate: the equity curve has each
            excluded trade's P&amp;L removed from that point on, but the run isn't re-simulated,
            so it can't show the freed capital funding a different trade.
          </p>
          {whatIf.isError && (
            <p className="mb-2 text-xs text-red-300">
              Could not recalculate — showing the full result.
            </p>
          )}
          <ul className="flex flex-col gap-1">
            {hiddenTrades.map((t) => (
              <li
                key={t.id}
                className="flex flex-wrap items-center gap-2 text-xs text-amber-100/90"
              >
                <span className="font-mono">{t.symbol}</span>
                <span>{new Date(t.entry_ts * 1000).toLocaleDateString()}</span>
                <span className={t.pnl >= 0 ? 'text-teal-300' : 'text-red-300'}>
                  {t.pnl >= 0 ? '+' : ''}
                  {t.pnl.toFixed(2)} ({t.pnl_pct.toFixed(2)}%)
                </span>
                <span className="text-amber-200/60">{t.exit_reason}</span>
                <button
                  onClick={() => restoreTrade(t.id)}
                  className="rounded border border-amber-400/40 px-2 py-0.5 hover:bg-amber-400/20"
                >
                  Restore
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {distributionWarning && (
        <p className="mb-2 rounded border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-xs leading-relaxed text-amber-200/90">
          {distributionWarning}
        </p>
      )}

      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-sm font-semibold text-gray-200">Metrics</h2>
        <Link
          to="/docs#metrics"
          title="Metric reference in Docs"
          className="text-xs text-gray-500 hover:text-teal-400"
        >
          ⓘ
        </Link>
      </div>
      <div className="my-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {Object.entries(METRIC_LABELS).map(([key, label]) => {
          const value = metrics[key]
          if (value == null) return null
          const num = typeof value === 'number' ? value : Number(value)
          if (Number.isNaN(num)) return null
          const colored = GREEN_GOOD.has(key)
          const color = colored ? (num >= 0 ? 'text-teal-400' : 'text-red-400') : 'text-gray-100'
          const suffix = PCT_KEYS.has(key) ? '%' : ''
          // While trades are excluded, show the movement against the SAME run
          // recomputed with nothing excluded — that difference is exactly "what
          // the excluded trades were worth".
          const baseRaw = baselineMetrics?.[key]
          const baseNum = typeof baseRaw === 'number' ? baseRaw : Number(baseRaw)
          const delta =
            baselineMetrics && baseRaw != null && !Number.isNaN(baseNum) ? num - baseNum : null
          return (
            <div key={key} className="rounded border border-white/10 p-3">
              <p className="mb-1 text-xs text-gray-500">{label}</p>
              <p className={`text-lg font-semibold ${color}`}>
                {num.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                {suffix}
              </p>
              {delta != null && Math.abs(delta) > 1e-9 && (
                <p
                  title="Change vs the same run with no trades excluded"
                  className={`text-xs ${delta > 0 ? 'text-teal-400/80' : 'text-red-400/80'}`}
                >
                  {delta > 0 ? '+' : ''}
                  {delta.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                  {suffix} vs full
                </p>
              )}
            </div>
          )
        })}
      </div>

      {hasPriceChart && (
        <section className="mb-6">
          <div className="mb-2 flex flex-wrap items-center gap-3">
            <h2 className="text-sm font-semibold text-gray-200">
              Price &amp; trades ({bar})
            </h2>
            {symbols.length > 1 ? (
              <div className="flex flex-wrap gap-1">
                {symbols.map((s) => {
                  const count = data.trades.filter((t) => t.symbol === s).length
                  const active = s === symbol
                  return (
                    <button
                      key={s}
                      onClick={() => setSelected(s)}
                      className={`rounded border px-2 py-1 text-xs ${
                        active
                          ? 'border-teal-400/50 bg-teal-400/10 text-teal-300'
                          : 'border-white/10 text-gray-400 hover:text-gray-200'
                      }`}
                    >
                      {s}
                      <span className="ml-1 text-gray-500">({count})</span>
                    </button>
                  )
                })}
              </div>
            ) : (
              <span className="text-sm text-gray-400">{symbol}</span>
            )}
          </div>
          <p className="mb-2 text-xs text-gray-500">
            Green up-arrows mark entries, arrows above bars mark exits (teal = winner, red =
            loser). Markers show only the selected symbol's trades; the number beside each
            symbol is its closed-trade count.
          </p>
          {candlesQuery.isError && (
            <p className="mb-2 text-sm text-red-400">Could not load price data for the chart.</p>
          )}
          <div className="h-[360px] rounded border border-white/10 bg-[#151412]">
            <div ref={priceRef} className="h-full w-full" />
          </div>
        </section>
      )}

      <section className="mb-3">
        <h2 className="mb-2 text-sm font-semibold text-gray-200">Equity</h2>
        <div className="h-[300px] rounded border border-white/10 bg-[#151412]">
          <div ref={equityRef} className="h-full w-full" />
        </div>
      </section>

      <section className="mb-6">
        <h2 className="mb-2 text-sm font-semibold text-gray-200">Drawdown</h2>
        <div className="h-[120px] rounded border border-white/10 bg-[#151412]">
          <div ref={ddRef} className="h-full w-full" />
        </div>
      </section>

      {periodReturns && (
        <section className="mb-6">
          <div className="mb-2 flex items-center gap-3">
            <h2 className="text-sm font-semibold text-gray-200">
              {returnsBy === 'year' ? 'Yearly returns' : 'Monthly returns'}
            </h2>
            {/* only worth offering when the window actually spans more than one year */}
            {yearlyReturns && Object.keys(yearlyReturns).length > 1 && (
              <div className="flex overflow-hidden rounded border border-white/10 text-xs">
                {(['month', 'year'] as const).map((by) => (
                  <button
                    key={by}
                    type="button"
                    onClick={() => setReturnsBy(by)}
                    className={
                      returnsBy === by
                        ? 'bg-teal-400/20 px-2 py-1 text-teal-300'
                        : 'px-2 py-1 text-gray-400 hover:bg-white/5'
                    }
                  >
                    by {by}
                  </button>
                ))}
              </div>
            )}
          </div>
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-6 lg:grid-cols-8">
            {Object.entries(periodReturns).map(([period, pct]) => (
              <div
                key={period}
                className="rounded border border-white/10 p-2 text-center text-xs"
              >
                <p className="mb-1 text-gray-500">
                  {returnsBy === 'year' ? period.slice(0, 4) : period}
                </p>
                <p className={pct >= 0 ? 'text-teal-400' : 'text-red-400'}>
                  {pct.toFixed(2)}%
                </p>
              </div>
            ))}
          </div>
        </section>
      )}

      {data.open_positions.length > 0 && (
        <section className="mb-6">
          <h2 className="mb-2 text-sm font-semibold text-gray-200">
            Open positions at end of window
          </h2>
          <p className="mb-2 text-xs text-gray-500">
            Still open when the backtest window ended — not a closed round trip, so it
            won't appear in the Trades table below, but it IS marked-to-market into the
            equity curve above (affecting Sharpe, return %, drawdown, etc).{' '}
            <Link to="/docs#no-trade" className="text-gray-400 underline hover:text-teal-400">
              ⓘ more on why didn't it trade
            </Link>
          </p>
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Symbol</th>
                  <th className="px-3 py-2 font-medium">Entry time</th>
                  <th className="px-3 py-2 font-medium">Entry px</th>
                  <th className="px-3 py-2 font-medium">Last px</th>
                  <th className="px-3 py-2 font-medium">Qty</th>
                  <th className="px-3 py-2 font-medium">Unrealized P&L</th>
                  <th className="px-3 py-2 font-medium">Unrealized P&L %</th>
                  <th className="px-3 py-2 font-medium">Bars held</th>
                </tr>
              </thead>
              <tbody>
                {data.open_positions.map((p) => (
                  <tr key={p.symbol} className="border-t border-white/10">
                    <td className="px-3 py-2 text-gray-300">{p.symbol}</td>
                    <td className="px-3 py-2 text-gray-300">
                      {new Date(p.entry_ts * 1000).toLocaleString()}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{p.entry_px.toFixed(2)}</td>
                    <td className="px-3 py-2 text-gray-300">{p.last_px.toFixed(2)}</td>
                    <td className="px-3 py-2 text-gray-300">{p.qty}</td>
                    <td
                      className={`px-3 py-2 ${p.unrealized_pnl >= 0 ? 'text-teal-400' : 'text-red-400'}`}
                    >
                      {p.unrealized_pnl.toFixed(2)}
                    </td>
                    <td className="px-3 py-2 text-gray-300">
                      {p.unrealized_pnl_pct.toFixed(2)}%
                    </td>
                    <td className="px-3 py-2 text-gray-300">{p.bars_held}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {perSymbol.length > 1 && (
        <section className="mb-6">
          {sideSplit && (
            <div className="mb-6">
              <h2 className="mb-2 text-sm font-semibold text-gray-200">
                Performance by side
              </h2>
              <div className="overflow-x-auto rounded border border-white/10">
                <table className="w-full text-left text-sm">
                  <thead className="bg-white/5 text-gray-400">
                    <tr>
                      <th className="px-3 py-2 font-medium">Side</th>
                      <th className="px-3 py-2 font-medium">Trades</th>
                      <th className="px-3 py-2 font-medium">Win rate</th>
                      <th className="px-3 py-2 font-medium">Total P&L</th>
                      <th className="px-3 py-2 font-medium">Avg P&L</th>
                      <th className="px-3 py-2 font-medium">Profit factor</th>
                      <th className="px-3 py-2 font-medium">Avg bars held</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sideSplit.map(({ side, stats }) => (
                      <tr key={side} className="border-t border-white/10">
                        <td className="px-3 py-2">
                          <span
                            className={`rounded px-1.5 py-0.5 text-xs font-medium ${
                              side === 'Short'
                                ? 'bg-orange-400/15 text-orange-300'
                                : 'bg-teal-400/15 text-teal-300'
                            }`}
                          >
                            {side.toUpperCase()}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-gray-300">{stats.count}</td>
                        <td className="px-3 py-2 text-gray-300">{stats.winRate.toFixed(1)}%</td>
                        <td
                          className={`px-3 py-2 ${stats.totalPnl >= 0 ? 'text-teal-400' : 'text-red-400'}`}
                        >
                          {stats.totalPnl.toFixed(2)}
                        </td>
                        <td className="px-3 py-2 text-gray-300">{stats.avgPnl.toFixed(2)}</td>
                        <td className="px-3 py-2 text-gray-300">{fmtPf(stats.profitFactor)}</td>
                        <td className="px-3 py-2 text-gray-300">{stats.avgBarsHeld.toFixed(1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <h2 className="mb-2 text-sm font-semibold text-gray-200">Performance by symbol</h2>
          <p className="mb-2 text-xs text-gray-500">
            Closed trades only, biggest total P&L first — click a row to filter the trade log
            and chart it above. Profit factor is gross wins ÷ gross losses (∞ = no losers).
          </p>
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Symbol</th>
                  <th className="px-3 py-2 font-medium">Trades</th>
                  <th className="px-3 py-2 font-medium">Win rate</th>
                  <th className="px-3 py-2 font-medium">Total P&L</th>
                  <th className="px-3 py-2 font-medium">Avg P&L</th>
                  <th className="px-3 py-2 font-medium">Avg P&L %</th>
                  <th className="px-3 py-2 font-medium">Profit factor</th>
                  <th className="px-3 py-2 font-medium">Best</th>
                  <th className="px-3 py-2 font-medium">Worst</th>
                  <th className="px-3 py-2 font-medium">Avg bars held</th>
                </tr>
              </thead>
              <tbody>
                {perSymbol.map(({ symbol: sym, stats }) => (
                  <tr
                    key={sym}
                    onClick={() => {
                      setTradeSymbol(sym)
                      setExitReason(ALL_REASONS)
                      setSelected(sym)
                    }}
                    className={`cursor-pointer border-t border-white/10 hover:bg-white/5 ${
                      tradeSymbol === sym ? 'bg-teal-400/10' : ''
                    }`}
                  >
                    <td className="px-3 py-2 font-medium text-gray-200">{sym}</td>
                    <td className="px-3 py-2 text-gray-300">{stats.count}</td>
                    <td className="px-3 py-2 text-gray-300">{stats.winRate.toFixed(1)}%</td>
                    <td
                      className={`px-3 py-2 ${stats.totalPnl >= 0 ? 'text-teal-400' : 'text-red-400'}`}
                    >
                      {stats.totalPnl.toFixed(2)}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{stats.avgPnl.toFixed(2)}</td>
                    <td className="px-3 py-2 text-gray-300">{stats.avgPnlPct.toFixed(2)}%</td>
                    <td className="px-3 py-2 text-gray-300">{fmtPf(stats.profitFactor)}</td>
                    <td className="px-3 py-2 text-teal-400">{stats.bestPnl.toFixed(2)}</td>
                    <td className="px-3 py-2 text-red-400">{stats.worstPnl.toFixed(2)}</td>
                    <td className="px-3 py-2 text-gray-300">{stats.avgBarsHeld.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section>
        <h2 className="mb-2 text-sm font-semibold text-gray-200">Trades</h2>
        {data.trades.length > 0 && (
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {/* one traded symbol -> the picker would be a no-op; the exit-reason
                filter, summary line and CSV export still apply. */}
            <div className="flex flex-wrap gap-1">
              {(perSymbol.length > 1 ? [ALL_SYMBOLS, ...perSymbol.map((s) => s.symbol)] : []).map((sym) => (
                <button
                  key={sym}
                  onClick={() => {
                    setTradeSymbol(sym)
                    setExitReason(ALL_REASONS)
                    if (sym !== ALL_SYMBOLS) setSelected(sym)
                  }}
                  className={`rounded border px-2 py-1 text-xs ${
                    tradeSymbol === sym
                      ? 'border-teal-400/50 bg-teal-400/10 text-teal-300'
                      : 'border-white/10 text-gray-400 hover:text-gray-200'
                  }`}
                >
                  {sym === ALL_SYMBOLS ? 'All' : sym}
                </button>
              ))}
            </div>
            {exitReasons.length > 1 && (
              <select
                value={exitReason}
                onChange={(e) => setExitReason(e.target.value)}
                className="rounded border border-white/10 bg-transparent px-2 py-1 text-xs text-gray-300"
              >
                <option value={ALL_REASONS}>All exit reasons</option>
                {exitReasons.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            )}
            <button
              onClick={() =>
                downloadCsv(
                  `backtest-${data.id.slice(0, 8)}-${
                    tradeSymbol === ALL_SYMBOLS ? 'all' : tradeSymbol
                  }-trades.csv`,
                  tradesToCsv(sortedTrades),
                )
              }
              disabled={sortedTrades.length === 0}
              className="rounded border border-white/10 px-2 py-1 text-xs text-gray-400 hover:text-gray-200 disabled:opacity-40"
            >
              Export CSV
            </button>
          </div>
        )}
        {filteredTrades.length > 0 && (
          <p className="mb-2 text-xs text-gray-400">
            {filteredStats.count} trade{filteredStats.count === 1 ? '' : 's'} ·{' '}
            <span className={filteredStats.totalPnl >= 0 ? 'text-teal-400' : 'text-red-400'}>
              {filteredStats.totalPnl >= 0 ? '+' : ''}
              {filteredStats.totalPnl.toFixed(2)}
            </span>{' '}
            total P&L · {filteredStats.wins}W / {filteredStats.losses}L (
            {filteredStats.winRate.toFixed(1)}% win rate) · avg{' '}
            {filteredStats.avgPnl.toFixed(2)} ({filteredStats.avgPnlPct.toFixed(2)}%) · profit
            factor {fmtPf(filteredStats.profitFactor)}
          </p>
        )}
        {data.trades.length > 0 && filteredTrades.length === 0 && (
          <p className="text-sm text-gray-600">No trades match this filter.</p>
        )}
        {data.trades.length === 0 && data.open_positions.length === 0 && (
          <p className="text-sm text-gray-600">No trades.</p>
        )}
        {data.trades.length === 0 && data.open_positions.length > 0 && (
          <p className="text-sm text-gray-600">
            No closed trades — the only position taken during this window is still open
            (see "Open positions at end of window" above).
          </p>
        )}
        {sortedTrades.length > 0 && (
          <div className="overflow-x-auto rounded border border-white/10">
            <table className="w-full text-left text-sm">
              <thead className="bg-white/5 text-gray-400">
                <tr>
                  <th className="px-3 py-2 font-medium">Symbol</th>
                  <th className="px-3 py-2 font-medium">Side</th>
                  {sortableTh('entry_ts', 'Entry time')}
                  {sortableTh('entry_px', 'Entry px')}
                  {sortableTh('exit_ts', 'Exit time')}
                  {sortableTh('exit_px', 'Exit px')}
                  <th className="px-3 py-2 font-medium">Qty</th>
                  {sortableTh('pnl', 'P&L')}
                  <th className="px-3 py-2 font-medium">P&L %</th>
                  <th className="px-3 py-2 font-medium">MAE</th>
                  <th className="px-3 py-2 font-medium">MFE</th>
                  <th className="px-3 py-2 font-medium">Bars held</th>
                  {sortableTh('exit_reason', 'Exit reason')}
                  <th className="px-3 py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {sortedTrades.map((t) => (
                  <tr key={t.id} className="border-t border-white/10">
                    <td className="px-3 py-2 text-gray-300">{t.symbol}</td>
                    <td className="px-3 py-2">
                      <span
                        className={`rounded px-1.5 py-0.5 text-xs font-medium ${
                          t.side === 'short'
                            ? 'bg-orange-400/15 text-orange-300'
                            : 'bg-teal-400/15 text-teal-300'
                        }`}
                      >
                        {t.side === 'short' ? 'SHORT' : 'LONG'}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-gray-300">
                      {new Date(t.entry_ts * 1000).toLocaleString()}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{t.entry_px.toFixed(2)}</td>
                    <td className="px-3 py-2 text-gray-300">
                      {new Date(t.exit_ts * 1000).toLocaleString()}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{t.exit_px.toFixed(2)}</td>
                    <td className="px-3 py-2 text-gray-300">{t.qty}</td>
                    <td className={`px-3 py-2 ${t.pnl >= 0 ? 'text-teal-400' : 'text-red-400'}`}>
                      {t.pnl.toFixed(2)}
                    </td>
                    <td className="px-3 py-2 text-gray-300">{t.pnl_pct.toFixed(2)}%</td>
                    <td className="px-3 py-2 text-gray-300">{t.mae.toFixed(2)}</td>
                    <td className="px-3 py-2 text-gray-300">{t.mfe.toFixed(2)}</td>
                    <td className="px-3 py-2 text-gray-300">{t.bars_held}</td>
                    <td className="px-3 py-2 text-gray-300">{t.exit_reason}</td>
                    <td className="px-3 py-2">
                      <button
                        onClick={() => hideTrade(t.id)}
                        title="Temporarily exclude this trade and recalculate every stat without it (not saved)"
                        className="rounded border border-white/10 px-2 py-0.5 text-xs text-gray-400 hover:border-amber-400/40 hover:text-amber-300"
                      >
                        Exclude
                      </button>
                    </td>
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
