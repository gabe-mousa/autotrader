"""Event-driven backtest engine (docs/plan/05-backtest-engine.md).

Bar loop over a merged multi-symbol timeline; signals at bar t close create
orders that interact with bar t+1. Conservative fill conventions (documented in
the results UI): gap-through stops fill at open; same-bar stop+target conflict
resolves WORST CASE (stop first); limit entries need a strict cross unless
fill_on_touch. Cash-behavior sizing (decision log #5); long-only v1."""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

from ..strategy.evaluator import StrategyEvaluator
from ..strategy.market_filter import MarketFilterEval
from ..strategy.schema import StrategyDocument
from ..strategy.sizing import compute_contract_qty, compute_position_size
from ..strategy.expression import Rejection, right_for_side
from .options_runtime import OptionsRuntime
from .options_runtime import is_options as _is_options
from ..strategy.stops import (compute_scale_levels, compute_stop_price,
                              compute_trail_activation_px, compute_trail_px, trail_is_armed)
from ..strategy.timeframe import BAR_SECONDS  # noqa: F401 — re-exported for existing importers

ET = "America/New_York"


class SlippageCfg(BaseModel):
    """Slippage model.

    `fixed_bps` applies one number to everything, which is crude: 5bps is
    punitive for AAPL (~1bp real spread) and lenient for a $2 high-beta name.
    Since the whole edge-vs-cost-noise judgement turns on this parameter, two
    refinements are supported:

      per_symbol_bps   explicit override map, e.g. {"AAPL": 1, "LCID": 12}
      atr_fraction     slippage = value * (ATR/price) — wide-range names cost
                       more automatically, no table to maintain
    """

    type: Literal["fixed_bps", "atr_fraction"] = "fixed_bps"
    value: float = Field(default=1.0, ge=0, le=100)
    per_symbol_bps: dict[str, float] = Field(default_factory=dict)

    def bps_for(self, symbol: str, atr_pct: float | None = None) -> float:
        """Effective slippage in bps for one symbol. `atr_pct` is ATR as a
        percent of price, only consulted for the atr_fraction model."""
        override = self.per_symbol_bps.get(symbol)
        if override is not None:
            return override
        if self.type == "atr_fraction" and atr_pct is not None and atr_pct == atr_pct:
            return self.value * atr_pct * 100  # value * (ATR/px) expressed in bps
        return self.value


class CommissionCfg(BaseModel):
    per_order: float = Field(default=0.0, ge=0)
    # Schwab equities are commission-free; options are $0.65 per contract, and
    # at typical position sizes that is a material share of round-trip cost.
    # Only charged on option trades, so every existing equity backtest is
    # unaffected.
    per_contract: float = Field(default=0.65, ge=0)


class OptionsBacktestCfg(BaseModel):
    """Pricing inputs for an options backtest (docs/plan/16 §8.5).

    Inert on a shares strategy. `rate_curve` maps an ISO year ("2008") to that
    year's short rate: applying a flat 4% to 2008 or 2021 is materially wrong at
    longer tenors, and a built-in curve costs nothing."""

    risk_free_rate: float = Field(default=0.04, ge=-0.02, le=0.25)
    rate_curve: dict[str, float] = Field(default_factory=dict)
    dividend_yield: dict[str, float] = Field(default_factory=dict)
    pricing_source: Literal["auto", "synthetic", "recorded"] = "auto"


class BacktestConfig(BaseModel):
    start: str  # ISO date
    end: str
    initial_capital: float = Field(default=100_000, gt=0)
    slippage: SlippageCfg = SlippageCfg()
    commission: CommissionCfg = CommissionCfg()
    fill_on_touch: bool = False
    options: OptionsBacktestCfg = OptionsBacktestCfg()


@dataclass
class OpenPosition:
    symbol: str
    # explicit side with an always-POSITIVE qty. A signed-quantity
    # representation would push sign handling into every P&L, sizing, margin
    # and risk expression, which is exactly where direction bugs hide.
    side: Literal["long", "short"]
    qty: int
    entry_px: float
    entry_ts: pd.Timestamp
    entry_i: int
    stop_px: float | None
    target_px: float | None
    trail_px: float | None
    trail_dist: float | None      # absolute distance for trailing recompute
    time_stop_bars: int | None
    high_water: float             # for MFE + trailing
    low_water: float              # for MAE
    bars_held: int = 0
    # scale-out ladder: (target_price, shares_to_sell) per configured level,
    # sized off the ORIGINAL fill quantity and consumed in order. Levels are
    # dropped as they fill, so each fires at most once per position.
    scale_levels: list[tuple[float, int]] = field(default_factory=list)
    original_qty: int = 0
    # margin dollars reserved for a short (0 for longs) — released pro-rata as
    # the position is covered
    margin_reserved: float = 0.0
    # ---- pyramiding (entry.pyramid) ----
    adds_done: int = 0
    next_add_px: float | None = None   # price at which the next add triggers
    add_qty: int = 0                   # shares per add
    # ---- trailing-stop activation (exit.trailing_stop.activate_at) ----
    # the trail stays dormant — contributing no stop at all, so only stop_loss
    # protects — until the favourable excursion reaches trail_activate_px.
    # None => no activation gate, armed from entry (the historical behaviour).
    trail_activate_px: float | None = None
    trail_armed: bool = True
    # ---- options (docs/plan/16). All None/1.0 for a share position, so every
    # expression below reads identically for shares whether or not the strategy
    # is an options one. ----
    #
    # NOTE `symbol` above stays the UNDERLYING even for an options position, and
    # `positions` stays keyed by it. That is the structural trick that keeps
    # cooldowns, entries_today, market_filter, selection and the whole bar loop
    # working unchanged; the contract identity rides alongside.
    contract: object | None = None      # options.contracts.Contract
    multiplier: float = 1.0             # 100 for options, 1 for shares
    #: premium paid per contract at entry — for a LONG option this is also the
    #: maximum possible loss, known exactly at entry (plan/16 D2)
    premium_paid: float = 0.0
    entry_iv: float | None = None
    entry_delta: float | None = None
    #: underlying price at entry, kept separately because `entry_px` holds the
    #: option premium for an options position
    underlying_entry_px: float = 0.0

    @property
    def is_option(self) -> bool:
        return self.contract is not None


@dataclass
class PendingEntry:
    symbol: str
    signal_i: int
    limit_px: float | None        # None = market
    side: Literal["long", "short"] = "long"


class BacktestResult:
    def __init__(self):
        self.equity_points: list[tuple[pd.Timestamp, float]] = []
        self.trades: list[dict] = []
        self.metrics: dict = {}
        self.assumptions: dict = {}
        # positions still open when the backtest window ended — never closed,
        # so they're excluded from `trades` (which only holds closed round
        # trips), but they DO mark-to-market into the equity curve and thus
        # affect sharpe/return/drawdown. Surfaced separately so a run with
        # e.g. sharpe=0.24 and 0 trades isn't mysterious: there's an open
        # position driving those numbers, it just hasn't closed yet.
        self.open_positions: list[dict] = []


def _slip(px: float, bps: float, side: str) -> float:
    return px * (1 + bps / 10_000) if side == "buy" else px * (1 - bps / 10_000)


def run_backtest(doc: StrategyDocument, cfg: BacktestConfig,
                 candles: dict[str, pd.DataFrame],
                 option_ticks: list[dict] | None = None) -> BacktestResult:
    """candles: symbol -> OHLCV DataFrame (UTC index, strategy timeframe).
    Callers should include some history BEFORE cfg.start (see
    estimate_warmup_bars) so indicators are already warmed up by the first
    evaluated bar — trimming to exactly [start, end] before computing
    indicators would leave long-period indicators NaN for most/all of a short
    date range, silently suppressing signals."""
    ev = StrategyEvaluator(doc)
    frames: dict[str, pd.DataFrame] = {}
    for sym in doc.universe.symbols:
        df = candles.get(sym)
        if df is None or df.empty:
            continue
        df = df.loc[:cfg.end]  # cap the end only — keep any pre-start warm-up rows
        if len(df) < 2:
            continue
        frames[sym] = df
        ev.compile_symbol(sym, df)
    result = BacktestResult()
    result.assumptions = {
        "fill_model": "signals at bar close act on next bar; market=next open±slip; "
                      "stop gap-through fills at open; same-bar stop+target = stop first "
                      "(worst case); scale-outs fill before the final target within a bar "
                      "and each books its own trade row (partial=true)",
        "fill_on_touch": cfg.fill_on_touch,
        "slippage_bps": cfg.slippage.value,
        "commission_per_order": cfg.commission.per_order,
        "sizing": "cash-behavior (no margin)",
    }
    if not frames:
        result.metrics = {"error": "no candle data in range"}
        return result

    # merged timeline of all bar timestamps — indicators were computed over
    # the full (possibly warm-up-extended) frames above, but only bars within
    # the REQUESTED window are actually evaluated/traded/reported, so a short
    # date range still benefits from any earlier history that was fetched.
    eval_start = pd.Timestamp(cfg.start, tz="UTC")
    timeline = sorted(
        ts for ts in set().union(*[set(df.index) for df in frames.values()]) if ts >= eval_start)
    if not timeline:
        result.metrics = {"error": "no candle data within the requested date range "
                                   "(warm-up history exists earlier, but nothing at/after start)"}
        return result
    index_of = {sym: {ts: i for i, ts in enumerate(df.index)} for sym, df in frames.items()}

    cash = cfg.initial_capital
    positions: dict[str, OpenPosition] = {}
    pending: dict[str, PendingEntry] = {}
    cooldown_until: dict[str, int] = {}
    entries_today: dict[str, int] = {}
    cur_day = None
    day_start_equity = cfg.initial_capital
    day_paused = False
    bars_in_market = 0
    intraday = doc.timeframe.bar != "1d"
    e = doc.exit
    sr = doc.risk.short
    py = doc.entry.pyramid
    # dollars of equity committed as margin against open shorts (see sized_qty)
    reserved_margin = 0.0
    _htb = {s.upper() for s in sr.hard_to_borrow}

    # ---- options expression (plan/16) ------------------------------------
    # `opt` is None for every shares strategy, which is every pre-existing
    # document — and when it is None, every branch below is skipped and the
    # engine behaves exactly as it did before.
    # Anchor frames ($VIX etc.) arrive in `candles` alongside the traded
    # symbols but are never traded themselves, so they are pulled out here
    # rather than added to `frames`.
    opt = (OptionsRuntime.build(doc, cfg, frames,
                                anchor_frames={k: v for k, v in candles.items()
                                               if k.startswith("$")})
           if _is_options(doc) else None)
    if opt is not None:
        # Recorded quotes, when the streamer captured these contracts over the
        # backtest window. Absent -> every price is modelled, exactly as before.
        #
        # `pricing_source` is the user's explicit control over that choice:
        #   auto       use recordings where they exist, model the rest
        #   synthetic  ignore recordings entirely — reproduces a pre-recording
        #              result, and is the only way to compare the two
        #   recorded   REFUSE to model. A window with no recording produces no
        #              trades rather than modelled ones, which is what makes a
        #              "fully recorded" claim checkable instead of assumed.
        source_mode = getattr(cfg.options, "pricing_source", "auto")
        if option_ticks and source_mode != "synthetic":
            from .tick_source import TickQuoteSource
            opt.ticks = TickQuoteSource.from_rows(option_ticks)
        opt.require_recorded = (source_mode == "recorded")
    if opt is not None:
        result.assumptions["instrument"] = "options"

    # ---- market-regime filter (plan/15 §C1) ------------------------------
    # Compiled on the REFERENCE symbol's own bars with its own indicators. The
    # gate reads the reference bar at or before the current timestamp, so it
    # can never see a bar the traded symbol hasn't reached (no look-ahead).
    mf = doc.market_filter
    _mf_eval = _mf_index = None
    if mf is not None:
        mf_df = candles.get(mf.symbol)
        if mf_df is not None and not mf_df.empty:
            mf_df = mf_df.loc[:cfg.end]
            _mf_eval = MarketFilterEval(mf, mf_df)
            _mf_index = mf_df.index

    def market_ok(ts, side: str) -> bool:
        if mf is None:
            return True
        if mf.applies_to != "both" and mf.applies_to != side:
            return True
        if _mf_eval is None:
            # a configured filter whose data is missing must FAIL CLOSED —
            # silently trading unfiltered would defeat the point of the gate
            return False
        j = _mf_index.searchsorted(ts, side="right") - 1
        if j < 0:
            return False
        return _mf_eval.ok(j)

    def market_exit(ts, side: str) -> bool:
        """Does the regime gate's own exit condition fire for an open position?"""
        if mf is None or mf.exit_when is None or _mf_eval is None:
            return False
        if mf.applies_to != "both" and mf.applies_to != side:
            return False
        j = _mf_index.searchsorted(ts, side="right") - 1
        return False if j < 0 else _mf_eval.exit_now(j)

    # ---- cross-sectional selection (plan/15 §C5) -------------------------
    sel = doc.selection

    def selected_at(ts) -> set[str] | None:
        """Symbols eligible to ENTER on this bar, or None when unrestricted."""
        if sel is None:
            return None
        scored: list[tuple[float, str]] = []
        for sym in frames:
            i = index_of[sym].get(ts)
            if i is None:
                continue
            try:
                v = ev.sym(sym).operand_at(sel.metric, i)
            except (KeyError, IndexError):
                continue
            if v == v:  # skip NaN (warm-up)
                scored.append((v, sym))
        if not scored:
            return set()
        # ties break on symbol name so a run is deterministic
        scored.sort(key=lambda t: (-t[0], t[1]) if sel.order == "desc" else (t[0], t[1]))
        return {s for _, s in scored[:sel.top_n]}

    def short_allowed(sym: str) -> bool:
        """Shorting requires an explicit opt-in AND a borrowable symbol. The
        opt-in is deliberately separate from the presence of entry.short so a
        short rule can never start trading through a config accident.

        OPTIONS ARE THE EXCEPTION, and it matters. On an options document a
        "short" signal BUYS PUTS (plan/16 D2): that is a long-premium,
        defined-risk position which consumes no margin, borrows nothing, and
        cannot lose more than its debit. Requiring `risk.short.enabled` for it
        would gate a put purchase behind stock-borrow machinery that has
        nothing to do with it — and would silently take zero trades, which is
        exactly how this was found. The equity gates still apply in full to
        share shorts."""
        if not doc.entry.short:
            return False
        if opt is not None:
            return doc.expression.short is not None
        return sr.enabled and sym.upper() not in _htb

    # per-symbol ATR% cache, only built when the atr_fraction model is in use.
    # Computed from the frame directly rather than requiring the strategy to
    # declare an ATR indicator — cost modelling shouldn't depend on what the
    # strategy happens to have configured.
    _atr_pct: dict[str, pd.Series] = {}

    def slip_bps(sym: str, i: int) -> float:
        if cfg.slippage.type != "atr_fraction":
            return cfg.slippage.bps_for(sym)
        s = _atr_pct.get(sym)
        if s is None:
            from ..indicators.library import atr as _atr
            df = frames[sym]
            s = (_atr(df, {"period": 14}, None)["value"] / df["close"]).fillna(0.0)
            _atr_pct[sym] = s
        val = float(s.iloc[i]) if 0 <= i < len(s) else 0.0
        return cfg.slippage.bps_for(sym, val)

    #: last known option mark per underlying, so a bar with no computable quote
    #: falls back to the previous mark instead of silently dropping the position
    #: from equity — which would produce an impossibly smooth equity curve.
    _last_option_mark: dict[str, float] = {}

    def equity_at(ts) -> float:
        """equity = cash + Σ_long(qty × px) − Σ_short(qty × px)

        `cash` already includes short-sale proceeds, so the short leg is
        subtracted as the liability to buy the shares back. Worked example:
        short 100 @ $10 from $100k -> cash 101,000, equity 101,000-1,000 =
        100,000 (unchanged, correctly). At $8: 101,000-800 = 100,200 (+$200).

        A LONG OPTION position marks to the contract's modelled mid times its
        multiplier, and is always additive: long premium cannot go negative."""
        eq = cash
        for sym, pos in positions.items():
            df = frames[sym]
            i = index_of[sym].get(ts)
            px = float(df["close"].iloc[i]) if i is not None else pos.underlying_entry_px
            if pos.is_option:
                q = opt.quote(pos.contract, px, int(ts.timestamp() * 1000),
                              i if i is not None else 0)
                mark = q.mid if q is not None else _last_option_mark.get(
                    sym, pos.premium_paid)
                _last_option_mark[sym] = mark
                eq += pos.qty * mark * pos.multiplier
            else:
                eq += pos.qty * px if pos.side == "long" else -pos.qty * px
        return eq

    def _premium_at_risk() -> float:
        """Total open option debit — what could actually be lost right now.
        Bounds `risk.options.max_premium_at_risk` at sizing time."""
        return sum(p.entry_px * p.qty * p.multiplier
                   for p in positions.values() if p.is_option)

    def short_notional_at(ts) -> float:
        tot = 0.0
        for sym, pos in positions.items():
            if pos.side != "short":
                continue
            i = index_of[sym].get(ts)
            px = float(frames[sym]["close"].iloc[i]) if i is not None else pos.entry_px
            tot += pos.qty * px
        return tot

    def stop_target_for(sym: str, i: int, entry_px: float, side: str):
        """Stop/target/trailing distance for either side. The distances are
        identical to a long's — only which side of entry they sit on flips."""
        stop = target = trail_dist = activate_px = None
        if e.stop_loss:
            stop = _stop_px(e.stop_loss, entry_px, ev, sym, i, side=side, kind="stop")
        if e.take_profit:
            target = _stop_px(e.take_profit, entry_px, ev, sym, i, side=side, kind="target")
        if e.trailing_stop:
            t = _stop_px(e.trailing_stop, entry_px, ev, sym, i, side=side, kind="stop")
            trail_dist = abs(entry_px - t) if t is not None else None
            spec = e.trailing_stop.activate_at
            activate_px = compute_trail_activation_px(
                e.trailing_stop, entry_px,
                ev.atr_value(sym, spec.atr_indicator, i)
                if spec is not None and spec.type == "atr_multiple" else None,
                side=side)
        return stop, target, trail_dist, activate_px

    def scale_levels_for(sym: str, i: int, entry_px: float, qty: int,
                         side: str) -> list[tuple[float, int]]:
        # shared with the live/paper runtime — app/strategy/stops.py
        def atr_for(spec):
            return ev.atr_value(sym, spec.atr_indicator, i) if spec.type == "atr_multiple" else None
        return compute_scale_levels(e.scale_outs, entry_px, qty, atr_for, side=side)

    def close_position(sym: str, px: float, ts, i: int, reason: str,
                       qty: int | None = None, exit_premium: float | None = None):
        """Close `qty` shares (default: the whole position). A partial close
        leaves the position open with the remainder and books a trade row for
        the shares actually sold — so a scaled exit shows up as multiple round
        trips off one entry, each with its own P&L, rather than being averaged
        into a single blended exit price that never happened.

        The entry commission is charged once per SHARE LOT sold, prorated, so
        a scaled-out position isn't charged a full entry commission per
        partial; the exit commission is charged per partial, which is correct
        (each partial is a real order)."""
        nonlocal cash, reserved_margin
        pos = positions[sym]
        qty = pos.qty if qty is None else min(qty, pos.qty)
        if qty <= 0:
            return
        partial = qty < pos.qty
        frac = qty / pos.qty if pos.qty else 1.0
        if partial:
            pos.qty -= qty
        else:
            positions.pop(sym)

        if pos.is_option:
            _close_option(pos, sym, qty, px, ts, i, reason, partial,
                          exit_premium=exit_premium)
            if not partial:
                cooldown_until[sym] = i + doc.entry.cooldown_bars
            return

        # closing a long SELLS (slip down); closing a short BUYS TO COVER
        # (slip up) — the adverse direction in both cases
        px = _slip(px, slip_bps(sym, i), "sell" if pos.side == "long" else "buy")
        # prorated entry commission (see docstring)
        entry_fee = cfg.commission.per_order * (qty / pos.original_qty
                                                if pos.original_qty else 1.0)
        if pos.side == "long":
            proceeds = qty * px - cfg.commission.per_order
            cash += proceeds
            pnl = proceeds - qty * pos.entry_px - entry_fee
        else:
            # buy the borrowed shares back; release the margin held against them
            cost = qty * px + cfg.commission.per_order
            cash -= cost
            release = pos.margin_reserved * frac
            pos.margin_reserved -= release
            reserved_margin -= release
            pnl = qty * pos.entry_px - cost - entry_fee
        # P&L relative to the position's ENTRY notional, matching the long
        # convention: long (exit-entry)/entry, short (entry-exit)/entry. Using
        # (entry/exit - 1) for a short would express the return against the
        # covering price instead and read ~11% where the long side reads 10%.
        pnl_pct = ((px / pos.entry_px - 1) if pos.side == "long"
                   else (1 - px / pos.entry_px)) * 100
        # excursions flip with direction: a short's best case is the LOW
        mfe_ref, mae_ref = ((pos.high_water, pos.low_water) if pos.side == "long"
                            else (pos.low_water, pos.high_water))
        sign = 1 if pos.side == "long" else -1
        result.trades.append({
            "symbol": sym, "side": pos.side,
            # epoch ms — matches the backtest_trades schema (INTEGER columns)
            # and every other timestamp in the system (candles, equity points).
            # A previous version used str(pd.Timestamp) here, which produces a
            # space-separated (non-ISO) string that JS's Date() parses
            # unreliably across browsers -> "Invalid Date" in the trades table.
            "entry_ts": int(pos.entry_ts.timestamp() * 1000), "entry_px": round(pos.entry_px, 4),
            "exit_ts": int(ts.timestamp() * 1000), "exit_px": round(px, 4), "qty": qty,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "mae_pct": round(sign * (mae_ref / pos.entry_px - 1) * 100, 4),
            "mfe_pct": round(sign * (mfe_ref / pos.entry_px - 1) * 100, 4),
            "bars_held": pos.bars_held, "exit_reason": reason,
            "partial": partial,
        })
        if not partial:
            # a partial exit must NOT start the re-entry cooldown — the
            # position is still open and the cooldown only governs the gap
            # between a FLAT symbol and its next entry.
            cooldown_until[sym] = i + doc.entry.cooldown_bars

    def _close_option(pos: OpenPosition, sym: str, qty: int, und_px: float,
                      ts, i: int, reason: str, partial: bool,
                      exit_premium: float | None = None) -> None:
        """Book an option exit.

        The trade row reports PREMIUM as entry_px/exit_px and contracts as qty,
        so `pnl == (exit - entry) * qty * multiplier - commissions` reads
        directly off the row and can be checked by hand — which
        tests/test_options_engine.py does for every trade.

        MAE/MFE stay in UNDERLYING terms: they are what the stops are measured
        against under the default `stop_basis`, and an excursion expressed in
        premium would not correspond to anything the strategy reacts to."""
        nonlocal cash
        ts_ms = int(ts.timestamp() * 1000)
        if opt.is_expired(pos.contract, ts_ms):
            exit_px = opt.settle_at_expiry(pos.contract, und_px)
            reason = "expired_unclosed"
        elif exit_premium is not None:
            # A premium stop/target fired INSIDE this bar, at a level taken
            # from the recorded quote range. Re-pricing it off the bar's close
            # would book the exit at a price the position never actually
            # reached — the whole point of an intrabar trigger.
            exit_px = exit_premium
        else:
            got = opt.transact(pos.contract, und_px, ts_ms, "close", i)
            # No computable quote: settle at intrinsic rather than dropping the
            # position, which would leak it out of the equity curve entirely.
            exit_px = got if got is not None else max(
                (und_px - pos.contract.strike_f) if pos.contract.is_call
                else (pos.contract.strike_f - und_px), 0.0)

        commission = cfg.commission.per_contract * qty
        entry_commission = cfg.commission.per_contract * qty      # prorated per contract
        proceeds = exit_px * qty * pos.multiplier - commission
        cash += proceeds
        pnl = (exit_px - pos.entry_px) * qty * pos.multiplier - commission - entry_commission
        pnl_pct = (exit_px / pos.entry_px - 1) * 100 if pos.entry_px else 0.0

        d = 1 if pos.side == "long" else -1
        result.trades.append({
            "symbol": sym, "side": pos.side,
            "entry_ts": int(pos.entry_ts.timestamp() * 1000),
            "entry_px": round(pos.entry_px, 4),          # premium per contract
            "exit_ts": ts_ms, "exit_px": round(exit_px, 4),
            "qty": qty, "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 4),
            # underlying excursions — what the stops actually react to
            "mae_pct": round(d * (pos.low_water / pos.underlying_entry_px - 1) * 100, 4)
            if pos.underlying_entry_px else 0.0,
            "mfe_pct": round(d * (pos.high_water / pos.underlying_entry_px - 1) * 100, 4)
            if pos.underlying_entry_px else 0.0,
            "bars_held": pos.bars_held, "exit_reason": reason, "partial": partial,
            # ---- options detail, so a trade row is self-describing ----
            "instrument": "option",
            "contract": pos.contract.symbol,
            "contract_label": pos.contract.describe(),
            "right": pos.contract.right,
            "strike": float(pos.contract.strike),
            "expiry": pos.contract.expiry.isoformat(),
            "multiplier": pos.multiplier,
            "dte_at_entry": (pos.contract.expiry - pos.entry_ts.date()).days,
            "dte_at_exit": (pos.contract.expiry - ts.date()).days,
            "underlying_entry_px": round(pos.underlying_entry_px, 4),
            "underlying_exit_px": round(und_px, 4),
            "entry_iv": round(pos.entry_iv, 4) if pos.entry_iv else None,
            "entry_delta": round(pos.entry_delta, 4) if pos.entry_delta else None,
            "premium_paid": round(pos.entry_px * qty * pos.multiplier, 2),
        })

    def sized_qty(entry_px: float, stop_px: float | None, equity: float) -> int:
        """Shared with the live/paper runtime — app/strategy/sizing.py.

        Buying power is cash MINUS margin already committed to open shorts:
        short proceeds land in cash but are not freely spendable, so passing
        raw cash would let the backtest open positions a real account could
        not."""
        return compute_position_size(doc.sizing, entry_px, stop_px, equity,
                                     max(cash - reserved_margin, 0.0),
                                     commission_per_order=cfg.commission.per_order)

    ew = doc.session.entry_window

    def in_entry_window(ts: pd.Timestamp) -> bool:
        if not intraday or ew is None:
            return True
        local = ts.tz_convert(ET)
        hhmm = f"{local.hour:02d}:{local.minute:02d}"
        return ew.start <= hhmm <= ew.end

    def is_last_bar_of_day(sym: str, i: int) -> bool:
        df = frames[sym]
        if i + 1 >= len(df):
            return True
        return df.index[i].tz_convert(ET).date() != df.index[i + 1].tz_convert(ET).date()

    for ts in timeline:
        # ---- day rollover ------------------------------------------------
        day = ts.tz_convert(ET).date()
        if day != cur_day:
            cur_day = day
            entries_today.clear()
            day_start_equity = equity_at(ts)
            day_paused = False

        # ---- 1) process pending entries at this bar's open ---------------
        for sym in list(pending):
            pe = pending.pop(sym)
            if sym in positions or day_paused:
                continue
            df = frames[sym]
            i = index_of[sym].get(ts)
            if i is None or i != pe.signal_i + 1:
                continue  # bar gap (halt/missing) — drop, re-evaluate naturally
            o, hi, lo = (float(df[k].iloc[i]) for k in ("open", "high", "low"))
            long_side = pe.side == "long"
            # opening a long BUYS (slip up); opening a short SELLS SHORT (slip
            # down) — the adverse direction in both cases
            enter_dir = "buy" if long_side else "sell"
            if pe.limit_px is None:
                fill = _slip(o, slip_bps(sym, i), enter_dir)
            elif long_side:
                if o < pe.limit_px:
                    fill = _slip(o, slip_bps(sym, i), "buy")
                elif (lo < pe.limit_px) or (cfg.fill_on_touch and lo <= pe.limit_px):
                    fill = pe.limit_px
                else:
                    continue  # unfilled; canceled (timeout semantics)
            else:
                # a short limit sells at or above the limit: mirror of the long
                if o > pe.limit_px:
                    fill = _slip(o, slip_bps(sym, i), "sell")
                elif (hi > pe.limit_px) or (cfg.fill_on_touch and hi >= pe.limit_px):
                    fill = pe.limit_px
                else:
                    continue
            equity = equity_at(ts)
            stop, target, trail_dist, trail_activate_px = stop_target_for(
                sym, pe.signal_i, fill, pe.side)

            # ---- options: express the signal as a contract ----------------
            # `fill` is the UNDERLYING price. Stops/targets/trails were computed
            # against it above and stay in underlying terms (the default
            # stop_basis), so the entire bar loop below works unchanged; only
            # the instrument being bought and the P&L arithmetic differ.
            if opt is not None:
                ts_ms = int(ts.timestamp() * 1000)
                atr_val = None
                side_expr = right_for_side(doc.expression, pe.side)
                if side_expr is not None and side_expr.strike.by == "atr_multiple":
                    atr_val = ev.atr_value(sym, side_expr.strike.atr_indicator,
                                           pe.signal_i)
                chosen = opt.select(sym, pe.side, pe.signal_i, ts_ms, atr=atr_val)
                if isinstance(chosen, Rejection):
                    continue
                entry_premium = opt.transact(chosen.contract, fill, ts_ms, "open", i)
                if entry_premium is None or entry_premium <= 0:
                    continue
                q = chosen.quote
                sizing_result = compute_contract_qty(
                    doc.sizing, doc.risk.options, entry_premium,
                    chosen.contract.multiplier,
                    q.greeks.delta if q else 0.5, fill, equity,
                    max(cash, 0.0), premium_at_risk_open=_premium_at_risk(),
                    commission_per_contract=cfg.commission.per_contract)
                if sizing_result.qty <= 0:
                    opt.rejections["sized_to_zero:" + sizing_result.bound_by] = \
                        opt.rejections.get("sized_to_zero:" + sizing_result.bound_by, 0) + 1
                    continue
                qty = sizing_result.qty
                cash -= qty * entry_premium * chosen.contract.multiplier \
                    + cfg.commission.per_contract * qty
                positions[sym] = OpenPosition(
                    symbol=sym, side=pe.side, qty=qty, entry_px=entry_premium,
                    entry_ts=ts, entry_i=i, stop_px=stop, target_px=target,
                    trail_px=(((fill - trail_dist) if long_side else (fill + trail_dist))
                              if trail_dist and trail_activate_px is None else None),
                    trail_dist=trail_dist, trail_activate_px=trail_activate_px,
                    trail_armed=trail_activate_px is None,
                    time_stop_bars=e.time_stop.bars if e.time_stop else None,
                    high_water=fill, low_water=fill, scale_levels=[],
                    original_qty=qty, margin_reserved=0.0,
                    contract=chosen.contract,
                    multiplier=chosen.contract.multiplier,
                    premium_paid=entry_premium,
                    entry_iv=q.iv if q else None,
                    entry_delta=q.greeks.delta if q else None,
                    underlying_entry_px=fill)
                entries_today[sym] = entries_today.get(sym, 0) + 1
                continue

            qty = sized_qty(fill, stop, equity)
            if qty <= 0:
                continue
            margin = 0.0
            if long_side:
                cash -= qty * fill + cfg.commission.per_order
            else:
                notional = qty * fill
                if notional > sr.max_short_notional:
                    qty = int(sr.max_short_notional // fill)
                    if qty <= 0:
                        continue
                    notional = qty * fill
                margin = notional * sr.initial_margin_pct / 100
                if margin > max(cash - reserved_margin, 0.0):
                    continue  # not enough buying power to carry the short
                cash += notional - cfg.commission.per_order
                reserved_margin += margin
            positions[sym] = OpenPosition(
                symbol=sym, side=pe.side, qty=qty, entry_px=fill, entry_ts=ts, entry_i=i,
                stop_px=stop, target_px=target,
                # a gated trail rests at NO price until it arms; only stop_loss
                # protects until then, so trail_px stays None rather than
                # sitting a trail-distance from entry
                trail_px=(((fill - trail_dist) if long_side else (fill + trail_dist))
                          if trail_dist and trail_activate_px is None else None),
                trail_dist=trail_dist,
                trail_activate_px=trail_activate_px,
                trail_armed=trail_activate_px is None,
                time_stop_bars=e.time_stop.bars if e.time_stop else None,
                high_water=fill, low_water=fill,
                scale_levels=scale_levels_for(sym, pe.signal_i, fill, qty, pe.side),
                original_qty=qty, margin_reserved=margin,
            )
            if py is not None:
                p = positions[sym]
                p.add_qty = int(qty * py.size_pct / 100)
                # first add triggers a FAVOURABLE `spacing` move from this fill
                p.next_add_px = compute_stop_price(
                    py.spacing, fill,
                    ev.atr_value(sym, py.spacing.atr_indicator, pe.signal_i)
                    if py.spacing.type == "atr_multiple" else None,
                    side=pe.side, kind="target")
            entries_today[sym] = entries_today.get(sym, 0) + 1

        # ---- 1b) option expiry / DTE lifecycle ---------------------------
        # BEFORE position management: a contract at its close-out DTE must be
        # resolved before any other exit logic touches it, and an expired one
        # must never be marked or stopped as though it were still live.
        if opt is not None and positions:
            for sym in [s for s, p in positions.items() if p.is_option]:
                pos = positions[sym]
                i = index_of[sym].get(ts)
                if i is None:
                    continue
                ts_ms = int(ts.timestamp() * 1000)
                if opt.is_expired(pos.contract, ts_ms):
                    close_position(sym, float(frames[sym]["close"].iloc[i]), ts, i,
                                   "expired_unclosed")
                elif opt.should_close_for_dte(pos.contract, ts_ms):
                    close_position(sym, float(frames[sym]["close"].iloc[i]), ts, i,
                                   "dte_close")

        # ---- 2) manage open positions through this bar's range -----------
        for sym in list(positions):
            df = frames[sym]
            i = index_of[sym].get(ts)
            if i is None:
                continue
            pos = positions[sym]
            o, h, lo, c = (float(df[k].iloc[i]) for k in ("open", "high", "low", "close"))
            pos.bars_held += 1
            pos.high_water = max(pos.high_water, h)
            pos.low_water = min(pos.low_water, lo)

            # ---- intrabar PREMIUM stop / target (recorded quotes only) ------
            # Checked before the underlying stop: `max_loss_pct_of_premium` is
            # the one protective order that rests at the broker for an option
            # position, so in life it fires on premium regardless of where the
            # underlying is. Needs real recorded quotes — a premium range
            # inferred from the underlying at fixed sigma is exactly the
            # approximation this is here to replace, so with no recording the
            # block is skipped entirely and behaviour is unchanged.
            if pos.is_option and opt is not None and opt.ticks is not None:
                prev_ts_ms = int(df.index[i - 1].timestamp() * 1000) if i > 0 \
                    else int(ts.timestamp() * 1000) - 1
                rng = opt.premium_range(pos.contract, prev_ts_ms,
                                        int(ts.timestamp() * 1000))
                if rng is not None:
                    prem_lo, prem_hi = rng
                    ox = doc.expression.exit
                    stop_prem = (pos.entry_px * (1 - ox.max_loss_pct_of_premium / 100)
                                 if ox.max_loss_pct_of_premium else None)
                    tgt_prem = (pos.entry_px * (1 + ox.take_profit_pct_of_premium / 100)
                                if ox.take_profit_pct_of_premium else None)
                    # Worst case first, same convention as the underlying path.
                    if stop_prem is not None and prem_lo <= stop_prem:
                        close_position(sym, c, ts, i, "premium_stop",
                                       exit_premium=min(stop_prem, prem_hi))
                        continue
                    if tgt_prem is not None and prem_hi >= tgt_prem:
                        close_position(sym, c, ts, i, "premium_target",
                                       exit_premium=max(tgt_prem, prem_lo))
                        continue

            is_long = pos.side == "long"
            stop = pos.stop_px
            if pos.trail_px is not None:
                # the tighter of the two: highest for a long, lowest for a short
                stop = (max(stop, pos.trail_px) if stop is not None else pos.trail_px) \
                    if is_long else \
                    (min(stop, pos.trail_px) if stop is not None else pos.trail_px)

            # worst-case ordering: stop checked before target, both sides.
            # A long stops out on weakness (low <= stop); a short stops out on
            # STRENGTH (high >= stop), which is the unbounded-loss direction.
            if stop is not None:
                gapped = o <= stop if is_long else o >= stop
                touched = lo <= stop if is_long else h >= stop
                if gapped:
                    close_position(sym, o, ts, i, "stop_gap"); continue
                if touched:
                    close_position(sym, stop, ts, i, "stop"); continue
            # scale-outs before the final target: they sit NEARER to entry, so
            # within a bar that reaches both, the partial necessarily filled
            # first. Levels are consumed in order and each fires at most once.
            # A gap straight through a level fills at the open (same
            # conservative convention as a gapped stop/target).
            while pos.scale_levels:
                level_px = pos.scale_levels[0][0]
                reached = (o >= level_px or h >= level_px) if is_long else \
                          (o <= level_px or lo <= level_px)
                if not reached:
                    break
                level_px, shares = pos.scale_levels.pop(0)
                gapped = o >= level_px if is_long else o <= level_px
                close_position(sym, o if gapped else level_px, ts, i,
                               "scale_out_gap" if gapped else "scale_out", qty=shares)
                if sym not in positions:
                    break
            if sym not in positions:
                continue

            # ---- pyramiding: add to a working position ----------------------
            # Checked AFTER the stop (a stop that hits in the same bar wins, the
            # conservative reading) and BEFORE the target, mirroring scale-outs.
            while (py is not None and pos.adds_done < py.max_adds
                   and pos.next_add_px is not None and pos.add_qty > 0):
                lvl = pos.next_add_px
                reached = (o >= lvl or h >= lvl) if is_long else (o <= lvl or lo <= lvl)
                if not reached:
                    break
                gapped = o >= lvl if is_long else o <= lvl
                add_px = _slip(o if gapped else lvl, slip_bps(sym, i),
                               "buy" if is_long else "sell")
                add_qty = pos.add_qty
                if is_long:
                    cost = add_qty * add_px + cfg.commission.per_order
                    if cost > max(cash - reserved_margin, 0.0):
                        break                      # cannot afford the add
                    cash -= cost
                else:
                    notional = add_qty * add_px
                    extra_margin = notional * sr.initial_margin_pct / 100
                    if extra_margin > max(cash - reserved_margin, 0.0):
                        break
                    cash += notional - cfg.commission.per_order
                    reserved_margin += extra_margin
                    pos.margin_reserved += extra_margin
                # merge into ONE position at the volume-weighted average price
                total = pos.qty + add_qty
                pos.entry_px = (pos.entry_px * pos.qty + add_px * add_qty) / total
                pos.qty = total
                pos.original_qty += add_qty
                pos.adds_done += 1
                # stop/target travel with the new average — leaving the original
                # stop in place would protect a much larger position from much
                # further away, which is the opposite of conservative
                new_stop, new_target, new_trail, new_activate = stop_target_for(
                    sym, i, pos.entry_px, pos.side)
                pos.stop_px, pos.target_px = new_stop, new_target
                if new_trail is not None:
                    pos.trail_dist = new_trail
                    # an add re-bases the activation gate on the new average
                    # entry, but never DISARMS a trail that already armed
                    pos.trail_activate_px = new_activate
                    if not pos.trail_armed:
                        pos.trail_px = None
                    else:
                        pos.trail_px = (pos.entry_px - new_trail if is_long
                                        else pos.entry_px + new_trail)
                pos.next_add_px = (
                    compute_stop_price(py.spacing, add_px,
                                       ev.atr_value(sym, py.spacing.atr_indicator, i)
                                       if py.spacing.type == "atr_multiple" else None,
                                       side=pos.side, kind="target")
                    if pos.adds_done < py.max_adds else None)

            if pos.target_px is not None:
                gapped = o >= pos.target_px if is_long else o <= pos.target_px
                touched = h >= pos.target_px if is_long else lo <= pos.target_px
                if gapped:
                    close_position(sym, o, ts, i, "target_gap"); continue
                if touched:
                    close_position(sym, pos.target_px, ts, i, "target"); continue
            if pos.time_stop_bars is not None and pos.bars_held >= pos.time_stop_bars:
                close_position(sym, c, ts, i, "time"); continue
            if intraday and doc.session.flatten_eod and is_last_bar_of_day(sym, i):
                close_position(sym, c, ts, i, "eod"); continue
            # trailing update AFTER exit checks (bar-close adjustment semantics).
            # A short's trail ratchets DOWN from the low-water mark.
            if pos.trail_dist is not None:
                # arm first, then ratchet. Both happen AFTER the exit checks
                # above, so the bar that arms the trail can never also be
                # stopped out by it.
                if not pos.trail_armed:
                    pos.trail_armed = trail_is_armed(
                        pos.trail_activate_px, pos.high_water, pos.low_water, side=pos.side)
                if pos.trail_armed:
                    pos.trail_px = compute_trail_px(
                        pos.trail_px, pos.trail_dist, pos.high_water, pos.low_water,
                        side=pos.side)

        # ---- 3) bar-close rule evaluation --------------------------------
        eligible = selected_at(ts)
        for sym, df in frames.items():
            i = index_of[sym].get(ts)
            if i is None or i >= len(df) - 1:
                continue
            if sym in positions:
                # a regime gate carrying `exit_when` also CLOSES positions
                # (round 6). Checked before the document's own exit signal so
                # the reason is attributable.
                if market_exit(ts, positions[sym].side):
                    nxt = i + 1
                    close_position(sym, float(df["open"].iloc[nxt]), df.index[nxt],
                                   nxt, "market_filter")
                elif ev.exit_signal(sym, i, positions[sym].side):
                    # market exit at next bar open
                    nxt = i + 1
                    close_position(sym, float(df["open"].iloc[nxt]), df.index[nxt], nxt, "signal")
            elif not day_paused and sym not in pending:
                if (len(positions) + len(pending)) >= doc.risk.max_open_positions:
                    continue
                if entries_today.get(sym, 0) >= doc.entry.max_entries_per_day:
                    continue
                if i < cooldown_until.get(sym, -1):
                    continue
                if not in_entry_window(ts):
                    continue
                if intraday and doc.session.flatten_eod and is_last_bar_of_day(sym, i):
                    continue
                if eligible is not None and sym not in eligible:
                    continue    # not in this bar's top-N by the selection metric
                # long is checked first; a symbol holds at most ONE position, so
                # a document with both sides firing on the same bar takes the long
                for side in ("long", "short"):
                    if side == "short" and not short_allowed(sym):
                        continue
                    if not market_ok(ts, side):
                        continue
                    if not ev.entry_signal(sym, i, side):
                        continue
                    limit = None
                    spec = doc.orders.entry
                    close_px = float(df["close"].iloc[i])
                    if spec.type == "limit":
                        limit = close_px
                    elif spec.type == "marketable_limit":
                        off = (spec.offset_bps or 10) / 10_000
                        # marketable = cross the spread: up to buy, down to sell
                        limit = close_px * (1 + off) if side == "long" else close_px * (1 - off)
                    pending[sym] = PendingEntry(sym, i, limit, side)
                    break

        # ---- 3b) maintenance margin / forced buy-in ----------------------
        # A short's loss is unbounded, so a runaway position must be closed
        # rather than allowed to report fantasy equity. Mirrors the real
        # forced buy-in: when account equity falls below maintenance_margin_pct
        # of the short market value, cover everything at the bar close.
        if positions and sr.enabled:
            sn = short_notional_at(ts)
            if sn > 0 and equity_at(ts) < sn * sr.maintenance_margin_pct / 100:
                for sym in [s for s, p in positions.items() if p.side == "short"]:
                    i = index_of[sym].get(ts)
                    if i is None:
                        continue
                    close_position(sym, float(frames[sym]["close"].iloc[i]), ts, i,
                                   "margin_call")

        # ---- 4) equity mark + daily-loss pause ---------------------------
        eq = equity_at(ts)
        result.equity_points.append((ts, eq))
        if positions:
            bars_in_market += 1
        if not day_paused and (day_start_equity - eq) >= doc.risk.max_daily_loss:
            day_paused = True
            pending.clear()

    # ---- final mark: liquidate nothing; report open positions ------------
    equity = pd.Series({ts: v for ts, v in result.equity_points}).sort_index()
    from .metrics import compute_metrics
    result.metrics = compute_metrics(
        equity, result.trades, bars_in_market, len(timeline),
        BAR_SECONDS[doc.timeframe.bar])
    result.metrics["open_positions_at_end"] = len(positions)
    if opt is not None:
        result.assumptions["options"] = opt.assumptions()
        # Options returns are floored at -100% and fat-tailed, so a Sharpe ratio
        # computed on them is close to meaningless. It stays in the metrics for
        # continuity, but the UI must caveat it rather than rank on it.
        result.metrics["return_distribution_warning"] = (
            "Long-option returns are floored at -100% and strongly non-normal. "
            "Sharpe and standard deviation are not meaningful here; judge on "
            "profit factor, expectancy and drawdown instead.")
    last_ts = timeline[-1] if timeline else None
    for sym, pos in positions.items():
        last_px = float(frames[sym]["close"].iloc[-1])
        d = 1 if pos.side == "long" else -1
        if pos.is_option:
            ts_ms = int(last_ts.timestamp() * 1000) if last_ts is not None else 0
            q = opt.quote(pos.contract, last_px, ts_ms, len(frames[sym]) - 1)
            mark = q.mid if q is not None else pos.entry_px
            result.open_positions.append({
                "symbol": sym, "qty": pos.qty, "side": pos.side,
                "instrument": "option", "contract": pos.contract.symbol,
                "contract_label": pos.contract.describe(),
                "entry_ts": int(pos.entry_ts.timestamp() * 1000),
                "entry_px": round(pos.entry_px, 4), "last_px": round(mark, 4),
                "underlying_last_px": round(last_px, 4),
                "unrealized_pnl": round((mark - pos.entry_px) * pos.qty * pos.multiplier, 2),
                "unrealized_pnl_pct": round((mark / pos.entry_px - 1) * 100, 4)
                if pos.entry_px else 0.0,
                "bars_held": pos.bars_held,
                "as_of_ts": ts_ms or None,
            })
            continue
        result.open_positions.append({
            "symbol": sym, "qty": pos.qty, "side": pos.side,
            "entry_ts": int(pos.entry_ts.timestamp() * 1000), "entry_px": round(pos.entry_px, 4),
            "last_px": round(last_px, 4),
            "unrealized_pnl": round(d * (last_px - pos.entry_px) * pos.qty, 2),
            "unrealized_pnl_pct": round(d * (last_px / pos.entry_px - 1) * 100, 4),
            "bars_held": pos.bars_held,
            "as_of_ts": int(last_ts.timestamp() * 1000) if last_ts is not None else None,
        })
    return result


def run_backtest_serialized(doc_data: dict, cfg_data: dict,
                            candles: dict[str, pd.DataFrame],
                            option_ticks: list[dict] | None = None):
    """Picklable entrypoint for ProcessPoolExecutor: plain dicts in, result out.

    `option_ticks` crosses the process boundary as plain row dicts rather than a
    DB handle: the pool workers have no event loop and no connection, so the
    recorded quotes are loaded once in the async layer and shipped in."""
    doc = StrategyDocument.model_validate(doc_data)
    cfg = BacktestConfig.model_validate(cfg_data)
    return run_backtest(doc, cfg, candles, option_ticks=option_ticks)


def _stop_px(spec, entry_px: float, ev: StrategyEvaluator, sym: str, i: int,
             below: bool | None = None, *, side: str = "long",
             kind: str | None = None) -> float | None:
    # shared with the live/paper runtime — app/strategy/stops.py
    atr_value = ev.atr_value(sym, spec.atr_indicator, i) if spec.type == "atr_multiple" else None
    return compute_stop_price(spec, entry_px, atr_value, below, side=side, kind=kind)
