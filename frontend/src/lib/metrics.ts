// Shared backtest-metric display metadata — used by BacktestResult.tsx's
// metric-tile grid and PromoteToLive.tsx's backtest-vs-paper comparison, so
// both pages agree on labels/formatting for the same metric keys.

export const METRIC_LABELS: Record<string, string> = {
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

export const PCT_KEYS = new Set([
  'total_return_pct',
  'cagr_pct',
  'max_drawdown_pct',
  'win_rate_pct',
  'expectancy',
])

export const GREEN_GOOD = new Set([
  'total_return_pct',
  'cagr_pct',
  'sharpe',
  'sortino',
  'calmar',
  'win_rate_pct',
  'profit_factor',
  'expectancy',
])
