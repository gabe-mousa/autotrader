import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { getCandles, ApiError, type Timeframe } from '../lib/api'
import { useTheme, chartTheme } from '../lib/theme'

const TIMEFRAMES: Timeframe[] = ['1m', '5m', '15m', '30m', '1h', '1d']

const DAY_MS = 86_400_000
// Date-range presets. Intraday bars only exist ~6 months back (Schwab lookback
// + whatever we've recorded), so wider presets are daily-only.
const RANGES = [
  { label: '1D', ms: 1 * DAY_MS, intraday: true },
  { label: '5D', ms: 5 * DAY_MS, intraday: true },
  { label: '1M', ms: 31 * DAY_MS, intraday: true },
  { label: '3M', ms: 93 * DAY_MS, intraday: true },
  { label: '6M', ms: 200 * DAY_MS, intraday: true },
  { label: '1Y', ms: 366 * DAY_MS, intraday: false },
  { label: '5Y', ms: 5 * 366 * DAY_MS, intraday: false },
  { label: 'Max', ms: 21 * 366 * DAY_MS, intraday: false },
] as const
type RangeLabel = (typeof RANGES)[number]['label']

function rangesFor(tf: Timeframe) {
  return tf === '1d' ? RANGES : RANGES.filter((r) => r.intraday)
}

function defaultRange(tf: Timeframe): RangeLabel {
  return tf === '1d' ? '1Y' : tf === '1m' ? '5D' : '1M'
}

const SYMBOL_KEY = 'charts.symbol'
const TF_KEY = 'charts.tf'
const RANGE_KEY = 'charts.range'

export default function Charts() {
  const { theme } = useTheme()
  const [symbolInput, setSymbolInput] = useState(
    () => localStorage.getItem(SYMBOL_KEY) ?? 'SPY',
  )
  const [symbol, setSymbol] = useState(() => localStorage.getItem(SYMBOL_KEY) ?? 'SPY')
  const [tf, setTf] = useState<Timeframe>(
    () => (localStorage.getItem(TF_KEY) as Timeframe | null) ?? '1d',
  )
  const [range, setRange] = useState<RangeLabel>(() => {
    const saved = localStorage.getItem(RANGE_KEY) as RangeLabel | null
    return saved && RANGES.some((r) => r.label === saved) ? saved : defaultRange('1d')
  })

  useEffect(() => {
    localStorage.setItem(SYMBOL_KEY, symbol)
  }, [symbol])

  useEffect(() => {
    localStorage.setItem(TF_KEY, tf)
  }, [tf])

  useEffect(() => {
    localStorage.setItem(RANGE_KEY, range)
  }, [range])

  // switching to an intraday tf while a daily-only range is active: snap back
  function selectTf(next: Timeframe) {
    setTf(next)
    if (!rangesFor(next).some((r) => r.label === range)) {
      setRange(defaultRange(next))
    }
  }

  const rangeMs = RANGES.find((r) => r.label === range)?.ms ?? 366 * DAY_MS
  const start = Date.now() - rangeMs

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['candles', symbol, tf, range],
    queryFn: () => getCandles(symbol, tf, start),
    retry: false,
    enabled: symbol.length > 0,
  })

  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)

  // Create the chart once.
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const chart = createChart(container, {
      ...chartTheme(theme),
      width: container.clientWidth,
      height: container.clientHeight,
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
      },
    })

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a',
      downColor: '#ef5350',
      borderUpColor: '#26a69a',
      borderDownColor: '#ef5350',
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350',
    })

    const volumeSeries = chart.addSeries(HistogramSeries, {
      color: '#26a69a',
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    })
    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    })

    chartRef.current = chart
    candleSeriesRef.current = candleSeries
    volumeSeriesRef.current = volumeSeries

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
      chartRef.current = null
      candleSeriesRef.current = null
      volumeSeriesRef.current = null
    }
  }, [])

  // Re-theme the chart canvas when light/dark toggles (it can't pick up
  // the CSS remap since lightweight-charts paints its own colors).
  useEffect(() => {
    chartRef.current?.applyOptions(chartTheme(theme))
  }, [theme])

  // Push new data into the series.
  useEffect(() => {
    if (!data || !candleSeriesRef.current || !volumeSeriesRef.current) return

    const candleData = data.candles.map((c) => ({
      time: c.time as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }))
    const volumeData = data.candles.map((c) => ({
      time: c.time as UTCTimestamp,
      value: c.volume,
      color: c.close >= c.open ? '#26a69a80' : '#ef535080',
    }))

    candleSeriesRef.current.setData(candleData)
    volumeSeriesRef.current.setData(volumeData)
    chartRef.current?.timeScale().fitContent()
  }, [data])

  const needsAuth = isError && error instanceof ApiError && error.status === 424

  function submitSymbol() {
    const trimmed = symbolInput.trim().toUpperCase()
    if (trimmed.length > 0) {
      setSymbol(trimmed)
      setSymbolInput(trimmed)
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <h1 className="mb-4 text-xl font-semibold text-gray-100">Charts</h1>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <input
          value={symbolInput}
          onChange={(e) => setSymbolInput(e.target.value.toUpperCase())}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submitSymbol()
          }}
          placeholder="Symbol"
          className="w-28 rounded border border-white/10 bg-white/5 px-3 py-1.5 text-sm text-gray-200 placeholder:text-gray-600 focus:border-emerald-500 focus:outline-none"
        />

        <div className="flex gap-1 rounded border border-white/10 p-1">
          {TIMEFRAMES.map((option) => (
            <button
              key={option}
              onClick={() => selectTf(option)}
              title="Bar size"
              className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                tf === option
                  ? 'bg-emerald-600 text-white'
                  : 'text-gray-400 hover:bg-white/5 hover:text-gray-200'
              }`}
            >
              {option}
            </button>
          ))}
        </div>

        <div className="flex gap-1 rounded border border-white/10 p-1">
          {rangesFor(tf).map((r) => (
            <button
              key={r.label}
              onClick={() => setRange(r.label)}
              title="Date range"
              className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                range === r.label
                  ? 'bg-teal-600 text-white'
                  : 'text-gray-400 hover:bg-white/5 hover:text-gray-200'
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>

        {isLoading && <span className="text-sm text-gray-500">Loading…</span>}
      </div>

      {isError && (
        <p className="mb-4 text-sm text-red-400">
          {needsAuth
            ? 'Connect Schwab in Settings first.'
            : error instanceof Error
              ? error.message
              : 'Failed to load candles.'}
        </p>
      )}

      <div className="min-h-0 flex-1 rounded border border-white/10 bg-[#151412]">
        <div ref={containerRef} className="h-full w-full" />
      </div>
    </div>
  )
}
