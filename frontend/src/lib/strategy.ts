// Types and helpers for the Strategy Library / Strategy Editor.
// Mirrors the backend strategy document schema.

export type Timeframe = '1m' | '5m' | '15m' | '30m' | '1h' | '1d'

export type Op =
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'
  | 'eq'
  | 'crosses_above'
  | 'crosses_below'
  | 'rising'
  | 'falling'

export type PriceField = 'open' | 'high' | 'low' | 'close' | 'volume'

// Series operands resolve to `raw * mul + add` (identity by default), so a
// threshold can be a multiple of another series — e.g. volume > 1.5x its
// 20-bar average. Literal `{ value }` operands are already constants and
// carry no scaling.
// `off_ind` adds an INDICATOR-valued offset on top of the affine part, so the
// resolved value is `raw * mul + add + (off_ind_value * off_mul)`. That is what
// volatility-relative levels need — `close < vwap - 1.5*ATR` is a moving
// distance a constant `add` cannot express.
export interface OperandScale {
  mul?: number
  add?: number
  off_ind?: string | null
  off_field?: string | null
  off_mul?: number
}

export type IndOperand = { ind: string; field?: string } & OperandScale
export type PriceOperand = { price: PriceField } & OperandScale
export type OffsetOperand = {
  ind_offset: { ind: string; field?: string; bars_ago: number }
} & OperandScale

export type Operand = IndOperand | PriceOperand | { value: number } | OffsetOperand

export interface Comparison {
  op: Op
  left: Operand
  right?: Operand
  bars?: number
}

export interface ExprAll {
  all: Expr[]
}
export interface ExprAny {
  any: Expr[]
}
export interface ExprNot {
  not: Expr
}

export type Expr = ExprAll | ExprAny | ExprNot | Comparison

export function isExprAll(e: Expr): e is ExprAll {
  return Object.prototype.hasOwnProperty.call(e, 'all')
}
export function isExprAny(e: Expr): e is ExprAny {
  return Object.prototype.hasOwnProperty.call(e, 'any')
}
export function isExprNot(e: Expr): e is ExprNot {
  return Object.prototype.hasOwnProperty.call(e, 'not')
}
export function isComparison(e: Expr): e is Comparison {
  return !isExprAll(e) && !isExprAny(e) && !isExprNot(e)
}

export function isOperandInd(o: Operand): o is IndOperand {
  return Object.prototype.hasOwnProperty.call(o, 'ind')
}
export function isOperandPrice(o: Operand): o is PriceOperand {
  return Object.prototype.hasOwnProperty.call(o, 'price')
}
export function isOperandValue(o: Operand): o is { value: number } {
  return Object.prototype.hasOwnProperty.call(o, 'value')
}
export function isOperandOffset(o: Operand): o is OffsetOperand {
  return Object.prototype.hasOwnProperty.call(o, 'ind_offset')
}

/** True for the operand kinds that support `mul`/`add` scaling. */
export function isScalableOperand(o: Operand): o is IndOperand | PriceOperand | OffsetOperand {
  return !isOperandValue(o)
}

export interface StopSpec {
  type: 'percent' | 'atr_multiple' | 'fixed_price_offset'
  value: number
  atr_indicator?: string | null
}

/** One partial profit-take: sell `percent` of the original position at
 * `target`, leaving the rest to run to take_profit / trailing stop / signal. */
export interface ScaleOut {
  target: StopSpec
  percent: number
}

/** A trailing stop is a StopSpec plus an optional activation threshold: until
 *  the position has run `activate_at` in the favourable direction the trail is
 *  dormant and only `stop_loss` protects. Once armed it never disarms. */
export interface TrailingStop extends StopSpec {
  activate_at?: StopSpec | null
}

/** Scale-IN: add to a position that is already working. Each add is triggered
 *  by a favourable move of `spacing` from the LAST fill and merges into one
 *  position at a volume-weighted average price. */
export interface Pyramid {
  max_adds: number
  spacing: StopSpec
  /** each add is this fraction of the ORIGINAL entry size (100 = same size) */
  size_pct?: number
}

export interface IndicatorInstance {
  id: string
  type: string
  params: Record<string, number>
  source?: string
}

export interface StrategyDocument {
  schema_version: number
  meta: {
    id?: string
    name: string
    description?: string
    tags?: string[]
    version?: number
    archived?: boolean
    starred?: boolean
  }
  universe: { symbols: string[] }
  timeframe: { bar: Timeframe; evaluate_on?: 'bar_close' }
  session?: {
    hours?: 'regular'
    entry_window?: { start: string; end: string } | null
    flatten_eod?: boolean
  }
  indicators: IndicatorInstance[]
  entry: {
    long: { when: Expr } | null
    /** plan/15: shorting is a first-class side, not a reserved slot */
    short?: { when: Expr } | null
    cooldown_bars?: number
    max_entries_per_day?: number
    /** scale-IN ladder; mirror image of exit.scale_outs */
    pyramid?: Pyramid | null
  }
  exit: {
    signal?: { when: Expr } | null
    /** optional asymmetric exit for shorts; falls back to `signal` */
    signal_short?: { when: Expr } | null
    stop_loss?: StopSpec | null
    take_profit?: StopSpec | null
    trailing_stop?: TrailingStop | null
    time_stop?: { bars: number } | null
    scale_outs?: ScaleOut[]
  }
  sizing: {
    type: 'fixed_dollars' | 'fixed_shares' | 'percent_of_equity' | 'risk_based'
    value: number
    max_position_notional?: number
    round_lot?: number
  }
  risk?: {
    max_open_positions?: number
    max_daily_loss?: number
    max_daily_orders?: number
    /** short-specific controls; `enabled` is a required explicit opt-in */
    short?: {
      enabled?: boolean
      initial_margin_pct?: number
      maintenance_margin_pct?: number
      max_short_notional?: number
      hard_to_borrow?: string[]
    }
    /** options portfolio limits; inert unless expression.instrument==='options' */
    options?: OptionRisk
  }
  /** How a fired signal is EXPRESSED as an instrument (docs/plan/16).
   *  Absent => trade shares, exactly as every pre-v2 document does. */
  expression?: Expression | null
  /** regime gate evaluated on a reference symbol and ANDed into every entry */
  market_filter?: {
    symbol: string
    indicators: IndicatorInstance[]
    when: Expr
    applies_to?: 'both' | 'long' | 'short'
    /** Separate gate-driven EXIT on the same reference symbol. When it fires,
     *  an open position is closed at the next bar's open with
     *  `exit_reason: "market_filter"`. Kept distinct from `not when` so the
     *  gate can carry HYSTERESIS (enter above one threshold, leave below a
     *  lower one). A strategy may have this as its ONLY exit. */
    exit_when?: Expr | null
  } | null
  /** cross-sectional ranking: trade only the top-N by `metric` each bar */
  selection?: {
    metric: Operand
    top_n?: number
    order?: 'desc' | 'asc'
  } | null
  orders?: {
    entry?: { type: 'market' | 'limit' | 'marketable_limit'; offset_bps?: number | null }
    exit?: { type: 'market' | 'limit' | 'marketable_limit'; offset_bps?: number | null }
    unfilled_timeout_sec?: number
    duration?: 'DAY'
  }
}

// ---------------------------------------------------------------------------
// Options expression (docs/plan/16). The rule layer above is UNCHANGED: these
// types only decide which instrument a fired signal is expressed in.
// ---------------------------------------------------------------------------

export type OptionRight = 'call' | 'put'

export interface StrikeSelector {
  by: 'delta' | 'moneyness_pct' | 'atr_multiple' | 'offset_dollars' | 'atm'
  target: number
  atr_indicator?: string | null
  tolerance?: number
}

export interface ExpirySelector {
  dte_min: number
  dte_target: number
  dte_max: number
  /** S=3rd Friday, M=end of month, Q=quarterly, W=weekly */
  allow: Array<'S' | 'M' | 'Q' | 'W'>
}

export interface OptionLiquidity {
  min_open_interest?: number
  min_volume?: number
  max_spread_pct?: number
  max_spread_abs?: number | null
  exclude_non_standard?: boolean
  exclude_mini?: boolean
}

export interface SideExpression {
  structure?: 'single' | 'vertical_debit'
  right: OptionRight
  expiry: ExpirySelector
  strike: StrikeSelector
  wing_width?: StrikeSelector | null
}

export interface OptionExit {
  /** 'underlying' = exit.stop_loss etc. measure the UNDERLYING's price, exactly
   *  as they do for shares. 'premium' = they measure the contract's premium.
   *  An underlying stop CANNOT rest at the broker — see the plan's §9.4. */
  stop_basis?: 'underlying' | 'premium'
  close_at_dte?: number
  roll?: boolean
  /** The one protective order that can rest at the broker under either basis. */
  max_loss_pct_of_premium?: number | null
  take_profit_pct_of_premium?: number | null
}

export interface OptionPricing {
  source?: 'auto' | 'synthetic' | 'recorded'
  /** variance risk premium; below 1.0 manufactures profit that never existed */
  vrp_mult?: number
  risk_free_rate?: number
  fill_aggression?: number
  liquidity_tier?: number | null
}

export interface Expression {
  instrument: 'shares' | 'options'
  long?: SideExpression | null
  short?: SideExpression | null
  liquidity?: OptionLiquidity
  exit?: OptionExit
  pricing?: OptionPricing
}

export interface OptionRisk {
  max_premium_at_risk?: number
  max_premium_per_trade_pct?: number
  max_portfolio_delta?: number | null
  max_portfolio_vega?: number | null
  max_contracts_per_position?: number
  avoid_earnings_within_days?: number
}

export interface StrategySummary {
  /** 'options' strategies express their signals as contracts */
  instrument?: 'shares' | 'options'
  slug: string
  id: string
  name: string
  description?: string
  tags: string[]
  version: number
  content_hash: string
  symbols: string[]
  bar: Timeframe
  archived: boolean
  starred: boolean
  /** Latest finished standalone backtest for this strategy; null if never run. */
  last_backtest: LastBacktest | null
}

export interface LastBacktest {
  id: string
  total_return_pct: number | null
  max_drawdown_pct: number | null
  sharpe: number | null
  start: string | null
  end: string | null
  finished_at: number | null
}

export interface StrategyListError {
  path: string
  message: string
}

export interface StrategyListResponse {
  strategies: StrategySummary[]
  errors: StrategyListError[]
}

export interface StrategyDetailResponse {
  slug: string
  document: StrategyDocument
  content_hash: string
  warnings: string[]
  errors: string[]
}

export interface ValidateResponse {
  valid: boolean
  errors: string[]
  warnings: string[]
  content_hash: string
}

export interface CreateStrategyResponse {
  slug: string
  content_hash: string
  warnings: string[]
}

export interface UpdateStrategyResponse {
  slug: string
  content_hash: string
  new_version: boolean
  version: number
  warnings: string[]
}

export interface IndicatorParamSpec {
  name: string
  default: number
  min: number
  max: number
  step: number
  doc: string
}

export interface IndicatorSpec {
  type: string
  doc: string
  outputs: string[]
  uses_source: boolean
  intraday_only: boolean
  params: IndicatorParamSpec[]
}

export interface CatalogResponse {
  indicators: IndicatorSpec[]
  schema_version: number
}

export interface AIUnsupported {
  requested: string
  reason: string
  approximation: string | null
}

export interface AIResult {
  conversation_id: string
  ok: boolean
  document: StrategyDocument | null
  content_hash: null
  assumptions: string[]
  unsupported: AIUnsupported[]
  notes: string
  errors: string[]
  warnings: string[]
}

export function newTemplateDocument(): StrategyDocument {
  return {
    schema_version: 1,
    meta: { name: 'New strategy', description: '', tags: [], version: 1 },
    universe: { symbols: ['SPY'] },
    timeframe: { bar: '1d', evaluate_on: 'bar_close' },
    session: { flatten_eod: false, entry_window: null },
    indicators: [{ id: 'rsi14', type: 'rsi', params: { period: 14 } }],
    entry: {
      long: {
        when: { op: 'lt', left: { ind: 'rsi14' }, right: { value: 30 } },
      },
      cooldown_bars: 0,
      max_entries_per_day: 1,
    },
    exit: {
      signal: null,
      stop_loss: { type: 'percent', value: 2 },
      take_profit: { type: 'percent', value: 5 },
      trailing_stop: null,
      time_stop: null,
    },
    sizing: { type: 'percent_of_equity', value: 20 },
    risk: {},
    orders: { entry: { type: 'market' }, exit: { type: 'market' } },
  }
}
