// Minimal typed fetch helpers for the autotrader backend API.
// All requests go through the Vite dev proxy at /api.

import type {
  AIResult,
  CatalogResponse,
  CreateStrategyResponse,
  StrategyDetailResponse,
  StrategyDocument,
  StrategyListResponse,
  UpdateStrategyResponse,
  ValidateResponse,
} from './strategy'

export interface HealthResponse {
  status: string
  auth: {
    connected: boolean
    access_token_expires_in: number | null
    refresh_token_expires_in: number | null
  }
  streamer: string
  ai_configured?: boolean
}

export interface AuthStatusResponse {
  connected: boolean
  access_token_expires_in: number | null
  refresh_token_expires_in: number | null
  refresh_token_expires_at: string | null
  last_refresh_at: string | null
  client_configured: boolean
}

export interface ConnectResponse {
  authorize_url: string
}

export interface Account {
  account_number_masked: string
  type: string
  equity: number
  cash: number
  buying_power: number
}

export interface AccountsResponse {
  accounts: Account[]
}

export class ApiError extends Error {
  status: number
  // The raw `detail` payload when the server sent a structured one. The
  // optimizer's "not enough data" refusal (422) carries the coverage it
  // actually found, which is the whole point of that error — a bare string
  // would drop the numbers the user needs to decide what to backfill.
  detail?: unknown
  constructor(message: string, status: number, detail?: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) {
    let detail = res.statusText
    let raw: unknown
    try {
      const body = await res.json()
      raw = body?.detail
      if (body && typeof body.detail === 'string') detail = body.detail
      else if (raw && typeof raw === 'object' && typeof (raw as { error?: unknown }).error === 'string') {
        detail = (raw as { error: string }).error
      }
    } catch {
      // ignore body parse errors
    }
    throw new ApiError(detail, res.status, raw)
  }
  return res.json() as Promise<T>
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health')
}

export function getAuthStatus(): Promise<AuthStatusResponse> {
  return request<AuthStatusResponse>('/api/auth/status')
}

export function connectSchwab(): Promise<ConnectResponse> {
  return request<ConnectResponse>('/api/auth/connect', { method: 'POST' })
}

export function getAccounts(): Promise<AccountsResponse> {
  return request<AccountsResponse>('/api/accounts')
}

export type Timeframe = '1m' | '5m' | '15m' | '30m' | '1h' | '1d'

export interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface CandlesResponse {
  symbol: string
  tf: Timeframe
  candles: Candle[]
}

export function getCandles(
  symbol: string,
  tf: Timeframe,
  start?: number,
  end?: number,
): Promise<CandlesResponse> {
  const params = new URLSearchParams({ symbol, tf })
  if (start != null) params.set('start', String(start))
  if (end != null) params.set('end', String(end))
  return request<CandlesResponse>(`/api/candles?${params.toString()}`)
}

export interface SymbolCoverage {
  first_ts: number
  last_ts: number
  bars: number
}

/** Timeframes stored natively and reported in coverage, fastest first. */
export const COVERAGE_TFS = ['1m', '5m', '15m', '30m', '1d'] as const
export type CoverageTf = (typeof COVERAGE_TFS)[number]

export type CoverageEntry = { symbol: string } & Record<
  CoverageTf,
  SymbolCoverage | null
>

export interface CoverageResponse {
  coverage: CoverageEntry[]
  recorded_bars_this_session: number
}

export function getCoverage(): Promise<CoverageResponse> {
  return request<CoverageResponse>('/api/data/coverage')
}

export interface BackfillResponse {
  symbol: string
  daily_bars_fetched: number
  minute_bars_fetched: number
  /** native Schwab intraday bars fetched, keyed by tf (e.g. { '30m': 2288 }) */
  intraday_bars_fetched: Record<string, number>
  intraday_bars_total: number
  coverage: Record<CoverageTf, SymbolCoverage | null>
}

export function postBackfill(symbol: string): Promise<BackfillResponse> {
  return request<BackfillResponse>('/api/data/backfill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol }),
  })
}

export interface WatchlistResponse {
  symbols: string[]
}

export function getWatchlist(): Promise<WatchlistResponse> {
  return request<WatchlistResponse>('/api/watchlist')
}

export function putWatchlist(symbols: string[]): Promise<WatchlistResponse> {
  return request<WatchlistResponse>('/api/watchlist', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbols }),
  })
}

export function getCatalog(): Promise<CatalogResponse> {
  return request<CatalogResponse>('/api/catalog')
}

export function getStrategies(opts?: { archived?: boolean }): Promise<StrategyListResponse> {
  const qs = opts?.archived ? '?archived=true' : ''
  return request<StrategyListResponse>(`/api/strategies${qs}`)
}

export function getStrategy(slug: string): Promise<StrategyDetailResponse> {
  return request<StrategyDetailResponse>(`/api/strategies/${slug}`)
}

export interface ArchiveStrategyResponse {
  slug: string
  archived: boolean
}

export function setStrategyArchived(
  slug: string,
  archived: boolean,
): Promise<ArchiveStrategyResponse> {
  return request<ArchiveStrategyResponse>(`/api/strategies/${slug}/archive`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ archived }),
  })
}

export interface StarStrategyResponse {
  slug: string
  starred: boolean
}

export function setStrategyStarred(
  slug: string,
  starred: boolean,
): Promise<StarStrategyResponse> {
  return request<StarStrategyResponse>(`/api/strategies/${slug}/star`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ starred }),
  })
}

export function validateStrategy(document: StrategyDocument): Promise<ValidateResponse> {
  return request<ValidateResponse>('/api/strategies/validate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document }),
  })
}

export function createStrategy(document: StrategyDocument): Promise<CreateStrategyResponse> {
  return request<CreateStrategyResponse>('/api/strategies', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document }),
  })
}

export function updateStrategy(
  slug: string,
  document: StrategyDocument,
): Promise<UpdateStrategyResponse> {
  return request<UpdateStrategyResponse>(`/api/strategies/${slug}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ document }),
  })
}

export interface BacktestConfig {
  start: string
  end: string
  initial_capital: number
  slippage: { type: 'fixed_bps'; value: number }
  commission: { per_order: number }
  fill_on_touch: boolean
}

export interface BacktestSummary {
  total_return_pct: number | null
  sharpe: number | null
  max_drawdown_pct: number | null
  win_rate_pct: number | null
  trade_count: number | null
}

export interface BacktestListItem {
  id: string
  hash: string
  strategy_name: string | null
  strategy_slug: string | null
  // set when this run was one combination of a parameter sweep; sweep_combo
  // holds that combination's lever values.
  sweep_id: string | null
  sweep_combo: Record<string, LeverValue> | null
  status: string
  started_at: string
  finished_at: string | null
  config: BacktestConfig
  summary: BacktestSummary | null
}

export interface BacktestListResponse {
  backtests: BacktestListItem[]
}

export interface CreateBacktestResponse {
  id: string
  strategy_version_hash: string
  metrics: Record<string, unknown>
  assumptions: Record<string, unknown>
  trade_count: number
}

export interface EquityPoint {
  time: number
  equity: number
  drawdown: number
}

export interface Trade {
  // backtest_trades.id is an INTEGER PRIMARY KEY — it arrives as a number,
  // and the what-if endpoint keys off these ids.
  id: number
  symbol: string
  side: string
  entry_ts: number
  entry_px: number
  exit_ts: number
  exit_px: number
  qty: number
  pnl: number
  pnl_pct: number
  mae: number
  mfe: number
  bars_held: number
  exit_reason: string
}

export interface OpenPosition {
  symbol: string
  qty: number
  entry_ts: number
  entry_px: number
  last_px: number
  unrealized_pnl: number
  unrealized_pnl_pct: number
  bars_held: number
  as_of_ts: number | null
}

export interface BacktestDetailResponse {
  id: string
  strategy_version_hash: string
  status: string
  // first symbol/bar of the strategy's universe/timeframe — null if the
  // frozen strategy snapshot can't be located (e.g. sweep-generated combos,
  // which are never frozen as their own strategy_versions entry).
  symbol: string | null
  // every symbol the run could have traded (universe ∪ traded symbols) —
  // selectable in the price & trades chart.
  symbols: string[]
  bar: Timeframe | null
  // slug/name of the strategy this ran against, for linking to its editor;
  // null when the version index has no row for the hash.
  strategy_slug: string | null
  strategy_name: string | null
  config: BacktestConfig
  metrics: Record<string, unknown>
  assumptions: Record<string, unknown>
  equity: EquityPoint[]
  trades: Trade[]
  open_positions: OpenPosition[]
}

export function postBacktest(
  slug: string,
  config: BacktestConfig,
): Promise<CreateBacktestResponse> {
  return request<CreateBacktestResponse>('/api/backtests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slug, config }),
  })
}

export function getBacktests(): Promise<BacktestListResponse> {
  return request<BacktestListResponse>('/api/backtests')
}

export function getBacktest(id: string): Promise<BacktestDetailResponse> {
  return request<BacktestDetailResponse>(`/api/backtests/${id}`)
}

/**
 * Metrics recomputed as if some trades had never been taken. Non-destructive:
 * the server writes nothing, so reloading the page restores the full result.
 * `approximate` is always true — the equity curve is adjusted by removing each
 * excluded trade's realized PnL, not re-simulated, so it can't model the
 * freed capital funding a different trade.
 */
export interface WhatIfResponse {
  id: string
  approximate: boolean
  excluded_trade_ids: number[]
  excluded_pnl: number
  metrics: Record<string, unknown>
  /**
   * The same run with NOTHING excluded, computed through the identical path.
   * Compare against this rather than the stored metrics: some older runs have
   * persisted metrics that disagree with their own persisted equity, which
   * would otherwise make excluding one trade look far more dramatic than it is.
   */
  baseline_metrics: Record<string, unknown>
  equity: EquityPoint[]
  trades: Trade[]
}

export function postBacktestWhatIf(
  id: string,
  excludeTradeIds: number[],
): Promise<WhatIfResponse> {
  return request<WhatIfResponse>(`/api/backtests/${id}/what-if`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ exclude_trade_ids: excludeTradeIds }),
  })
}

// ---- Compare (feature 2) ----

export interface CompareResponse {
  backtests: BacktestDetailResponse[]
}

export function getBacktestsCompare(ids: string[]): Promise<CompareResponse> {
  return request<CompareResponse>(`/api/backtests/compare?ids=${ids.join(',')}`)
}

// ---- Parameter sweeps (feature 3) ----

export interface SweepLeverSpec {
  path: string
  // numbers for most levers; bar strings ("5m", "1h") for timeframe.bar
  values: LeverValue[]
}

export type LeverValue = number | string

export type SweepGrid = Record<string, SweepLeverSpec>

export interface SweepResultItem {
  combo: Record<string, LeverValue>
  backtest_id: string
  metrics_summary: {
    total_return_pct: number | null
    sharpe: number | null
    max_drawdown_pct: number | null
    win_rate_pct: number | null
    trade_count: number | null
  }
}

export interface SweepSkippedItem {
  combo: Record<string, LeverValue>
  reason: string
}

export interface SweepResponse {
  sweep_id: string
  results: SweepResultItem[]
  skipped: SweepSkippedItem[]
}

export interface SweepDetailResponse {
  sweep_id: string
  grid: SweepGrid
  // strategy this sweep varied; null when the version index can't resolve it
  // (sweeps run before backtests.strategy_id existed).
  strategy_slug: string | null
  strategy_name: string | null
  results: SweepResultItem[]
}

export function postSweep(
  slug: string,
  config: BacktestConfig,
  grid: SweepGrid,
): Promise<SweepResponse> {
  return request<SweepResponse>('/api/backtests/sweep', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slug, config, grid }),
  })
}

export function getSweep(sweepId: string): Promise<SweepDetailResponse> {
  return request<SweepDetailResponse>(`/api/backtests/sweep/${sweepId}`)
}

export interface AiStatusResponse {
  configured: boolean
}

export function getAiStatus(): Promise<AiStatusResponse> {
  return request<AiStatusResponse>('/api/ai/status')
}

export function postAiGenerate(prompt: string, conversationId?: string): Promise<AIResult> {
  return request<AIResult>('/api/ai/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, conversation_id: conversationId }),
  })
}

export function postAiRefine(
  conversationId: string,
  message: string,
  document: StrategyDocument,
): Promise<AIResult> {
  return request<AIResult>('/api/ai/refine', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: conversationId, message, document }),
  })
}

export interface AiExplainResponse {
  answer: string
}

export function postAiExplain(question: string, context?: string): Promise<AiExplainResponse> {
  return request<AiExplainResponse>('/api/ai/explain', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, context }),
  })
}

// ---- Paper trading runs ----

export type RunState =
  | 'STARTING'
  | 'RUNNING'
  | 'PAUSED'
  | 'STOPPED'
  | 'ERROR'
  | 'SUSPENDED_AUTH'
  | 'DEGRADED_DATA'

export interface RunPosition {
  qty: number
  avg_price: number
  bars_held: number
  side?: string
  asset_type?: 'EQUITY' | 'OPTION'
  // ---- options only ----
  contract?: string
  contract_label?: string
  /** the runner reports the OCC letter, not the word */
  right?: 'C' | 'P' 
  strike?: number
  expiry?: string
  dte?: number | null
  multiplier?: number
  premium_paid?: number
  /** what this position can actually lose: premium x qty x multiplier */
  premium_at_risk?: number
  underlying_entry_px?: number
  mark?: number | null
  delta?: number | null
  theta?: number | null
  vega?: number | null
  iv?: number | null
  unrealized_pnl?: number | null
}

export interface RunOptionsStatus {
  instrument: 'options'
  portfolio_delta: number
  portfolio_vega: number
  premium_at_risk: number
  max_premium_at_risk: number
  breaches: string[]
  rejections: Record<string, number>
  stop_basis: 'underlying' | 'premium'
  /** false = nothing rests at the broker; the stop needs this app alive */
  broker_side_protection: boolean
}

export interface RunLiveStatus {
  run_id: string
  state: RunState
  mode: string
  positions: Record<string, RunPosition>
  pending_entries: string[]
  orders_today: number
  realized_pnl_today: number
  /** present only for an options run */
  options?: RunOptionsStatus
}

export interface RunSummary {
  /** 'options' runs trade contracts; the list badges them distinctly */
  instrument?: 'shares' | 'options'
  id: string
  mode: string
  state: RunState
  started_at: number | null
  stopped_at: number | null
  starting_equity: number | null
  account_hash: string | null
  slug: string
  name: string
  /** Lifetime realized P&L, summed over every fill the run has journaled —
   * unlike `live.realized_pnl_today` it survives restarts and covers a run
   * that has been going for days. Realized only, like the daily figure. */
  realized_pnl_total: number
  /** `realized_pnl_total` as a percentage of `starting_equity`; null when the
   * run has no starting equity recorded. */
  realized_pnl_total_pct: number | null
  live?: RunLiveStatus
  live_support_gaps?: LiveSupportGap[]
}

/** One way a run's document behaves differently live than in backtest.
 * Audit C1: before this, nothing in the UI told a user that a strategy used a
 * lever the live runtime does not honour as backtested. */
export interface LiveSupportGap {
  field: string
  detail: string
  blocking: boolean
}

export interface RunsListResponse {
  runs: RunSummary[]
}

export interface RunEvent {
  ts: number
  level: 'info' | 'warning' | 'error'
  kind: 'state' | 'order' | 'fill' | 'recovery' | 'error'
  payload_json: string
}

export interface RunDetail extends RunSummary {
  strategy_id: string
  strategy_version_hash: string
  resume_policy: string
  promotion_id: string | null
  recent_events: RunEvent[]
}

export interface StartRunResponse {
  run_id: string
}

export interface RunActionResponse {
  run_id: string
  event: 'pause' | 'resume' | 'flatten' | 'stop'
  // stop only: true when there was no active in-process runner to stop
  // (e.g. a live run left SUSPENDED_AUTH by a restart) -- the app closed
  // its own bookkeeping row only, and did NOT touch anything on Schwab's
  // side. Absent/false for a normal clean stop.
  forced?: boolean
  mode?: string
}

export function startRun(slug: string, startingCash?: number): Promise<StartRunResponse> {
  return request<StartRunResponse>('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slug, starting_cash: startingCash }),
  })
}

export function pauseRun(runId: string): Promise<RunActionResponse> {
  return request<RunActionResponse>(`/api/runs/${runId}/pause`, { method: 'POST' })
}

export function resumeRun(runId: string): Promise<RunActionResponse> {
  return request<RunActionResponse>(`/api/runs/${runId}/resume`, { method: 'POST' })
}

export function flattenRun(runId: string): Promise<RunActionResponse> {
  return request<RunActionResponse>(`/api/runs/${runId}/flatten`, { method: 'POST' })
}

export function stopRun(runId: string): Promise<RunActionResponse> {
  return request<RunActionResponse>(`/api/runs/${runId}/stop`, { method: 'POST' })
}

export interface RunRecoverResponse {
  run_id: string
  event: 'recovered'
  positions: number
  resume_policy: string
  started_paused: boolean
  /** paper only — reconstructed from the run's own fill history */
  reconstructed_cash?: number
  mode?: string
  // ---- live only: what reconciling against Schwab actually found ----
  account_hash?: string
  /** protective stop/target orders still resting at Schwab, re-adopted rather
   * than re-placed (re-placing would rest two stops against one position) */
  adopted_protective_orders?: number
  /** positions the journal held that Schwab no longer does — their exit filled
   * while the app was down */
  closed_while_down?: { symbol: string; journal_qty?: number }[]
  /** positions in symbols the strategy no longer trades (its YAML changed while
   * the run was down) — not adoptable, left in the record and reported */
  outside_universe?: { symbol: string; journal_qty: number; broker_qty: number }[]
  /** positions Schwab holds that the journal never recorded, adopted at the
   * broker's average price */
  adopted_from_broker?: { symbol: string; qty: number; avg_price: number }[]
  /** quantities taken from Schwab where the journal disagreed */
  qty_corrected?: { symbol: string; journal_qty: number; broker_qty: number }[]
  /** interrupted orders Schwab could account for, recorded as what they became */
  settled?: { intent_id: string; symbol: string; status: string; filled_qty: number }[]
  /** interrupted orders Schwab could NOT account for — genuinely unknown */
  unknown?: { intent_id: string; symbol: string }[]
}

/** Re-adopt a run the backend lost its in-process runner for (restart mid-run,
 * or a failed startup recovery) so it can be controlled again. Paper rebuilds
 * broker state from the durable positions/events tables; live reconciles
 * against Schwab and comes back PAUSED, never trading on its own. */
export function recoverRun(runId: string): Promise<RunRecoverResponse> {
  return request<RunRecoverResponse>(`/api/runs/${runId}/recover`, { method: 'POST' })
}

export function getRuns(): Promise<RunsListResponse> {
  return request<RunsListResponse>('/api/runs')
}

export function getRun(runId: string): Promise<RunDetail> {
  return request<RunDetail>(`/api/runs/${runId}`)
}

// ---- Promotion to live (Phase 6 slice 2/3, docs/plan/07-paper-trading.md) ----

export interface LiveEligibility {
  eligible: boolean
  has_backtest: boolean
  backtest_id: string | null
  backtest_metrics: Record<string, unknown> | null
  has_paper_run: boolean
  paper_run_id: string | null
  paper_elapsed_days: number
  paper_round_trips: number
  min_paper_days: number
  min_round_trips: number
}

export function getLiveEligibility(slug: string): Promise<LiveEligibility> {
  return request<LiveEligibility>(`/api/runs/live/eligibility?slug=${encodeURIComponent(slug)}`)
}

export function getLiveAccounts(): Promise<{ accounts: LiveProbeAccount[] }> {
  return request('/api/runs/live/accounts')
}

export function startLiveRun(
  slug: string,
  accountHash: string,
  overrideGate: boolean,
): Promise<StartRunResponse> {
  return request<StartRunResponse>('/api/runs/live', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slug, account_hash: accountHash, override_gate: overrideGate }),
  })
}

export function activateLiveRun(runId: string): Promise<{ run_id: string; event: string }> {
  return request(`/api/runs/${runId}/activate`, { method: 'POST' })
}

// ---- Kill switch ----

export interface KillStatusResponse {
  active: boolean
}

export interface KillActivateResponse {
  active: boolean
  failed_cancels_by_run: Record<string, string[]>
}

export function getKillStatus(): Promise<KillStatusResponse> {
  return request<KillStatusResponse>('/api/kill')
}

export function activateKill(cancelAll?: boolean): Promise<KillActivateResponse> {
  return request<KillActivateResponse>('/api/kill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cancel_all: cancelAll ?? false }),
  })
}

export function deactivateKill(): Promise<KillStatusResponse> {
  return request<KillStatusResponse>('/api/kill', { method: 'DELETE' })
}

// ---- Activity feed ----

export interface ActivityEntry {
  ts: number
  actor: string
  category: string
  payload: Record<string, unknown>
}

export interface ActivityResponse {
  activity: ActivityEntry[]
}

export function getActivity(limit?: number): Promise<ActivityResponse> {
  const params = new URLSearchParams()
  if (limit != null) params.set('limit', String(limit))
  const qs = params.toString()
  return request<ActivityResponse>(`/api/activity${qs ? `?${qs}` : ''}`)
}

// ---- Risk limits ----

export interface RiskLimitsResponse {
  max_total_notional: number
  max_total_daily_loss: number
  symbol_blocklist: string[]
  symbol_allowlist: string[] | null
}

export function getRiskLimits(): Promise<RiskLimitsResponse> {
  return request<RiskLimitsResponse>('/api/risk/limits')
}

export function putRiskLimits(
  overrides: Partial<RiskLimitsResponse>,
): Promise<RiskLimitsResponse> {
  return request<RiskLimitsResponse>('/api/risk/limits', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(overrides),
  })
}

// ---- Credentials (.env, via the Settings page) ----

export type CredentialField =
  | 'schwab_client_id'
  | 'schwab_client_secret'
  | 'schwab_callback_url'
  | 'anthropic_api_key'

export interface CredentialsResponse {
  fields: Record<CredentialField, { configured: boolean; value?: string }>
  env_path: string
  restart_required: boolean
}

export function getCredentials(): Promise<CredentialsResponse> {
  return request<CredentialsResponse>('/api/settings/credentials')
}

export function putCredentials(
  updates: Partial<Record<CredentialField, string>>,
): Promise<{ saved: string[]; restart_required: boolean; message: string }> {
  return request('/api/settings/credentials', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
}

export interface LiveTradingStatus {
  enabled: boolean
  restart_required: boolean
}

export function getLiveTradingEnabled(): Promise<LiveTradingStatus> {
  return request<LiveTradingStatus>('/api/settings/live-trading')
}

export function putLiveTradingEnabled(
  enabled: boolean,
): Promise<{ enabled: boolean; restart_required: boolean; message: string }> {
  return request('/api/settings/live-trading', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
}

// ---- Live probe (Phase 5.5 — places REAL orders, not linked from the sidebar) ----

export interface LiveProbeAccount {
  hash: string
  account_number_masked: string
  type: string
}

export interface LiveProbeStatusResponse {
  enabled: boolean
  max_qty: number
  max_notional: number
  accounts: LiveProbeAccount[]
  kill_switch_active?: boolean
}

export interface LiveProbeOrderFields {
  account_hash: string
  symbol: string
  side: 'buy' | 'sell'
  qty: number
  order_type: 'market' | 'limit' | 'stop' | 'stop_limit'
  limit_px?: number | null
  stop_px?: number | null
}

export interface LiveProbeOrderResult {
  intent_id: string
  broker_order_id: string
  status: string
}

export interface LiveProbeOrderIntent {
  id: string
  run_id: string
  ts: number
  symbol: string
  side: string
  qty: number
  order_type: string
  limit_px: number | null
  stop_px: number | null
  purpose: string
  status: string
  gateway_decision_json: string
}

export interface LiveProbeReconcileResponse {
  broker_positions: Record<string, number>
  broker_account: {
    cash: number | null
    equity: number | null
    buying_power: number | null
    positions: Record<string, number>
  }
  broker_orders_today: unknown[]
  local_orders: LiveProbeOrderIntent[]
}

export interface LiveProbeActivityMessage {
  id: number
  received_at: number
  seq: number | null
  raw_json: string
}

export function getLiveProbeStatus(): Promise<LiveProbeStatusResponse> {
  return request<LiveProbeStatusResponse>('/api/live-probe/status')
}

export function postLiveProbePreview(fields: LiveProbeOrderFields): Promise<unknown> {
  return request('/api/live-probe/preview', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  })
}

export function postLiveProbeOrder(
  fields: LiveProbeOrderFields,
  confirm: boolean,
): Promise<LiveProbeOrderResult> {
  return request<LiveProbeOrderResult>('/api/live-probe/orders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...fields, confirm }),
  })
}

export function postLiveProbeCancel(
  brokerOrderId: string,
  accountHash: string,
  confirm: boolean,
): Promise<{ broker_order_id: string; event: string }> {
  return request(`/api/live-probe/orders/${brokerOrderId}/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account_hash: accountHash, confirm }),
  })
}

export function putLiveProbeReplace(
  brokerOrderId: string,
  fields: LiveProbeOrderFields,
  confirm: boolean,
): Promise<LiveProbeOrderResult> {
  return request<LiveProbeOrderResult>(`/api/live-probe/orders/${brokerOrderId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...fields, confirm }),
  })
}

export function getLiveProbeOrders(): Promise<{ orders: LiveProbeOrderIntent[] }> {
  return request('/api/live-probe/orders')
}

export function getLiveProbeReconcile(accountHash: string): Promise<LiveProbeReconcileResponse> {
  return request<LiveProbeReconcileResponse>(
    `/api/live-probe/reconcile?account_hash=${encodeURIComponent(accountHash)}`,
  )
}

export function getLiveProbeActivityLog(): Promise<{ messages: LiveProbeActivityMessage[] }> {
  return request('/api/live-probe/activity-log')
}

// ---- Orders & transaction history ----

export interface OrderRow {
  intent_id: string
  run_id: string
  ts: number
  symbol: string
  side: string
  qty: number
  order_type: string
  limit_px: number | null
  stop_px: number | null
  purpose: string
  intent_status: string
  gateway_decision_json: string | null
  order_id: string | null
  mode: string | null
  schwab_order_id: string | null
  tag: string | null
  order_status: string | null
  submitted_at: number | null
  closed_at: number | null
  strategy_id: string | null
  slug: string | null
  name: string | null
  is_live_probe: boolean
  status: string
  /** 'OPTION' rows carry the OCC contract in `symbol`; `underlying` is the
   *  ticker a user actually groups by. Rows written before options existed
   *  read as EQUITY. */
  asset_type: 'EQUITY' | 'OPTION'
  underlying: string
  multiplier: number
  contract_label?: string
}

export interface OrdersListResponse {
  orders: OrderRow[]
}

export interface OrdersFilter {
  mode?: string
  status?: string
  symbol?: string
  run_id?: string
  start?: number
  end?: number
  limit?: number
}

export function ordersQueryString(filter: OrdersFilter): string {
  const params = new URLSearchParams()
  if (filter.mode) params.set('mode', filter.mode)
  if (filter.status) params.set('status', filter.status)
  if (filter.symbol) params.set('symbol', filter.symbol)
  if (filter.run_id) params.set('run_id', filter.run_id)
  if (filter.start != null) params.set('start', String(filter.start))
  if (filter.end != null) params.set('end', String(filter.end))
  if (filter.limit != null) params.set('limit', String(filter.limit))
  return params.toString()
}

export function getOrders(filter: OrdersFilter): Promise<OrdersListResponse> {
  const qs = ordersQueryString(filter)
  return request<OrdersListResponse>(`/api/orders${qs ? `?${qs}` : ''}`)
}

export interface OrderTimelineEvent {
  ts: number
  level: string
  kind: string
  payload_json: string
  payload: Record<string, unknown>
}

export interface OrderTimelineResponse {
  intent: {
    id: string
    run_id: string
    ts: number
    symbol: string
    side: string
    qty: number
    order_type: string
    limit_px: number | null
    stop_px: number | null
    purpose: string
    status: string
    gateway_decision_json: string | null
    gateway_decision: Array<Record<string, unknown>>
  }
  order: {
    id: string
    intent_id: string
    run_id: string
    mode: string
    schwab_order_id: string | null
    tag: string | null
    status: string
    submitted_at: number | null
    closed_at: number | null
    raw_last_json: string | null
  } | null
  is_live_probe: boolean
  matched_events: OrderTimelineEvent[]
}

export function getOrderTimeline(intentId: string): Promise<OrderTimelineResponse> {
  return request<OrderTimelineResponse>(`/api/orders/${intentId}/timeline`)
}

// ---- Open orders (live, straight from Schwab -- independent of any run) ----
//
// The order_intents-backed rows above are this app's OWN journal; a live
// order whose run has since stopped has no in-process broker left tracking
// it, so its real-time status can only come from asking Schwab directly.
// See app/api/execution.py's module docstring.

export interface OpenOrder {
  orderId: number
  status: string
  enteredTime: string
  quantity: number
  filledQuantity: number
  orderType: string
  account_hash: string
  orderLegCollection?: Array<{
    instruction: string
    quantity: number
    instrument: { symbol: string }
  }>
  [key: string]: unknown
}

export interface OpenOrdersResponse {
  orders: OpenOrder[]
}

export function getOpenOrders(): Promise<OpenOrdersResponse> {
  return request<OpenOrdersResponse>('/api/execution/open-orders')
}

export function getOrderStatus(orderId: string | number, accountHash: string): Promise<OpenOrder> {
  return request<OpenOrder>(
    `/api/execution/orders/${orderId}?account_hash=${encodeURIComponent(accountHash)}`,
  )
}

export function cancelOpenOrder(
  orderId: string | number,
  accountHash: string,
): Promise<{ order_id: string; canceled_at: number }> {
  return request(`/api/execution/open-orders/${orderId}/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ account_hash: accountHash }),
  })
}

// ---- Strategy optimizer (Phase 8, docs/plan/14-optimizer.md) ----
//
// Entirely additive: nothing above this line changes, and the optimizer lives
// on its own pages (/optimize) rather than inside the Backtests UI.

export type DimType = 'int' | 'float' | 'categorical'

export interface SpaceDim {
  path: string
  type: DimType
  low?: number | null
  high?: number | null
  step?: number | null
  log?: boolean
  choices?: (string | number)[]
  // present only on suggested spaces, for display in the editor table
  label?: string
  current?: number
}

export interface OptimizeConstraints {
  min_trades_per_fold: number
  min_trades_total: number
  max_drawdown_pct: number
  max_losing_streak: number
  require_all_folds_traded: boolean
}

export interface OptimizeObjective {
  metric: string
  lambda: number
  constraints: OptimizeConstraints
}

export interface PlanFold {
  index: number
  train_start: number
  train_end: number
  test_start: number
  test_end: number
  train_bars: number
  test_bars: number
  // distinct session dates, not calendar days
  test_days: number
  train_days: number
}

export interface OptimizePlan {
  bar: string
  symbols: string[]
  coverage_start: number
  coverage_end: number
  total_bars: number
  search_start: number
  search_end: number
  holdout_start: number
  holdout_end: number
  holdout_bars: number
  folds: PlanFold[]
  mode: string
  warnings: string[]
  per_symbol: Record<string, { bars: number; start: number | null; end: number | null }>
  total_days: number
  holdout_days: number
  bars_per_day: number
}

export interface StudyProgress {
  total: number
  done: number
  rejected: number
  errored: number
  attempted: number
  phase: string
  elapsed_sec: number
  eta_sec: number | null
  best_score: number | null
}

export interface TrialFold {
  index: number
  test: {
    score: number | null
    trade_count: number | null
    total_return_pct: number | null
    max_drawdown_pct: number | null
  }
  train: { score: number | null; trade_count: number | null }
}

export interface Trial {
  id: string
  study_id: string
  trial_number: number
  params: Record<string, number | string>
  status: 'done' | 'rejected' | 'error'
  reject_reason: string | null
  score: number | null
  stability: number | null
  oos_mean: number | null
  oos_std: number | null
  is_mean: number | null
  folds: TrialFold[] | null
  metrics: Record<string, number | null> | null
  created_at: number
}

export interface StudyHoldout {
  metrics?: Record<string, number | null>
  score?: number | null
  metric?: string
  start?: number
  end?: number
  bars?: number
  error?: string
}

// One rung of the champion's capital ladder (docs/plan/14-optimizer.md §1).
// Capital is not a search axis — it is a post-hoc check, because a champion's
// returns can be an artifact of the starting-capital number rather than of the
// strategy.
export interface CapitalLevel {
  multiplier: number
  capital: number
  metrics: Record<string, number | null> | null
  error?: string
  // fraction of the 1x return retained; null when it isn't meaningful
  retention?: number | null
  trade_drift?: number | null
  // static, simulation-free reasons this level is a different strategy
  notes: string[]
}

export type CapitalVerdict =
  | 'robust'
  | 'extremes-only'
  | 'fragile'
  | 'broken'
  | 'inconclusive'
  | 'unknown'

export interface CapitalSensitivity {
  base_capital: number
  metric: string
  multipliers: number[]
  levels: CapitalLevel[]
  verdict: CapitalVerdict
  headline: string
  worst_multiplier: number | null
  worst_retention: number | null
  window?: { start: number; end: number }
  reference_price?: number | null
  error?: string
}

export interface StudyDiagnostics {
  importance?: Record<string, number>
  capital_sensitivity?: CapitalSensitivity
  trials_scored?: number
  trials_rejected?: number
  trials_errored?: number
  champion_stability?: number | null
  champion_score?: number | null
  best_raw_score?: number | null
  champion_is_best_raw?: boolean
  warnings?: string[]
  note?: string
}

export interface StudyDetail {
  study_id: string
  slug: string
  strategy_id: string | null
  status: 'running' | 'done' | 'cancelled' | 'interrupted' | 'error'
  sampler: string
  seed: number
  n_trials: number
  created_at: number
  finished_at: number | null
  error: string | null
  space: SpaceDim[]
  objective: OptimizeObjective
  plan: OptimizePlan
  config: Record<string, unknown>
  holdout: StudyHoldout | null
  diagnostics: StudyDiagnostics | null
  champion: Trial | null
  progress: StudyProgress | null
  counts: { n: number; done: number; rejected: number; errored: number }
}

export interface StudyListItem {
  id: string
  slug: string
  strategy_id: string | null
  sampler: string
  seed: number
  n_trials: number
  status: StudyDetail['status']
  created_at: number
  finished_at: number | null
  error: string | null
  objective: OptimizeObjective
  holdout: StudyHoldout | null
  diagnostics: StudyDiagnostics | null
  bar: string
  folds: number
  progress: StudyProgress | null
}

export interface OptimizeMeta {
  metrics: string[]
  samplers: string[]
  max_dims: number
  max_trials: number
  default_trials: number
}

export interface PreviewPlanResponse {
  plan: OptimizePlan
  estimated_backtests: number
  n_trials: number
}

export interface CreateStudyBody {
  slug: string
  space: SpaceDim[]
  objective?: Partial<OptimizeObjective>
  n_trials?: number
  folds?: number
  mode?: string
  sampler?: string
  seed?: number
  config?: Record<string, unknown>
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function getOptimizeMeta(): Promise<OptimizeMeta> {
  return request<OptimizeMeta>('/api/optimize/meta')
}

export function suggestSpace(
  slug: string,
): Promise<{ space: SpaceDim[]; max_dims: number; document: StrategyDocument }> {
  return post('/api/optimize/suggest-space', { slug })
}

export function previewPlan(body: Partial<CreateStudyBody>): Promise<PreviewPlanResponse> {
  return post('/api/optimize/preview-plan', body)
}

export function createStudy(
  body: CreateStudyBody,
): Promise<{ study_id: string; plan: OptimizePlan; n_trials: number }> {
  return post('/api/optimize/studies', body)
}

export function getStudies(): Promise<{ studies: StudyListItem[] }> {
  return request('/api/optimize/studies')
}

export function getStudy(studyId: string): Promise<StudyDetail> {
  return request(`/api/optimize/studies/${studyId}`)
}

export function getTrials(studyId: string): Promise<{ trials: Trial[] }> {
  return request(`/api/optimize/studies/${studyId}/trials`)
}

export function cancelStudy(studyId: string): Promise<{ ok: boolean }> {
  return post(`/api/optimize/studies/${studyId}/cancel`, {})
}

export interface PromoteResult {
  backtest: { id: string; metrics: Record<string, number | null> }
  params: Record<string, number | string>
  // set when saved as a new VERSION of the same strategy
  saved_version: number | null
  // set when saved as a SEPARATE new strategy
  new_slug: string | null
}

export type PromoteMode = 'backtest_only' | 'new_version' | 'new_strategy'

export function promoteChampion(
  studyId: string,
  mode: PromoteMode,
  newName?: string,
): Promise<PromoteResult> {
  return post(`/api/optimize/studies/${studyId}/promote`, {
    save_as_version: mode === 'new_version',
    save_as_new_strategy: mode === 'new_strategy',
    new_name: newName,
  })
}

export function deleteStudy(studyId: string): Promise<{ ok: boolean }> {
  return request(`/api/optimize/studies/${studyId}`, { method: 'DELETE' })
}

// ---------------------------------------------------------------------------
// Options (docs/plan/16-options-trading.md)
//
// Deliberately namespaced under /api/options and kept in its own block: a
// reader should never have to guess whether a call is about shares or
// contracts. The UI applies the same rule visually — options surfaces use the
// amber accent, equities keep emerald/teal (see components/AssetTypeBadge).
// ---------------------------------------------------------------------------

export type OptionRight = 'C' | 'P'

export interface OptionContractRow {
  symbol: string
  describe: string
  right: OptionRight
  strike: number
  expiry: string
  dte: number
  bid: number | null
  ask: number | null
  mark: number | null
  last: number | null
  bid_size: number | null
  ask_size: number | null
  spread: number
  spread_pct: number | null
  volume: number | null
  open_interest: number | null
  iv: number | null
  delta: number | null
  gamma: number | null
  theta: number | null
  vega: number | null
  rho: number | null
  multiplier: number
}

export interface OptionChainResponse {
  underlying: string
  underlying_px: number | null
  ts: number
  contracts: OptionContractRow[]
  /** contracts Schwab returned, before validation */
  requested: number
  returned: number
  /** dropped by the data-quality guards — surfaced so a thin chain is explainable */
  filtered: number
  filter_reasons: Record<string, number>
  error: string | null
}

export function getOptionChain(params: {
  symbol: string
  maxDte?: number
  contractType?: 'ALL' | 'CALL' | 'PUT'
  strikeCount?: number
}): Promise<OptionChainResponse> {
  const q = new URLSearchParams({ symbol: params.symbol })
  if (params.maxDte != null) q.set('max_dte', String(params.maxDte))
  if (params.contractType) q.set('contract_type', params.contractType)
  if (params.strikeCount != null) q.set('strike_count', String(params.strikeCount))
  return request<OptionChainResponse>(`/api/options/chain?${q}`)
}

export interface OptionExpiration {
  expiry: string
  dte: number
  expiration_type: string | null
  settlement_type: string | null
  standard: boolean
}

export interface OptionExpirationsResponse {
  underlying: string
  /** 'schwab' = live and real; 'recorded' = from our snapshots; 'generated' =
   *  our own calendar model, i.e. believed-to-have-existed, not observed. */
  source: 'schwab' | 'recorded' | 'generated'
  expirations: OptionExpiration[]
}

export function getOptionExpirations(
  symbol: string,
  live = true,
): Promise<OptionExpirationsResponse> {
  return request<OptionExpirationsResponse>(
    `/api/options/expirations?symbol=${encodeURIComponent(symbol)}&live=${live}`,
  )
}

export interface OptionCoverageEntry {
  underlying: string
  first_ts: number
  last_ts: number
  rows: number
  snapshots: number
  expiries: number
  mean_contracts_per_snapshot: number
}

export interface OptionSnapshotRun {
  underlying: string
  ts: number
  requested: number
  written: number
  rejected: number
  reject_reasons: Record<string, number>
  underlying_px: number | null
  error: string | null
}

export interface OptionCoverageResponse {
  watchlist: string[]
  coverage: OptionCoverageEntry[]
  recent_runs: OptionSnapshotRun[]
  snapshots_taken: number
  rows_recorded: number
  /** ET clock times chain snapshots are scheduled for, e.g. ["09:45", ...] */
  schedule: string[]
}

export function getOptionCoverage(): Promise<OptionCoverageResponse> {
  return request<OptionCoverageResponse>('/api/options/coverage')
}

/** Snapshots per session day for one underlying. A day showing 1 is a day the
 *  backtest pricer extrapolated through. */
export interface OptionIntradayDay {
  date: string
  snapshots: number
  rows: number
  first_ts: number
  last_ts: number
}

export interface OptionIntradayCoverage {
  underlying: string
  days: number
  /** how many snapshots a COMPLETE day should have, per the schedule */
  expected_per_day: number
  by_day: OptionIntradayDay[]
}

export function getOptionIntradayCoverage(
  symbol: string,
  days = 30,
): Promise<OptionIntradayCoverage> {
  return request<OptionIntradayCoverage>(
    `/api/options/coverage/intraday?symbol=${encodeURIComponent(symbol)}&days=${days}`,
  )
}

/** Live streamed-quote recorder state. */
export interface OptionTickStats {
  enabled: boolean
  universe: number
  position_subscriptions: number
  buffered: number
  recorded: number
  /** real moves suppressed by the minimum write interval — the fidelity cost */
  dropped_conflated: number
  /** repeated quotes; free to drop, no information lost */
  dropped_unchanged: number
  min_interval_ms: number
  strikes_each_side: number
  expiries: number
  max_symbols: number
}

export interface OptionTickSummary {
  rows: number
  contracts: number
  underlyings: number
  first_ts: number | null
  last_ts: number | null
  days: number
}

export interface OptionTickDay {
  underlying: string
  date: string
  rows: number
  contracts: number
  first_ts: number
  last_ts: number
}

export interface OptionTickCoverage {
  stats: OptionTickStats
  summary: OptionTickSummary
  by_day: OptionTickDay[]
}

export function getOptionTickCoverage(days = 14): Promise<OptionTickCoverage> {
  return request<OptionTickCoverage>(`/api/options/coverage/ticks?days=${days}`)
}

export function refreshOptionTickUniverse(): Promise<Record<string, unknown>> {
  return request('/api/options/ticks/refresh', { method: 'POST' })
}

/** Which contracts are currently being streamed (tick-recorded). */
export function getOptionTickUniverse(
  symbol?: string,
): Promise<{ underlying: string | null; count: number; symbols: string[] }> {
  const q = symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''
  return request(`/api/options/ticks/universe${q}`)
}

export function getOptionWatchlist(): Promise<{ symbols: string[] }> {
  return request('/api/options/watchlist')
}

export function putOptionWatchlist(symbols: string[]): Promise<{ symbols: string[] }> {
  return request('/api/options/watchlist', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbols }),
  })
}

export function postOptionSnapshot(symbol?: string): Promise<{
  results: Array<{
    underlying: string
    requested?: number
    written: number
    rejected?: number
    reject_reasons?: Record<string, number>
    underlying_px?: number | null
    error?: string | null
  }>
}> {
  return request('/api/options/snapshot', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(symbol ? { symbol } : {}),
  })
}

export interface OptionContractHistory {
  symbol: string
  describe: string
  underlying: string
  right: OptionRight
  strike: number
  expiry: string
  /** chain-snapshot rows for this contract (the coarse, whole-chain series) */
  recorded: number
  points: Array<Record<string, number | null>>
  /** streamed quotes for the same contract — the high-resolution series */
  ticks: { count: number; first_ts: number | null; last_ts: number | null }
  /** whether it is in the tick-recording band RIGHT NOW */
  streaming: boolean
}

export function getOptionHistory(
  symbol: string,
  days = 180,
): Promise<OptionContractHistory> {
  return request<OptionContractHistory>(
    `/api/options/history?symbol=${encodeURIComponent(symbol)}&days=${days}`,
  )
}

export interface SelectPreviewResponse {
  underlying: string
  underlying_px: number
  rejection: { reason: string; detail: string } | null
  selected: {
    symbol: string
    describe: string
    right: OptionRight
    right_word: string
    strike: number
    expiry: string
    dte: number
    multiplier: number
    bid: number | null
    ask: number | null
    mid: number | null
    spread_pct: number | null
    iv: number | null
    delta: number | null
    theta: number | null
    vega: number | null
    open_interest: number | null
    cost_per_contract: number
  } | null
}

/** Dry-run contract selection: "given this expression, what would you buy right
 *  now?" Turns an abstract config block into something checkable without
 *  running a whole backtest — and answers "why did my strategy take no trades"
 *  in one click rather than one afternoon. */
export function postOptionSelect(body: {
  symbol: string
  expression: unknown
  liquidity?: unknown
  atr?: number
}): Promise<SelectPreviewResponse> {
  return request<SelectPreviewResponse>('/api/options/select', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}
