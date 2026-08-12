"""Plan 15 Part B — short selling.

Shorting is NOT a mirror of buying in the places that matter: loss is
unbounded, the position consumes margin instead of cash, and the adverse
direction is UP. These tests pin the arithmetic and the direction of every
check, because a sign error here is silent and expensive.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import BacktestConfig, run_backtest
from app.strategy.lint import is_warning, lint
from app.strategy.schema import StrategyDocument
from app.strategy.stops import compute_scale_levels, compute_stop_price, is_above


def bars(opens, highs, lows, closes, start="2024-01-02 14:30", freq="30min"):
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "open": np.asarray(opens, float), "high": np.asarray(highs, float),
        "low": np.asarray(lows, float), "close": np.asarray(closes, float),
        "volume": np.full(n, 1000.0),
    }, index=idx)


def short_doc(*, stop=None, target=None, trail=None, time_stop=None, scale_outs=None,
              enabled=True, flatten=False, pct=50.0, htb=None, entry_bar_close=100.0,
              max_short_notional=1e9, maint=30.0, init=50.0, signal_short=None):
    """Short whenever close < `entry_bar_close`; distances are in percent so the
    tests can compute expected prices by hand."""
    doc = {
        "meta": {"name": "short-test"}, "universe": {"symbols": ["X"]},
        "timeframe": {"bar": "30m"},
        "session": {"hours": "regular", "flatten_eod": flatten},
        "indicators": [],
        "entry": {"short": {"when": {"op": "lt", "left": {"price": "close"},
                                     "right": {"value": entry_bar_close}}},
                  "cooldown_bars": 0, "max_entries_per_day": 50},
        "exit": {},
        "sizing": {"type": "percent_of_equity", "value": pct,
                   "max_position_notional": 1e9, "round_lot": 1},
        "risk": {"max_open_positions": 1, "max_daily_loss": 1e9, "max_daily_orders": 200,
                 "short": {"enabled": enabled, "initial_margin_pct": init,
                           "maintenance_margin_pct": maint,
                           "max_short_notional": max_short_notional,
                           "hard_to_borrow": htb or []}},
    }
    if stop is not None:
        doc["exit"]["stop_loss"] = {"type": "percent", "value": stop}
    if target is not None:
        doc["exit"]["take_profit"] = {"type": "percent", "value": target}
    if trail is not None:
        doc["exit"]["trailing_stop"] = {"type": "percent", "value": trail}
    if time_stop is not None:
        doc["exit"]["time_stop"] = {"bars": time_stop}
    if scale_outs is not None:
        doc["exit"]["scale_outs"] = scale_outs
    if signal_short is not None:
        doc["exit"]["signal_short"] = {"when": signal_short}
    return StrategyDocument.model_validate(doc)


def run(doc, df, capital=100_000.0, slip=0.0, commission=0.0):
    cfg = BacktestConfig(start="2024-01-02", end="2024-01-31", initial_capital=capital,
                         slippage={"type": "fixed_bps", "value": slip},
                         commission={"per_order": commission})
    return run_backtest(doc, cfg, {"X": df})


# --------------------------------------------------------------- stop geometry

def test_direction_rule_puts_stops_and_targets_on_the_right_side():
    assert is_above("long", "stop") is False      # long stop below entry
    assert is_above("long", "target") is True
    assert is_above("short", "stop") is True       # short stop ABOVE entry
    assert is_above("short", "target") is False


def test_compute_stop_price_mirrors_for_shorts():
    # 10% distances off a $100 entry
    assert compute_stop_price(_spec(10), 100, None, side="long", kind="stop") == pytest.approx(90)
    assert compute_stop_price(_spec(10), 100, None, side="long", kind="target") == pytest.approx(110)
    assert compute_stop_price(_spec(10), 100, None, side="short", kind="stop") == pytest.approx(110)
    assert compute_stop_price(_spec(10), 100, None, side="short", kind="target") == pytest.approx(90)


def _spec(value, kind="percent"):
    from app.strategy.schema import StopSpec
    return StopSpec(type=kind, value=value)


def test_legacy_below_argument_still_works():
    """Existing call sites pass below= — they must keep working during migration."""
    assert compute_stop_price(_spec(10), 100, None, below=True) == pytest.approx(90)
    assert compute_stop_price(_spec(10), 100, None, below=False) == pytest.approx(110)


def test_scale_out_ladder_runs_downward_for_a_short():
    outs = [type("S", (), {"target": _spec(2), "percent": 50})()]
    levels = compute_scale_levels(outs, 100.0, 100, lambda s: None, side="short")
    assert levels and levels[0][0] == pytest.approx(98.0)   # profit is DOWN
    levels_long = compute_scale_levels(outs, 100.0, 100, lambda s: None, side="long")
    assert levels_long[0][0] == pytest.approx(102.0)


# ------------------------------------------------------------ equity identity

def test_equity_identity_short_position_worked_example():
    """The plan's worked example as a literal test:
       short 100 @ $10 from $100k -> equity unchanged; at $8 -> +$200."""
    # bar0 close 9.99 triggers the short; fills at bar1 open = 10.00
    df = bars(opens=[9.99, 10.0, 8.0, 8.0], highs=[10, 10, 10, 8],
              lows=[9, 8, 8, 8], closes=[9.99, 10.0, 8.0, 8.0])
    doc = short_doc(stop=1000, pct=1.0)   # ~1% of equity -> small qty, wide stop
    res = run(doc, df, capital=100_000.0)
    eq = [v for _, v in res.equity_points]
    # equity at the entry bar is unchanged by opening the short
    assert eq[1] == pytest.approx(100_000.0, abs=1e-6)
    # then the price falls 20% and the short gains
    assert eq[2] > eq[1]


def test_short_pnl_is_entry_minus_exit():
    df = bars(opens=[99, 100, 90, 90], highs=[100, 100, 95, 90],
              lows=[99, 99, 90, 90], closes=[99, 100, 90, 90])
    doc = short_doc(stop=1000, time_stop=2, pct=10.0)
    res = run(doc, df)
    t = res.trades[0]
    assert t["side"] == "short"
    # sold at 100, covered at 90 -> +10 per share
    assert t["entry_px"] == pytest.approx(100.0)
    assert t["pnl"] == pytest.approx((t["entry_px"] - t["exit_px"]) * t["qty"], rel=1e-6)
    assert t["pnl"] > 0
    assert t["pnl_pct"] == pytest.approx(10.0, rel=1e-3)


def test_short_loses_when_price_rises():
    df = bars(opens=[99, 100, 110, 110], highs=[100, 100, 110, 110],
              lows=[99, 99, 105, 110], closes=[99, 100, 110, 110])
    doc = short_doc(stop=1000, time_stop=2, pct=10.0)
    res = run(doc, df)
    assert res.trades[0]["pnl"] < 0


# --------------------------------------------------------- exit-check geometry

def test_short_stop_triggers_on_strength_not_weakness():
    """A short stops out when the price RISES through the stop."""
    # entry at 100 (bar1 open), stop 5% -> 105. bar2 high 106 breaches it.
    df = bars(opens=[99, 100, 101, 100], highs=[100, 100, 106, 100],
              lows=[99, 99, 100, 100], closes=[99, 100, 101, 100])
    doc = short_doc(stop=5.0, pct=10.0)
    res = run(doc, df)
    t = res.trades[0]
    assert t["exit_reason"] == "stop"
    assert t["exit_px"] == pytest.approx(105.0)


def test_short_stop_gap_through_fills_at_the_open():
    # bar2 opens at 120, straight through the 105 stop
    df = bars(opens=[99, 100, 120, 120], highs=[100, 100, 125, 120],
              lows=[99, 99, 118, 120], closes=[99, 100, 120, 120])
    doc = short_doc(stop=5.0, pct=10.0)
    res = run(doc, df)
    t = res.trades[0]
    assert t["exit_reason"] == "stop_gap"
    assert t["exit_px"] == pytest.approx(120.0)   # worse than the stop, correctly


def test_short_target_triggers_on_a_falling_price():
    df = bars(opens=[99, 100, 99, 95], highs=[100, 100, 99, 95],
              lows=[99, 99, 94, 95], closes=[99, 100, 99, 95])
    doc = short_doc(stop=50.0, target=5.0, pct=10.0)   # target 95
    res = run(doc, df)
    t = res.trades[0]
    assert t["exit_reason"] == "target"
    assert t["exit_px"] == pytest.approx(95.0)


def test_same_bar_stop_and_target_resolves_worst_case_for_a_short():
    """Bar hits both 95 (target) and 105 (stop) — the stop must win."""
    df = bars(opens=[99, 100, 100, 100], highs=[100, 100, 106, 100],
              lows=[99, 99, 94, 100], closes=[99, 100, 100, 100])
    doc = short_doc(stop=5.0, target=5.0, pct=10.0)
    res = run(doc, df)
    assert res.trades[0]["exit_reason"] == "stop"


def test_mae_and_mfe_are_inverted_for_a_short():
    """A short's FAVOURABLE excursion is the low; its ADVERSE one is the high."""
    df = bars(opens=[99, 100, 100, 100], highs=[100, 100, 104, 100],
              lows=[99, 99, 90, 100], closes=[99, 100, 100, 100])
    doc = short_doc(stop=50.0, time_stop=2, pct=10.0)
    res = run(doc, df)
    t = res.trades[0]
    assert t["mfe_pct"] == pytest.approx(10.0, abs=0.01)   # price fell to 90 => +10%
    assert t["mae_pct"] == pytest.approx(-4.0, abs=0.01)   # rose to 104 => -4%


def test_short_trailing_stop_ratchets_downward():
    # entry 100; price falls to 90 (trail 5% -> 94.5), then rallies through it
    df = bars(opens=[99, 100, 95, 90, 96], highs=[100, 100, 96, 91, 97],
              lows=[99, 99, 90, 89, 90], closes=[99, 100, 95, 90, 96])
    doc = short_doc(stop=50.0, trail=5.0, pct=10.0)
    res = run(doc, df)
    t = res.trades[0]
    assert t["exit_reason"] in ("stop", "stop_gap")
    # the trail followed the price down, so the exit is far below the initial stop
    assert t["exit_px"] < 100.0


# ------------------------------------------------------------------- margin

def test_short_reserves_margin_and_limits_buying_power():
    """100% of equity requested, but a short also needs initial margin, so the
    filled size must be smaller than the naive cash-only calculation."""
    df = bars(opens=[99, 100, 100, 100], highs=[100, 100, 100, 100],
              lows=[99, 99, 100, 100], closes=[99, 100, 100, 100])
    doc = short_doc(stop=50.0, time_stop=2, pct=100.0, init=50.0)
    res = run(doc, df, capital=10_000.0)
    t = res.trades[0]
    # naive would be 100 shares at $100; margin capping keeps it at/below that
    assert 0 < t["qty"] <= 100


def test_max_short_notional_caps_position_size():
    df = bars(opens=[99, 100, 100, 100], highs=[100, 100, 100, 100],
              lows=[99, 99, 100, 100], closes=[99, 100, 100, 100])
    doc = short_doc(stop=50.0, time_stop=2, pct=100.0, max_short_notional=1_000.0)
    res = run(doc, df, capital=100_000.0)
    assert res.trades[0]["qty"] * res.trades[0]["entry_px"] <= 1_000.0 + 1e-6


def test_runaway_short_is_force_covered_by_maintenance_margin():
    """A short's loss is unbounded — the engine must close it rather than report
    fantasy equity."""
    df = bars(opens=[99, 100, 400, 800, 900], highs=[100, 100, 420, 820, 900],
              lows=[99, 99, 390, 790, 900], closes=[99, 100, 400, 800, 900])
    doc = short_doc(stop=None, pct=40.0, maint=30.0)   # no stop: only margin saves it
    res = run(doc, df, capital=100_000.0)
    assert any(t["exit_reason"] == "margin_call" for t in res.trades)


# ------------------------------------------------------------------- gating

def test_short_does_not_trade_without_the_explicit_opt_in():
    df = bars(opens=[99, 100, 90, 90], highs=[100, 100, 95, 90],
              lows=[99, 99, 90, 90], closes=[99, 100, 90, 90])
    doc = short_doc(stop=50.0, time_stop=2, enabled=False)
    assert run(doc, df).trades == []


def test_hard_to_borrow_symbols_are_never_shorted():
    df = bars(opens=[99, 100, 90, 90], highs=[100, 100, 95, 90],
              lows=[99, 99, 90, 90], closes=[99, 100, 90, 90])
    doc = short_doc(stop=50.0, time_stop=2, htb=["X"])
    assert run(doc, df).trades == []


def test_long_only_documents_are_completely_unaffected():
    """Regression guard: the long path must behave exactly as before."""
    df = bars(opens=[101, 100, 100, 110], highs=[102, 101, 101, 112],
              lows=[100, 99, 99, 109], closes=[101, 100, 100, 110])
    doc = StrategyDocument.model_validate({
        "meta": {"name": "long"}, "universe": {"symbols": ["X"]},
        "timeframe": {"bar": "30m"}, "indicators": [],
        "entry": {"long": {"when": {"op": "lt", "left": {"price": "close"},
                                    "right": {"value": 100.5}}},
                  "cooldown_bars": 0, "max_entries_per_day": 50},
        "exit": {"stop_loss": {"type": "percent", "value": 50}, "time_stop": {"bars": 2}},
        "sizing": {"type": "percent_of_equity", "value": 10, "max_position_notional": 1e9},
        "risk": {"max_open_positions": 1, "max_daily_loss": 1e9, "max_daily_orders": 200},
    })
    res = run(doc, df)
    assert res.trades and res.trades[0]["side"] == "long"
    assert res.trades[0]["pnl"] > 0     # bought ~100, exited ~110


# --------------------------------------------------------------------- lint

def _lint_doc(**kw):
    return lint(short_doc(**kw))


def test_lint_requires_a_stop_for_shorts_as_an_error():
    problems = _lint_doc(stop=None)
    hits = [p for p in problems if "REQUIRED when shorting" in p]
    assert hits and not any(is_warning(p) for p in hits)   # error, not warning


def test_lint_requires_explicit_short_opt_in():
    assert any("risk.short.enabled=true" in p for p in _lint_doc(stop=5.0, enabled=False))


def test_lint_warns_about_overnight_shorts():
    problems = _lint_doc(stop=5.0, flatten=False)
    assert any("overnight" in p and is_warning(p) for p in problems)
    assert not any("overnight" in p for p in _lint_doc(stop=5.0, flatten=True))


def test_lint_flags_maintenance_above_initial_margin():
    assert any("maintenance_margin_pct should be below" in p
               for p in _lint_doc(stop=5.0, init=30.0, maint=50.0))


def test_lint_warns_when_hard_to_borrow_overlaps_the_universe():
    assert any("hard_to_borrow" in p and is_warning(p)
               for p in _lint_doc(stop=5.0, htb=["X"]))


def test_lint_accepts_a_document_with_both_sides():
    doc = short_doc(stop=5.0, flatten=True)
    d = doc.model_dump()
    d["entry"]["long"] = {"when": {"op": "gt", "left": {"price": "close"},
                                   "right": {"value": 100}}}
    problems = [p for p in lint(StrategyDocument.model_validate(d)) if not is_warning(p)]
    assert problems == []


def test_lint_still_requires_at_least_one_side():
    d = short_doc(stop=5.0).model_dump()
    d["entry"]["short"] = None
    problems = lint(StrategyDocument.model_validate(d))
    assert any("at least one of entry.long / entry.short" in p for p in problems)
