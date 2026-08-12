"""The strategy document schema (docs/plan/02-strategy-schema.md).

Declarative, closed-vocabulary, everything-is-a-lever. Consumed identically by
the backtest engine, the live/paper runtime, the UI, and AI generation
(structured outputs target). Structural validation lives here; cross-field
semantic checks live in lint.py."""

from __future__ import annotations

from typing import Annotated, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..indicators.registry import INDICATORS, SOURCES

# 2 adds the optional `expression:` block (options — docs/plan/16). Documents
# written as version 1 are UNCHANGED in meaning and continue to load: every
# addition below is optional with an identity default, and
# tests/test_options_schema_compat.py asserts that for every strategy on disk,
# including the ones running live right now.
SCHEMA_VERSION = 2

Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "1d"]
CmpOp = Literal["gt", "gte", "lt", "lte", "eq", "crosses_above", "crosses_below",
                "rising", "falling"]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- operands & expressions ----------------------------------------------

class ScaledOperand(Base):
    """Mixin for series operands. The resolved value is

        raw * mul + add + (off_ind_value * off_mul)

    `mul`/`add` express thresholds relative to another series ("volume > 1.5x
    its 20-bar average"). `off_ind` adds an INDICATOR-valued offset, which is
    what volatility-relative levels need — `close < vwap - 1.5*ATR` is a moving
    distance that a constant `add` cannot express. Without it such rules had to
    be approximated with Bollinger bands, which measure deviation from an SMA
    rather than from VWAP.

    All defaults are the identity, so every pre-existing document evaluates
    exactly as before."""

    mul: float = 1.0
    add: float = 0.0
    off_ind: str | None = None        # indicator id whose value is added
    off_field: str | None = None      # output name for multi-output indicators
    off_mul: float = 1.0              # scale applied to the offset indicator


class IndOperand(ScaledOperand):
    ind: str
    field: str | None = None  # output name for multi-output indicators


class PriceOperand(ScaledOperand):
    price: Literal["open", "high", "low", "close", "volume"]


class ValueOperand(Base):
    value: float


class IndOffsetOperand(ScaledOperand):
    ind_offset: "IndOffsetSpec"


class IndOffsetSpec(Base):
    ind: str
    field: str | None = None
    bars_ago: int = Field(ge=1, le=100)


Operand = Union[IndOperand, PriceOperand, ValueOperand, IndOffsetOperand]


class Comparison(Base):
    op: CmpOp
    left: Operand
    right: Operand | None = None      # rising/falling omit right
    bars: int | None = Field(default=None, ge=1, le=100)  # rising/falling window


class AllExpr(Base):
    all: list["Expr"] = Field(min_length=1, max_length=20)


class AnyExpr(Base):
    any: list["Expr"] = Field(min_length=1, max_length=20)


class NotExpr(Base):
    not_: "Expr" = Field(alias="not")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


Expr = Union[AllExpr, AnyExpr, NotExpr, Comparison]

AllExpr.model_rebuild()
AnyExpr.model_rebuild()
NotExpr.model_rebuild()
IndOffsetOperand.model_rebuild()


# ---- document sections ----------------------------------------------------

class Meta(Base):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    tags: list[str] = Field(default_factory=list, max_length=10)
    version: int = Field(default=1, ge=1)
    generated_by: dict | None = None  # AI provenance {model, conversation_id, turn}
    archived: bool = False  # hidden from the default strategy list/pickers, not deleted
    starred: bool = False  # hand-picked favorite; marker only, never reorders the list


class Universe(Base):
    # 100 is the scanner-universe ceiling: enough for a real intraday scanner
    # watchlist (the 50-100 range users actually ask for), while still bounded
    # so backfill/warm-up per backtest stays predictable.
    symbols: list[str] = Field(min_length=1, max_length=100)

    @field_validator("symbols")
    @classmethod
    def _norm(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = s.strip().upper()
            if not s or len(s) > 10 or not s.replace(".", "").isalnum():
                raise ValueError(f"invalid symbol {s!r}")
            out.append(s)
        if len(set(out)) != len(out):
            raise ValueError("duplicate symbols")
        return out


class TimeframeCfg(Base):
    bar: Timeframe = "1d"
    evaluate_on: Literal["bar_close"] = "bar_close"  # intrabar_tick reserved post-v1


class EntryWindow(Base):
    start: str = "09:35"  # ET HH:MM
    end: str = "15:45"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        h, m = v.split(":")
        assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        return v


class Session(Base):
    hours: Literal["regular"] = "regular"  # extended reserved post-v1
    entry_window: EntryWindow | None = None
    flatten_eod: bool = False


class IndicatorDef(Base):
    id: str = Field(min_length=1, max_length=30, pattern=r"^[a-z][a-z0-9_]*$")
    type: str
    params: dict[str, float] = Field(default_factory=dict)
    source: str = "close"

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in INDICATORS:
            raise ValueError(f"unknown indicator type {v!r}; valid: {sorted(INDICATORS)}")
        return v

    @field_validator("source")
    @classmethod
    def _known_source(cls, v: str) -> str:
        if v not in SOURCES:
            raise ValueError(f"unknown source {v!r}")
        return v


class Pyramid(Base):
    """Scale-IN: add to a position that is already working.

    Mirror image of `exit.scale_outs`. Each add is triggered by a FAVOURABLE
    move of `spacing` from the LAST fill (so adds are spaced, not clustered),
    is sized by the same `sizing` rule as the original entry, and merges into
    one position at a volume-weighted average price. The stop/target are
    recomputed from that new average, which is the conservative choice: the
    stop travels with the position rather than leaving the original (now
    further away) stop protecting a much larger size."""

    max_adds: int = Field(default=1, ge=1, le=10)
    spacing: StopSpec
    # each add is this fraction of the ORIGINAL entry size (100 = same size)
    size_pct: float = Field(default=100.0, gt=0, le=100)


class Entry(Base):
    """Entry rules. At least one side must be present (lint enforces it).

    A document may define both sides; they are evaluated independently per
    symbol, but a symbol can only hold ONE position at a time — there is no
    netting, and long+short on the same symbol simultaneously is rejected."""

    long: "RuleBlock | None" = None
    short: "RuleBlock | None" = None   # plan/15: shorting is no longer reserved
    cooldown_bars: int = Field(default=1, ge=0, le=500)
    max_entries_per_day: int = Field(default=1, ge=1, le=50)
    pyramid: Pyramid | None = None


class RuleBlock(Base):
    when: Expr


class StopSpec(Base):
    type: Literal["percent", "atr_multiple", "fixed_price_offset"]
    value: float = Field(gt=0)
    atr_indicator: str | None = None  # required when type == atr_multiple


class TrailingStop(StopSpec):
    """A trailing stop, optionally DORMANT until the position has already run
    `activate_at` in the favourable direction.

    Subclasses StopSpec rather than replacing it so every existing
    `trailing_stop: {type, value}` document keeps parsing unchanged, and every
    call site that accepts a StopSpec keeps working.

    `activate_at` is a favourable-direction distance from entry (same shape and
    units as take_profit). Until it is touched the trail contributes nothing and
    only `stop_loss` protects the position; once armed the trail ratchets from
    the high-water mark and never loosens or disarms. Arming is evaluated at bar
    close AFTER exit checks, so the bar that arms the trail can never also be
    stopped out by it."""

    activate_at: StopSpec | None = None


class TimeStop(Base):
    bars: int = Field(ge=1, le=10_000)


class ScaleOut(Base):
    """One partial profit-take: sell `percent` of the ORIGINAL entry quantity
    when `target` is reached, leaving the remainder to run to take_profit /
    trailing_stop / signal. Scale levels are nearer than the final take_profit,
    so they're checked first within a bar."""

    target: StopSpec
    percent: float = Field(gt=0, lt=100)


class Exit(Base):
    """Exit rules. Every distance spec is DIRECTION-RELATIVE: `stop_loss` means
    "an adverse move of X" and `take_profit` "a favourable move of X". For a
    long the stop sits below entry and the target above; for a short they are
    mirrored. One document shape therefore describes both sides, rather than
    duplicating every field into short_stop_loss/short_take_profit."""

    signal: RuleBlock | None = None
    # optional asymmetric exit for shorts; falls back to `signal` when absent
    signal_short: RuleBlock | None = None
    stop_loss: StopSpec | None = None
    take_profit: StopSpec | None = None
    trailing_stop: TrailingStop | None = None
    time_stop: TimeStop | None = None
    scale_outs: list[ScaleOut] = Field(default_factory=list, max_length=3)
    # precedence on same bar: stop_loss > trailing_stop > scale_outs > take_profit
    #                         > time_stop > signal


class Sizing(Base):
    type: Literal["fixed_dollars", "fixed_shares", "percent_of_equity", "risk_based"]
    value: float = Field(gt=0)
    max_position_notional: float = Field(default=25_000, gt=0)
    round_lot: int = Field(default=1, ge=1)


class ShortRisk(Base):
    """Short-specific risk controls.

    Shorting is NOT symmetric with buying: loss is unbounded, the position
    consumes margin rather than cash, shares must be borrowable, and a broker
    can force a buy-in. Each of those gets an explicit lever here rather than
    being implied by the long-side settings."""

    # explicit opt-in — belt and braces alongside the presence of entry.short,
    # so a short rule can never start trading through a config accident
    enabled: bool = False
    # Reg-T style: equity required BEYOND the sale proceeds, as % of notional
    initial_margin_pct: float = Field(default=50.0, ge=0, le=300)
    # equity/notional floor before a forced cover is simulated
    maintenance_margin_pct: float = Field(default=30.0, ge=0, le=300)
    max_short_notional: float = Field(default=25_000, gt=0)
    # names known to be hard/impossible to borrow — never shorted
    hard_to_borrow: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("hard_to_borrow")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s.strip()]


class OptionRisk(Base):
    """Portfolio-level option limits (docs/plan/16-options-trading.md §7.1).

    `sizing.max_position_notional` is reinterpreted for options as
    delta-adjusted UNDERLYING exposure, and the premium caps here bound what
    can actually be lost. BOTH are needed: 10 SPY 35-delta calls cost ~$3,000
    of premium but carry ~$210,000 of underlying exposure, and a system
    capping only one of those would be blind to the other."""

    max_premium_at_risk: float = Field(default=5_000, gt=0)          # total open debit
    max_premium_per_trade_pct: float = Field(default=2.0, gt=0, le=100)  # of equity
    max_portfolio_delta: float | None = Field(default=None, gt=0)    # delta-shares
    max_portfolio_vega: float | None = Field(default=None, gt=0)     # $ per vol point
    max_contracts_per_position: int = Field(default=50, ge=1, le=1000)
    # IV crush after an earnings print is the most common way a long-premium
    # trade loses money while being right on direction. 0 disables.
    avoid_earnings_within_days: int = Field(default=0, ge=0, le=30)


class Risk(Base):
    max_open_positions: int = Field(default=1, ge=1, le=50)
    max_daily_loss: float = Field(default=500, gt=0)
    max_daily_orders: int = Field(default=10, ge=1, le=200)
    short: ShortRisk = ShortRisk()
    # Only consulted when `expression.instrument == "options"`; inert on every
    # shares document, and (being a default) absent from `exclude_unset` dumps
    # so saving an untouched strategy never rewrites its file.
    options: OptionRisk = OptionRisk()


class OrderSpec(Base):
    type: Literal["market", "limit", "marketable_limit"] = "market"
    offset_bps: float | None = Field(default=None, ge=0, le=200)  # marketable_limit only


class Orders(Base):
    entry: OrderSpec = OrderSpec()
    exit: OrderSpec = OrderSpec()
    unfilled_timeout_sec: int = Field(default=60, ge=5, le=3600)
    duration: Literal["DAY"] = "DAY"


class MarketFilter(Base):
    """A regime gate evaluated on a REFERENCE symbol and ANDed into every entry
    (long and short).

    This is the gap that mattered most in the July-2026 research: rules could
    only see the symbol being evaluated, so every strategy bought dips into a
    falling market with no awareness of the tape. `symbol` is usually an index
    proxy (SPY/QQQ); its indicators are declared here separately because they
    are computed on the reference symbol's own bars, not the traded symbol's."""

    symbol: str = Field(min_length=1, max_length=10)
    indicators: list[IndicatorDef] = Field(default_factory=list, max_length=10)
    when: Expr
    # which side the gate applies to; "both" is the usual case
    applies_to: Literal["both", "long", "short"] = "both"
    # Optional SEPARATE exit condition on the same reference symbol. When it
    # fires, an open position gated by this filter is CLOSED at the next bar's
    # open (`exit_reason: "market_filter"`). Added in round 6
    # (`research4/FINDINGS4-LEVERAGED-TREND.md`).
    #
    # Two reasons it is a distinct expression rather than `not when`:
    #  * an entry-only gate is half a gate — a strategy holding a leveraged ETF
    #    while its UNDERLYING index trends up needs its EXIT to read the
    #    underlying too, and indicators are otherwise computed only on the
    #    traded symbol;
    #  * keeping it separate is what allows HYSTERESIS (enter above one
    #    threshold, leave below a lower one). On 1999-2009 the banded rule
    #    returned +193% against +96% for the same rule forced to a single
    #    threshold, because a shared threshold whipsaws in a bear market.
    #
    # Defaults to None = no gate-driven exits = exactly the pre-round-6
    # behaviour, so no existing strategy changes.
    exit_when: Expr | None = None

    @field_validator("symbol")
    @classmethod
    def _norm(cls, v: str) -> str:
        return v.strip().upper()


class Selection(Base):
    """Cross-sectional ranking: trade only the strongest/weakest `top_n` names
    in the universe on each bar.

    Without this, a multi-symbol strategy takes whichever signal happens to fire
    first in timestamp order, which is why the research had to hand-pick fixed
    universes and ended up sector-concentrated. Ties break on symbol name so a
    run is deterministic."""

    metric: Operand
    top_n: int = Field(default=3, ge=1, le=100)
    # desc = highest metric first (strength); asc = lowest first (weakness)
    order: Literal["desc", "asc"] = "desc"


# ---- options expression (docs/plan/16-options-trading.md) -----------------
#
# The central idea (plan/16 decision D1): options are an EXPRESSION of a signal,
# not a second signal language. Everything above this comment — indicators,
# entry/exit rules, market_filter, selection — keeps evaluating on the
# UNDERLYING's bars, completely unchanged. The block below only decides WHICH
# INSTRUMENT a fired signal is expressed in.
#
# That is what lets an existing strategy become an options strategy by adding
# one section, with no rule rewriting, and it is why the whole research corpus
# in research*/ stays valid.

Right = Literal["call", "put"]


class StrikeSelector(Base):
    """How to pick a strike once a signal fires.

    `delta` is the default because it is the only measure that means the same
    thing across underlyings and tenors: a 35-delta call is a comparable
    position on SPY and on NVDA, while "5% out of the money" is a wildly
    different bet on each."""

    by: Literal["delta", "moneyness_pct", "atr_multiple", "offset_dollars", "atm"] = "delta"
    # delta 0.35 | 5.0 (percent) | 1.5 (ATR multiples) | 2.50 (dollars)
    target: float = Field(default=0.35, gt=0)
    atr_indicator: str | None = None      # required when by == "atr_multiple"
    # How far from `target` a contract may sit and still be accepted. Widening
    # the search beats failing outright: real chains have gaps, and a strategy
    # that silently takes no trades because nothing matched to 3 decimal places
    # is worse than one that takes a 0.33-delta contract when it wanted 0.35.
    tolerance: float = Field(default=0.10, gt=0)


class ExpirySelector(Base):
    """Which expiration to trade.

    Defaults target the 25-55 DTE band: far enough out that theta is not
    punishing, near enough that the contract is liquid and responsive."""

    dte_min: int = Field(default=25, ge=0, le=730)
    dte_target: int = Field(default=35, ge=0, le=730)
    dte_max: int = Field(default=55, ge=0, le=730)
    # Weeklies are liquid on SPY/QQQ/IWM and thin on most single names, so
    # monthlies/quarterlies are the safer default for a broad universe.
    allow: list[Literal["S", "M", "Q", "W"]] = Field(
        default_factory=lambda: ["S", "M", "Q"])


class Liquidity(Base):
    """Contract-level filters.

    An options strategy that ignores these will backtest beautifully on
    contracts nobody could actually have traded. Note that open interest and
    volume CANNOT be enforced against a synthetic chain — that limitation is
    reported in the backtest's assumptions rather than silently ignored."""

    min_open_interest: int = Field(default=100, ge=0)
    min_volume: int = Field(default=0, ge=0)
    max_spread_pct: float = Field(default=10.0, gt=0, le=100)   # of mid
    max_spread_abs: float | None = Field(default=None, gt=0)    # dollars
    # v1 excludes these rather than mispricing them (plan/16 D10): an adjusted
    # contract does not deliver 100 shares, and a mini has a different
    # multiplier, both of which look entirely plausible while being wrong.
    exclude_non_standard: bool = True
    exclude_mini: bool = True


class SideExpression(Base):
    """How ONE signal direction becomes a contract.

    A `long` signal buys calls; a `short` signal buys puts. Both are
    long-premium and therefore defined-risk: the maximum loss is the debit
    paid, known exactly at entry. That property is what the whole safety
    argument rests on (plan/16 D2/D3), which is why `structure` has no
    naked-short member."""

    structure: Literal["single", "vertical_debit"] = "single"
    right: Right
    expiry: ExpirySelector = ExpirySelector()
    strike: StrikeSelector = StrikeSelector()
    # vertical_debit only: the short wing, as a distance from the long strike.
    wing_width: StrikeSelector | None = None


class OptionExit(Base):
    """Option-specific exits, ADDED to `exit.*` rather than replacing it.

    `stop_basis` decides what `exit.stop_loss` / `take_profit` /
    `trailing_stop` are measured against:

      underlying  the existing meaning — distances apply to the UNDERLYING's
                  price exactly as they do for a share position. This is what
                  every existing strategy already expresses, so it is the
                  default and translation is free.
      premium     distances apply to the CONTRACT's premium (percent only).

    THE CHOICE HAS A LIVE-SAFETY CONSEQUENCE. No broker offers "sell this call
    if SPY drops below 580" — an underlying-based stop cannot rest at Schwab
    and must be monitored in-process. `max_loss_pct_of_premium` exists so that
    a real protective order can ALWAYS rest at the broker regardless of basis.
    See plan/16 §9.4."""

    stop_basis: Literal["underlying", "premium"] = "underlying"
    # Hard lifecycle exit, always active. Never hold to expiration (plan/16 D9):
    # gamma explodes, spreads widen and assignment mechanics take over.
    close_at_dte: int = Field(default=1, ge=0, le=365)
    # Roll to the next expiration instead of closing, when still in profit.
    roll: bool = False
    # Cap the loss at a fraction of premium paid. This is the ONE protective
    # order that can rest at the broker under either stop_basis, so disabling
    # it leaves an underlying-stopped position with no broker-side protection
    # at all — lint warns about exactly that.
    max_loss_pct_of_premium: float | None = Field(default=60.0, gt=0, le=100)
    take_profit_pct_of_premium: float | None = Field(default=None, gt=0)


class PricingCfg(Base):
    """Backtest-only pricing controls. IGNORED live, where real quotes are
    mandatory and a modelled price is never acceptable."""

    source: Literal["auto", "synthetic", "recorded"] = "auto"
    # The variance risk premium (plan/16 D6). Implied vol has exceeded
    # subsequent realized vol on index products in ~85% of months since 1990;
    # without this multiplier, options priced off realized vol are
    # systematically too cheap and every long-premium strategy shows a profit
    # that never existed. Lint WARNS below 1.0.
    vrp_mult: float = Field(default=1.15, ge=0.5, le=2.0)
    risk_free_rate: float = Field(default=0.04, ge=-0.02, le=0.25)
    # 1.0 = cross the full bid/ask. Assuming a passive mid fill that may never
    # happen is how a backtest quietly awards itself free money every trade.
    fill_aggression: float = Field(default=1.0, ge=0.0, le=1.5)
    liquidity_tier: int | None = Field(default=None, ge=1, le=4)   # None = auto


class Expression(Base):
    """Maps rule-layer signals onto instruments.

    ABSENT (the default, and the state of every pre-existing document) means
    trade shares, exactly as before."""

    instrument: Literal["shares", "options"] = "shares"
    long: SideExpression | None = None     # used when entry.long fires
    short: SideExpression | None = None    # used when entry.short fires
    liquidity: Liquidity = Liquidity()
    exit: OptionExit = OptionExit()
    pricing: PricingCfg = PricingCfg()


class StrategyDocument(Base):
    schema_version: int = SCHEMA_VERSION
    meta: Meta
    universe: Universe
    timeframe: TimeframeCfg = TimeframeCfg()
    session: Session = Session()
    indicators: list[IndicatorDef] = Field(default_factory=list, max_length=25)
    entry: Entry
    exit: Exit = Exit()
    market_filter: MarketFilter | None = None
    selection: Selection | None = None
    # None = trade shares, exactly as every pre-v2 document does. Options are
    # opt-in and additive; see the `Expression` docstring.
    expression: Expression | None = None
    sizing: Sizing
    risk: Risk = Risk()
    orders: Orders = Orders()

    @field_validator("indicators")
    @classmethod
    def _unique_ids(cls, v: list[IndicatorDef]) -> list[IndicatorDef]:
        ids = [i.id for i in v]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate indicator ids")
        return v


Entry.model_rebuild()
