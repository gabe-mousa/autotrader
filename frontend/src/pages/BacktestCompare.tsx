import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { createChart, LineSeries, type IChartApi, type ISeriesApi, type UTCTimestamp } from 'lightweight-charts'
import { getBacktestsCompare, type BacktestDetailResponse } from '../lib/api'
import { useTheme, chartTheme } from '../lib/theme'

const METRIC_LABELS: Record<string, string> = {
  total_return_pct: 'Total return %',
  cagr_pct: 'CAGR %',
  sharpe: 'Sharpe',
  sortino: 'Sortino',
  max_drawdown_pct: 'Max drawdown %',
  calmar: 'Calmar',
  win_rate_pct: 'Win rate %',
  profit_factor: 'Profit factor',
  expectancy: 'Expectancy',
  trade_count: 'Trades',
  exposure_pct: 'Exposure %',
  avg_bars_held: 'Avg bars held',
  open_positions_at_end: 'Open at end',
}

const PCT_KEYS = new Set(['total_return_pct', 'cagr_pct', 'max_drawdown_pct', 'win_rate_pct', 'expectancy'])

const LINE_COLORS = ['#2dd4bf', '#818cf8', '#f472b6', '#fbbf24']

function fmtMetric(key: string, value: unknown): string {
  if (value == null) return '—'
  const num = typeof value === 'number' ? value : Number(value)
  if (Number.isNaN(num)) return '—'
  const suffix = PCT_KEYS.has(key) ? '%' : ''
  return `${num.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`
}

export default function BacktestCompare() {
  const { theme } = useTheme()
  const [searchParams] = useSearchParams()
  const ids = (searchParams.get('ids') ?? '').split(',').filter((s) => s.length > 0)

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['backtests-compare', ids],
    queryFn: () => getBacktestsCompare(ids),
    enabled: ids.length >= 2 && ids.length <= 4,
    retry: false,
  })

  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRefs = useRef<ISeriesApi<'Line'>[]>([])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const chart = createChart(container, {
      ...chartTheme(theme),
      width: container.clientWidth,
      height: container.clientHeight,
      timeScale: { timeVisible: true, secondsVisible: false },
    })
    chartRef.current = chart

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
      seriesRefs.current = []
    }
    // containerRef only mounts once `data` arrives (see the `{data && (...)}`
    // guard below) — on the very first render `data` is still undefined, so
    // a `[]`-only effect would see a null container forever and never create
    // the chart. Re-run when the container actually appears. Depend on the
    // boolean, not `data` itself — `data` is a new object on every refetch
    // and would otherwise tear down/recreate the chart unnecessarily.
  }, [!!data])

  useEffect(() => {
    chartRef.current?.applyOptions(chartTheme(theme))
  }, [theme])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !data) return
    // clear any series from a previous fetch (id list can change via URL)
    for (const s of seriesRefs.current) chart.removeSeries(s)
    seriesRefs.current = []

    data.backtests.forEach((bt: BacktestDetailResponse, i: number) => {
      const series = chart.addSeries(LineSeries, {
        color: LINE_COLORS[i % LINE_COLORS.length],
        lineWidth: 2,
        title: bt.id.slice(0, 8),
      })
      series.setData(bt.equity.map((p) => ({ time: p.time as UTCTimestamp, value: p.equity })))
      seriesRefs.current.push(series)
    })
    chart.timeScale().fitContent()
  }, [data])

  if (ids.length < 2 || ids.length > 4) {
    return (
      <p className="text-sm text-red-400">
        Compare needs 2-4 backtest ids in the URL, e.g. /backtests/compare?ids=a,b,c
      </p>
    )
  }

  return (
    <div>
      <h1 className="mb-4 text-xl font-semibold text-gray-100">Compare backtests</h1>

      {isLoading && <p className="text-sm text-gray-500">Loading…</p>}
      {isError && (
        <p className="text-sm text-red-400">
          {error instanceof Error ? error.message : 'Could not load backtests to compare.'}
        </p>
      )}

      {data && (
        <>
          <section className="mb-6">
            <h2 className="mb-2 text-sm font-semibold text-gray-200">Equity (overlaid)</h2>
            <div className="h-[360px] rounded border border-white/10 bg-[#151412]">
              <div ref={containerRef} className="h-full w-full" />
            </div>
            <div className="mt-2 flex flex-wrap gap-4 text-xs text-gray-400">
              {data.backtests.map((bt, i) => (
                <span key={bt.id} className="flex items-center gap-1.5">
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ backgroundColor: LINE_COLORS[i % LINE_COLORS.length] }}
                  />
                  {bt.id.slice(0, 8)} · {bt.config.start} → {bt.config.end}
                </span>
              ))}
            </div>
          </section>

          <section>
            <h2 className="mb-2 text-sm font-semibold text-gray-200">Metrics</h2>
            <div className="overflow-x-auto rounded border border-white/10">
              <table className="w-full text-left text-sm">
                <thead className="bg-white/5 text-gray-400">
                  <tr>
                    <th className="px-3 py-2 font-medium">Metric</th>
                    {data.backtests.map((bt) => (
                      <th key={bt.id} className="px-3 py-2 font-medium">
                        <Link to={`/backtests/${bt.id}`} className="hover:text-emerald-300">
                          {bt.id.slice(0, 8)}
                        </Link>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(METRIC_LABELS).map(([key, label]) => {
                    const anyPresent = data.backtests.some((bt) => bt.metrics[key] != null)
                    if (!anyPresent) return null
                    return (
                      <tr key={key} className="border-t border-white/10">
                        <td className="px-3 py-2 text-gray-400">{label}</td>
                        {data.backtests.map((bt) => (
                          <td key={bt.id} className="px-3 py-2 text-gray-200">
                            {fmtMetric(key, bt.metrics[key])}
                          </td>
                        ))}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  )
}
