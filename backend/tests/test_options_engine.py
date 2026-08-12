"""Options in the backtest engine.

THE CENTRAL TEST HERE is `test_every_trade_pnl_checks_out_by_hand`: it
recomputes every trade's P&L from the row's own fields and asserts the engine
agrees. Plan/16 §18 names the multiplier as the second-biggest risk in this
whole feature — a missed x100 produces numbers that look entirely plausible and
are wrong by two orders of magnitude — and this is the test that catches it.
Plan 15 did exactly this for shorts (538 trades checked by hand) and it found
real bugs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import BacktestConfig, run_backtest
from app.backtest.tick_source import TickQuoteSource
from app.strategy.schema import StrategyDocument

DAY_MS = 86_400_000


def make_bars(n=400, start="2024-01-02", seed=7, drift=0.0004, vol=0.011,
              start_px=100.0) -> pd.DataFrame:
    """Deterministic daily bars. Seeded, so a failure is always reproducible."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = start_px * np.exp(np.cumsum(rets))
    idx = pd.date_range(start, periods=n, freq="B", tz="UTC")
    high = close * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.004, n)))
    open_ = np.concatenate([[start_px], close[:-1]])
    return pd.DataFrame({"open": open_, "high": np.maximum(high, np.maximum(open_, close)),
                         "low": np.minimum(low, np.minimum(open_, close)),
                         "close": close, "volume": 1_000_000}, index=idx)


def options_doc(**overrides) -> StrategyDocument:
    """A simple, reliably-firing options strategy: buy a ~35-delta call when
    close > its 5-day SMA, exit on a 3% underlying stop / 6% target."""
    base = {
        "schema_version": 2,
        "meta": {"name": "Opt Engine Test"},
        "universe": {"symbols": ["SPY"]},
        "timeframe": {"bar": "1d"},
        "indicators": [{"id": "sma5", "type": "sma", "params": {"period": 5}}],
        "entry": {"long": {"when": {"op": "gt", "left": {"price": "close"},
                                    "right": {"ind": "sma5"}}},
                  "cooldown_bars": 3},
        "exit": {"stop_loss": {"type": "percent", "value": 3.0},
                 "take_profit": {"type": "percent", "value": 6.0}},
        "expression": {
            "instrument": "options",
            "long": {"right": "call",
                     "expiry": {"dte_min": 25, "dte_target": 35, "dte_max": 60},
                     "strike": {"by": "delta", "target": 0.35, "tolerance": 0.20}},
            "liquidity": {"min_open_interest": 0, "max_spread_pct": 50},
            "exit": {"stop_basis": "underlying", "close_at_dte": 5},
        },
        "sizing": {"type": "fixed_dollars", "value": 2000,
                   "max_position_notional": 500_000},
        "risk": {"max_open_positions": 1, "max_daily_loss": 100_000,
                 "options": {"max_premium_at_risk": 50_000,
                             "max_premium_per_trade_pct": 10.0}},
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return StrategyDocument.model_validate(base)


def run(doc=None, bars=None, **cfg_kw) -> tuple:
    bars = bars if bars is not None else make_bars()
    doc = doc or options_doc()
    cfg = BacktestConfig(start="2024-01-02", end="2025-08-01", initial_capital=100_000,
                         **cfg_kw)
    return run_backtest(doc, cfg, {"SPY": bars}), doc


# ---- it trades at all -----------------------------------------------------

def test_options_backtest_produces_trades():
    res, _ = run()
    assert not res.metrics.get("error"), res.metrics
    assert res.trades, "no options trades — check selection rejections: " + str(
        res.assumptions.get("options", {}).get("selection_rejections"))


def test_every_trade_is_an_option_and_self_describing():
    res, _ = run()
    for t in res.trades:
        assert t["instrument"] == "option"
        assert len(t["contract"]) == 21          # OCC symbol
        assert t["right"] == "C"
        assert t["multiplier"] == 100.0
        assert t["strike"] > 0
        assert t["expiry"]
        assert t["dte_at_entry"] > 0
        assert t["underlying_entry_px"] > 0


# ---- THE test -------------------------------------------------------------

def test_every_trade_pnl_checks_out_by_hand():
    """pnl == (exit - entry) x qty x multiplier - commissions, exactly, for
    every trade. This is what catches a missing x100."""
    res, _ = run()
    assert res.trades
    per_contract = 0.65
    for t in res.trades:
        gross = (t["exit_px"] - t["entry_px"]) * t["qty"] * t["multiplier"]
        commissions = 2 * per_contract * t["qty"]      # entry + exit
        expected = gross - commissions
        # `entry_px`/`exit_px` are display-rounded to 4dp while `pnl` is exact,
        # and the multiplier amplifies that rounding by qty x 100. Allow for it
        # explicitly rather than with a fudged constant — this tolerance is
        # cents, so a MISSING x100 (which would be wrong by ~100x) still fails
        # loudly, which is the whole point of this test.
        tol = 2e-4 * t["qty"] * t["multiplier"] + 0.01
        assert t["pnl"] == pytest.approx(expected, abs=tol), t


def test_pnl_pct_is_a_premium_return():
    res, _ = run()
    for t in res.trades:
        expected = (t["exit_px"] / t["entry_px"] - 1) * 100
        assert t["pnl_pct"] == pytest.approx(expected, abs=0.05)


def test_a_long_option_can_never_lose_more_than_its_premium():
    """The property the entire safety argument rests on (plan/16 D2/D3)."""
    res, _ = run()
    for t in res.trades:
        max_loss = t["premium_paid"] + 2 * 0.65 * t["qty"]
        assert t["pnl"] >= -max_loss - 0.01, t


def test_equity_curve_never_goes_negative_and_is_finite():
    res, _ = run()
    eq = [v for _, v in res.equity_points]
    assert eq
    assert all(v > 0 for v in eq)
    assert all(v == v for v in eq)


# ---- costs actually bite --------------------------------------------------

def test_commission_reduces_pnl():
    free, _ = run(commission={"per_order": 0.0, "per_contract": 0.0})
    paid, _ = run(commission={"per_order": 0.0, "per_contract": 0.65})
    assert free.trades and paid.trades
    assert sum(t["pnl"] for t in paid.trades) < sum(t["pnl"] for t in free.trades)


def test_crossing_the_spread_costs_money():
    """fill_aggression 1.0 (the default) must be strictly worse than 0.0."""
    mid_doc = options_doc()
    mid_doc.expression.pricing.fill_aggression = 0.0
    aggressive, _ = run()
    passive, _ = run(mid_doc)
    assert sum(t["pnl"] for t in aggressive.trades) < \
        sum(t["pnl"] for t in passive.trades)


def test_a_lower_vrp_makes_options_cheaper_and_flatters_the_result():
    """The failure mode plan/16 D6 exists to prevent, demonstrated: dial the
    variance risk premium down and long-premium performance improves for no
    reason other than the assumption."""
    cheap = options_doc()
    cheap.expression.pricing.vrp_mult = 0.6
    honest = options_doc()
    honest.expression.pricing.vrp_mult = 1.5
    # a realized-vol path is required for the VRP to apply at all (an anchor
    # index would already contain the premium), which is this test's setup
    r_cheap, _ = run(cheap)
    r_honest, _ = run(honest)
    if r_cheap.trades and r_honest.trades:
        assert r_cheap.trades[0]["entry_px"] < r_honest.trades[0]["entry_px"]


# ---- expiry lifecycle -----------------------------------------------------

def test_positions_are_closed_before_expiry_never_held_through():
    res, _ = run()
    assert res.trades
    for t in res.trades:
        assert t["dte_at_exit"] >= 0, "a position was held past expiration"
    assert res.assumptions["options"]["expired_unclosed_count"] == 0


def test_dte_close_fires_when_nothing_else_exits_first():
    """A strategy with no stop/target must still exit at close_at_dte."""
    doc = options_doc()
    doc.exit.stop_loss = None
    doc.exit.take_profit = None
    res, _ = run(doc)
    assert res.trades
    assert any(t["exit_reason"] == "dte_close" for t in res.trades)


def test_close_at_dte_is_respected():
    doc = options_doc()
    doc.expression.exit.close_at_dte = 10
    res, _ = run(doc)
    for t in res.trades:
        if t["exit_reason"] == "dte_close":
            assert t["dte_at_exit"] <= 10


# ---- underlying-based stops still drive the exits -------------------------

def test_underlying_stops_and_targets_fire():
    """The payoff of plan/16 D1: an existing percent/ATR stop keeps working
    with no exit-logic changes, it just closes a contract instead of shares."""
    res, _ = run()
    reasons = {t["exit_reason"] for t in res.trades}
    assert reasons & {"stop", "stop_gap", "target", "target_gap", "dte_close"}


def test_stop_exit_records_the_underlying_move_that_caused_it():
    res, _ = run()
    stops = [t for t in res.trades if t["exit_reason"] in ("stop", "stop_gap")]
    for t in stops:
        # the underlying fell at least ~3% from entry (the configured stop)
        move = t["underlying_exit_px"] / t["underlying_entry_px"] - 1
        assert move < -0.02, t


# ---- puts -----------------------------------------------------------------

def put_doc(delta_target=0.35) -> StrategyDocument:
    """A put-buying strategy, built from a dict rather than by mutating a call
    document — mutation leaves half-validated sub-models behind."""
    return options_doc(
        entry={"long": None,
               "short": {"when": {"op": "lt", "left": {"price": "close"},
                                  "right": {"ind": "sma5"}}},
               "cooldown_bars": 3},
        expression={
            "instrument": "options",
            "long": None,
            "short": {"right": "put",
                      "expiry": {"dte_min": 25, "dte_target": 35, "dte_max": 60},
                      "strike": {"by": "delta", "target": delta_target,
                                 "tolerance": 0.25}},
            "liquidity": {"min_open_interest": 0, "max_spread_pct": 50},
            "exit": {"stop_basis": "underlying", "close_at_dte": 5},
        })


def test_short_signals_buy_puts():
    res, _ = run(put_doc())
    assert res.trades, res.assumptions.get("options", {}).get("selection_rejections")
    for t in res.trades:
        assert t["right"] == "P"
        assert t["side"] == "short"


def test_puts_profit_when_the_underlying_falls():
    """A put is long premium: it needs no margin and no borrow, and it makes
    money when the underlying drops."""
    res, _ = run(put_doc(0.40), bars=make_bars(drift=-0.002, seed=11))
    assert res.trades
    winners = [t for t in res.trades
               if t["underlying_exit_px"] < t["underlying_entry_px"]]
    assert winners, "the downtrend produced no favourable put trades"
    assert any(t["pnl"] > 0 for t in winners)


# ---- sizing and risk caps -------------------------------------------------

def test_premium_at_risk_cap_limits_position_size():
    doc = options_doc()
    doc.risk.options.max_premium_at_risk = 500
    res, _ = run(doc)
    for t in res.trades:
        assert t["premium_paid"] <= 500 + 1e-6


def test_max_contracts_per_position_is_a_hard_backstop():
    doc = options_doc()
    doc.risk.options.max_contracts_per_position = 2
    doc.sizing.value = 1_000_000
    res, _ = run(doc)
    for t in res.trades:
        assert t["qty"] <= 2


def test_sizing_to_zero_is_recorded_with_the_binding_cap():
    """'My strategy takes no trades' must always be answerable."""
    doc = options_doc()
    doc.risk.options.max_premium_per_trade_pct = 0.0001
    res, _ = run(doc)
    rej = res.assumptions["options"]["selection_rejections"]
    assert any(k.startswith("sized_to_zero:") for k in rej), rej


# ---- the honesty block ----------------------------------------------------

def test_assumptions_block_is_complete_and_honest():
    res, _ = run()
    a = res.assumptions["options"]
    assert a["pricing_source"] == "synthetic"
    assert a["pct_bars_recorded"] == 0.0
    assert "Black-Scholes" in a["model"]
    assert a["vrp_mult"] == 1.15
    assert a["commission_per_contract"] == 0.65
    assert a["fill_aggression"] == 1.0
    assert "min_open_interest" in a["liquidity_NOT_enforced"]
    assert "IGNORED" in a["intrabar_iv"]
    assert a["assignment"].startswith("not modelled")
    assert isinstance(a["selection_rejections"], dict)
    assert a["vol_source_mix"]


def test_sharpe_carries_a_distribution_warning():
    """Long-option returns are floored at -100% and fat-tailed; Sharpe on them
    is close to meaningless and the result must say so."""
    res, _ = run()
    assert "return_distribution_warning" in res.metrics


def test_shares_backtests_get_no_options_assumptions():
    """The other half of the guarantee: an equity strategy is untouched."""
    doc = options_doc()
    doc.expression = None
    doc = StrategyDocument.model_validate(doc.model_dump(mode="json"))
    res, _ = run(doc)
    assert "options" not in res.assumptions
    assert "return_distribution_warning" not in res.metrics
    assert all(t.get("instrument") != "option" for t in res.trades)


# ---- open positions -------------------------------------------------------

def test_open_option_positions_are_reported_with_contract_detail():
    doc = options_doc()
    doc.entry.cooldown_bars = 0
    res, _ = run(doc)
    for p in res.open_positions:
        if p.get("instrument") == "option":
            assert len(p["contract"]) == 21
            assert p["last_px"] >= 0
            assert "underlying_last_px" in p


# ---- volatility anchor ($VIX) --------------------------------------------

def test_vix_anchor_is_used_when_available_and_aligned_to_the_traded_bars():
    """The anchor is looked up by POSITIONAL index into the traded symbol's
    frame, so it must be reindexed onto those bars. A raw anchor series has its
    own length and holidays and would silently read the wrong date's
    volatility, drifting further out of alignment the longer the run."""
    bars = make_bars(n=300)
    # a VIX series on a DIFFERENT (sparser) calendar, to prove alignment happens
    vix_idx = bars.index[::2]
    vix = pd.DataFrame({"open": 18.0, "high": 18.0, "low": 18.0,
                        "close": np.linspace(12.0, 34.0, len(vix_idx)),
                        "volume": 0}, index=vix_idx)
    doc = options_doc()
    cfg = BacktestConfig(start="2024-01-02", end="2025-08-01", initial_capital=100_000)
    res = run_backtest(doc, cfg, {"SPY": bars, "$VIX": vix})
    mix = res.assumptions["options"]["vol_source_mix"]
    assert "vol_index_anchor" in mix, mix
    assert mix["vol_index_anchor"] > 50


def test_without_an_anchor_the_result_says_realized_vol_was_used():
    """Falling back is allowed; hiding it is not."""
    res, _ = run()
    mix = res.assumptions["options"]["vol_source_mix"]
    assert "vol_index_anchor" not in mix
    assert any(k.startswith("realized") or k == "fallback_flat" for k in mix), mix


def test_higher_vix_makes_options_more_expensive():
    """Sanity: the anchor must actually drive price."""
    bars = make_bars(n=300)
    def entry_premium(level):
        vix = pd.DataFrame({"open": level, "high": level, "low": level,
                            "close": float(level), "volume": 0}, index=bars.index)
        cfg = BacktestConfig(start="2024-01-02", end="2025-08-01",
                             initial_capital=100_000)
        r = run_backtest(options_doc(), cfg, {"SPY": bars, "$VIX": vix})
        return r.trades[0]["entry_px"] if r.trades else None

    calm, wild = entry_premium(12.0), entry_premium(45.0)
    assert calm and wild
    assert wild > calm * 1.5


# ---- recorded quotes / intrabar premium -----------------------------------
#
# What this covers: a 30-minute chain snapshot is a POINT SAMPLE, so a contract
# that spiked and came back inside a bar looks like it never moved, and a
# premium stop that would have fired in life never fires. Recorded ticks are
# what make that simulable — and the result must SAY how much of it was
# recorded versus modelled.

def _ticks(symbol, rows):
    """rows: (ts_ms, bid, ask)"""
    return [{"symbol": symbol, "ts": t, "bid": b, "ask": a,
             "mark": (b + a) / 2} for t, b, a in rows]


def test_tick_source_is_backward_only():
    """Reaching forward imports information the strategy could not have had,
    and on a fast-repricing option that error is large and flattering."""
    src = TickQuoteSource.from_rows(_ticks("X", [(1_000, 1.0, 1.1),
                                                 (5_000, 9.0, 9.1)]))
    assert src.quote("X", 4_999)[0] == pytest.approx(1.0)   # not the 5s quote
    assert src.quote("X", 5_000)[0] == pytest.approx(9.0)
    assert src.quote("X", 999) is None                      # nothing earlier


def test_tick_source_refuses_a_stale_quote():
    """A contract that has not quoted in minutes has an ask nobody will honour;
    treating it as live manufactures fills at prices that never existed."""
    src = TickQuoteSource.from_rows(_ticks("X", [(1_000, 1.0, 1.1)]),
                                    max_age_ms=60_000)
    assert src.quote("X", 60_000) is not None
    assert src.quote("X", 62_000) is None


def test_tick_source_sorts_unordered_input():
    src = TickQuoteSource.from_rows(_ticks("X", [(5_000, 9.0, 9.1),
                                                 (1_000, 1.0, 1.1)]))
    assert src.quote("X", 2_000)[0] == pytest.approx(1.0)


def test_tick_source_extremes_within_a_bar():
    """The intrabar path: open/close say nothing about what happened between."""
    src = TickQuoteSource.from_rows(_ticks("X", [
        (1_000, 6.4, 6.5), (1_500, 9.8, 9.9), (2_000, 6.5, 6.6)]))
    lo, hi = src.extremes("X", 999, 2_000)
    assert lo == pytest.approx(6.45)
    assert hi == pytest.approx(9.85)
    # an empty window is None — "unknown", never "unchanged"
    assert src.extremes("X", 3_000, 4_000) is None


def test_tick_source_hit_rate_is_reported():
    src = TickQuoteSource.from_rows(_ticks("X", [(1_000, 1.0, 1.1)]))
    src.quote("X", 1_500)
    src.quote("MISSING", 1_500)
    assert src.stats()["quote_hits"] == 1
    assert src.stats()["quote_misses"] == 1
    assert src.stats()["hit_rate_pct"] == pytest.approx(50.0)


def _bar_ms(bars, i):
    return int(bars.index[i].timestamp() * 1000)


def test_intrabar_premium_stop_fires_on_recorded_quotes():
    """THE test for intrabar fidelity.

    Construct a contract whose premium collapses through the max-loss level
    *inside* a bar and recovers by the close. With only bar-close prices the
    position looks untouched; against recorded quotes the stop fires — which is
    what happens in life, because max_loss_pct_of_premium rests at the broker."""
    bars = make_bars()
    base, doc = run(bars=bars)
    assert base.trades, "need a baseline trade to build ticks against"
    t0 = base.trades[0]
    sym = t0["contract"]
    entry_px = t0["entry_px"]
    entry_i = list(bars.index).index(pd.Timestamp(t0["entry_ts"], unit="ms", tz="UTC"))

    # Default max_loss_pct_of_premium is 60% -> stop at 40% of entry premium.
    stop_level = entry_px * 0.40
    rows = []
    # Bar of entry and the two after it: a deep spike DOWN mid-bar that recovers
    # to roughly the entry premium by the close.
    for i in (entry_i, entry_i + 1, entry_i + 2):
        ts = _bar_ms(bars, i)
        rows += _ticks(sym, [
            (ts - 3600_000, entry_px, entry_px * 1.02),
            (ts - 1800_000, stop_level * 0.5, stop_level * 0.52),   # the spike
            (ts, entry_px * 0.99, entry_px * 1.01),                 # recovered
        ])

    res = run_backtest(doc, BacktestConfig(start="2024-01-02", end="2025-08-01",
                                           initial_capital=100_000),
                       {"SPY": bars}, option_ticks=rows)
    reasons = [t["exit_reason"] for t in res.trades]
    assert "premium_stop" in reasons, reasons
    opts = res.assumptions["options"]
    assert opts["intrabar_premium_bars"] > 0
    assert "RECORDED" in opts["intrabar_iv"]


def test_without_ticks_the_intrabar_path_is_inert():
    """No recordings -> behaviour is bit-for-bit what it was before, and the
    assumptions block keeps the old warning rather than implying fidelity it
    does not have."""
    res, _ = run()
    opts = res.assumptions["options"]
    assert opts["pricing_source"] == "synthetic"
    assert opts["pct_bars_recorded"] == 0.0
    assert "IGNORED" in opts["intrabar_iv"]
    assert opts["intrabar_premium_bars"] == 0


def test_recorded_quotes_are_preferred_and_counted():
    bars = make_bars()
    base, doc = run(bars=bars)
    t0 = base.trades[0]
    sym = t0["contract"]
    # A recorded quote on every bar of the window for the first traded contract.
    rows = []
    for i in range(len(bars)):
        rows += _ticks(sym, [(_bar_ms(bars, i), t0["entry_px"], t0["entry_px"] * 1.02)])

    res = run_backtest(doc, BacktestConfig(start="2024-01-02", end="2025-08-01",
                                           initial_capital=100_000),
                       {"SPY": bars}, option_ticks=rows)
    opts = res.assumptions["options"]
    assert opts["quotes_recorded"] > 0
    assert opts["pricing_source"] in ("mixed", "recorded")
    assert 0 < opts["pct_bars_recorded"] <= 100
    assert opts["tick_source"]["contracts"] == 1


def test_pricing_source_synthetic_ignores_recordings():
    """The only way to compare a recorded result against the modelled one it
    replaces."""
    bars = make_bars()
    base, doc = run(bars=bars)
    t0 = base.trades[0]
    rows = []
    for i in range(len(bars)):
        rows += _ticks(t0["contract"],
                       [(_bar_ms(bars, i), t0["entry_px"], t0["entry_px"] * 1.02)])
    cfg = BacktestConfig(start="2024-01-02", end="2025-08-01",
                         initial_capital=100_000,
                         options={"pricing_source": "synthetic"})
    res = run_backtest(doc, cfg, {"SPY": bars}, option_ticks=rows)
    assert res.assumptions["options"]["pricing_source"] == "synthetic"
    assert res.assumptions["options"]["quotes_recorded"] == 0


def test_pricing_source_recorded_refuses_to_model():
    """A window with no recording must produce NO trades rather than modelled
    ones — that is what makes a 'fully recorded' claim checkable."""
    cfg = BacktestConfig(start="2024-01-02", end="2025-08-01",
                         initial_capital=100_000,
                         options={"pricing_source": "recorded"})
    res = run_backtest(options_doc(), cfg, {"SPY": make_bars()}, option_ticks=[])
    assert res.trades == []
    assert res.assumptions["options"]["missing_quote_count"] > 0
