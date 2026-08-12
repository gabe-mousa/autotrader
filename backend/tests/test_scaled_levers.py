"""Tests for the three levers that used to be reported as "not fully supported"
by AI generation: operand scaling (`mul`/`add`), partial/scaled exits
(`exit.scale_outs`), and a scanner-sized universe (up to 100 symbols)."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import BacktestConfig, run_backtest
from app.strategy.evaluator import StrategyEvaluator
from app.strategy.lint import is_warning, lint
from app.strategy.schema import StrategyDocument
from app.strategy.stops import compute_scale_levels
from tests.test_strategy import EXAMPLE

CFG = BacktestConfig(start="2024-01-01", end="2025-12-31", initial_capital=100_000,
                     slippage={"type": "fixed_bps", "value": 0},
                     commission={"per_order": 0})


def make_doc(**sections) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    data.update(sections)
    return StrategyDocument.model_validate(data)


def errors_of(doc: StrategyDocument) -> list[str]:
    return [p for p in lint(doc) if not is_warning(p)]


def frame(closes, volumes=None, start="2024-01-01"):
    n = len(closes)
    idx = pd.bdate_range(start, periods=n, tz="UTC") + pd.Timedelta(hours=21)
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes,
        "volume": np.full(n, 1_000_000.0) if volumes is None
                  else np.asarray(volumes, dtype=float),
    }, index=idx)


# ---- operand scaling (mul / add) ------------------------------------------

def test_operand_mul_scales_the_series():
    """`volume > vol_sma * 1.5` — the exact threshold the AI used to have to
    approximate as a plain above-average filter."""
    doc = make_doc(indicators=[{"id": "vsma", "type": "volume_sma", "params": {"period": 3}}])
    ev = StrategyEvaluator(doc)
    # NB volume_sma(3) includes the spike bar itself: with a 1400 spike the
    # average is (1000+1000+1400)/3 = 1133, so 1400 clears the plain average
    # but not the 1.5x threshold (1700). The multiplier is load-bearing.
    df = frame([100] * 6, volumes=[1000, 1000, 1000, 1000, 1000, 1400])
    ev.compile_symbol("SPY", df)
    sym = ev.sym("SPY")

    plain = {"op": "gt", "left": {"price": "volume"}, "right": {"ind": "vsma"}}
    scaled = {"op": "gt", "left": {"price": "volume"},
              "right": {"ind": "vsma", "mul": 1.5}}
    i = len(df) - 1
    assert sym.eval_expr(_expr(plain), i) is True
    assert sym.eval_expr(_expr(scaled), i) is False

    # a 3000 spike gives an average of 1666.7 and a threshold of 2500 — cleared
    df2 = frame([100] * 6, volumes=[1000, 1000, 1000, 1000, 1000, 3000])
    ev.compile_symbol("SPY", df2)
    assert ev.sym("SPY").eval_expr(_expr(scaled), i) is True


def test_operand_add_offsets_the_series():
    doc = make_doc(indicators=[{"id": "sma3", "type": "sma", "params": {"period": 3}}])
    ev = StrategyEvaluator(doc)
    df = frame([100, 100, 100, 100.5])
    ev.compile_symbol("SPY", df)
    sym, i = ev.sym("SPY"), 3
    # close (100.5) is above sma3 but not above sma3 + 1
    assert sym.eval_expr(_expr({"op": "gt", "left": {"price": "close"},
                                "right": {"ind": "sma3"}}), i) is True
    assert sym.eval_expr(_expr({"op": "gt", "left": {"price": "close"},
                                "right": {"ind": "sma3", "add": 1.0}}), i) is False


def test_scaling_defaults_are_identity():
    """Every pre-existing document must evaluate exactly as before."""
    doc = make_doc(indicators=[{"id": "sma3", "type": "sma", "params": {"period": 3}}])
    left = doc.entry.long.when.all[0].left
    assert left.mul == 1.0 and left.add == 0.0


def test_ind_offset_operand_scales_too():
    doc = make_doc(indicators=[{"id": "sma2", "type": "sma", "params": {"period": 2}}])
    ev = StrategyEvaluator(doc)
    df = frame([100, 100, 100, 100])
    ev.compile_symbol("SPY", df)
    op = {"op": "gt", "left": {"price": "close"},
          "right": {"ind_offset": {"ind": "sma2", "bars_ago": 1}, "mul": 2.0}}
    # close 100 vs 2x the prior sma (200) -> false; without mul it would tie
    assert ev.sym("SPY").eval_expr(_expr(op), 3) is False


def test_mul_zero_is_a_lint_error():
    doc = make_doc(entry={"long": {"when": {
        "op": "gt", "left": {"price": "close"}, "right": {"ind": "rsi14", "mul": 0}}}})
    assert any("mul=0" in p for p in errors_of(doc))


def test_value_operand_rejects_scaling():
    """A literal is already a constant — `mul` on it would be a silent no-op,
    so the schema refuses it rather than accepting a misleading document."""
    with pytest.raises(Exception):
        make_doc(entry={"long": {"when": {
            "op": "gt", "left": {"price": "close"},
            "right": {"value": 30, "mul": 1.5}}}})


# ---- universe size --------------------------------------------------------

def test_universe_accepts_100_symbols():
    symbols = [f"SYM{i}" for i in range(100)]
    doc = make_doc(universe={"symbols": symbols})
    assert len(doc.universe.symbols) == 100
    assert errors_of(doc) == []


def test_universe_rejects_101_symbols():
    with pytest.raises(Exception):
        make_doc(universe={"symbols": [f"SYM{i}" for i in range(101)]})


# ---- scale-out ladder construction ----------------------------------------

def test_compute_scale_levels_floors_shares_and_keeps_a_remainder():
    doc = make_doc(exit={
        "stop_loss": {"type": "percent", "value": 1},
        "scale_outs": [{"target": {"type": "percent", "value": 1}, "percent": 50},
                       {"target": {"type": "percent", "value": 2}, "percent": 25}],
        "take_profit": {"type": "percent", "value": 3},
    })
    levels = compute_scale_levels(doc.exit.scale_outs, 100.0, 10, lambda spec: None)
    assert levels == [(101.0, 5), (102.0, 2)]
    assert sum(shares for _, shares in levels) < 10  # remainder always survives


def test_scale_level_rounding_to_zero_shares_is_dropped():
    doc = make_doc(exit={
        "stop_loss": {"type": "percent", "value": 1},
        "scale_outs": [{"target": {"type": "percent", "value": 1}, "percent": 10}],
        "take_profit": {"type": "percent", "value": 3},
    })
    # 10% of 5 shares floors to 0 -> no zero-share order is emitted
    assert compute_scale_levels(doc.exit.scale_outs, 100.0, 5, lambda spec: None) == []


# ---- scale-out lint -------------------------------------------------------

def _scale_doc(scale_outs, take_profit=None):
    exit_section = {"stop_loss": {"type": "percent", "value": 1}, "scale_outs": scale_outs}
    if take_profit:
        exit_section["take_profit"] = take_profit
    return make_doc(exit=exit_section)


def test_scale_outs_totalling_100_percent_rejected():
    doc = _scale_doc([{"target": {"type": "percent", "value": 1}, "percent": 60},
                      {"target": {"type": "percent", "value": 2}, "percent": 40}])
    assert any("must be under 100%" in p for p in errors_of(doc))


def test_scale_outs_must_increase_in_distance():
    doc = _scale_doc([{"target": {"type": "percent", "value": 2}, "percent": 30},
                      {"target": {"type": "percent", "value": 1}, "percent": 30}])
    assert any("increasing distance" in p for p in errors_of(doc))


def test_scale_out_beyond_take_profit_rejected():
    doc = _scale_doc([{"target": {"type": "percent", "value": 5}, "percent": 50}],
                     take_profit={"type": "percent", "value": 3})
    assert any("beyond take_profit" in p for p in errors_of(doc))


def test_valid_scale_ladder_lints_clean():
    doc = _scale_doc([{"target": {"type": "percent", "value": 1}, "percent": 50}],
                     take_profit={"type": "percent", "value": 2})
    assert errors_of(doc) == []


def test_scale_out_percent_bounds_enforced_by_schema():
    for bad in (0, 100, -5):
        with pytest.raises(Exception):
            _scale_doc([{"target": {"type": "percent", "value": 1}, "percent": bad}])


# ---- scale-outs through the backtest engine -------------------------------

def scale_backtest_doc(scale_outs, take_profit) -> StrategyDocument:
    """Buys on the first evaluable bar (RSI-free trigger) with a fixed 100-share
    position, so the share maths in the assertions are exact."""
    data = copy.deepcopy(EXAMPLE)
    data["indicators"] = [{"id": "sma2", "type": "sma", "params": {"period": 2}}]
    data["entry"] = {"long": {"when": {
        "op": "crosses_above", "left": {"price": "close"}, "right": {"ind": "sma2"}}}}
    data["exit"] = {"stop_loss": {"type": "percent", "value": 50},
                    "scale_outs": scale_outs, "take_profit": take_profit}
    data["sizing"] = {"type": "fixed_shares", "value": 100,
                      "max_position_notional": 1_000_000}
    return StrategyDocument.model_validate(data)


def test_scale_out_books_a_partial_then_a_final_exit():
    doc = scale_backtest_doc(
        [{"target": {"type": "percent", "value": 10}, "percent": 50}],
        {"type": "percent", "value": 30})
    # flat, dip (so close crosses above sma2 on the way up), then a steady rally
    # through both the +10% scale level and the +30% final target
    closes = [100, 99, 100, 101, 105, 112, 120, 130, 140, 150]
    res = run_backtest(doc, CFG, {"SPY": frame(closes)})

    scale_trades = [t for t in res.trades if t["exit_reason"].startswith("scale_out")]
    final_trades = [t for t in res.trades if t["exit_reason"] == "target"
                    or t["exit_reason"] == "target_gap"]
    assert len(scale_trades) == 1, res.trades
    assert len(final_trades) == 1, res.trades

    partial, final = scale_trades[0], final_trades[0]
    assert partial["partial"] is True
    assert final["partial"] is False
    # 50% of 100 shares off at the scale level, the other 50 at the target
    assert partial["qty"] == 50
    assert final["qty"] == 50
    # the partial exited NEARER to entry than the final one
    assert partial["exit_px"] < final["exit_px"]
    assert partial["entry_px"] == final["entry_px"]  # one entry, two exits


def test_two_scale_levels_fire_in_order():
    doc = scale_backtest_doc(
        [{"target": {"type": "percent", "value": 5}, "percent": 40},
         {"target": {"type": "percent", "value": 10}, "percent": 30}],
        {"type": "percent", "value": 25})
    closes = [100, 99, 100, 101, 104, 108, 113, 120, 128, 135]
    res = run_backtest(doc, CFG, {"SPY": frame(closes)})

    scale_trades = [t for t in res.trades if t["exit_reason"].startswith("scale_out")]
    assert len(scale_trades) == 2, res.trades
    assert [t["qty"] for t in scale_trades] == [40, 30]
    # strictly increasing exit prices — the ladder is consumed nearest-first
    assert scale_trades[0]["exit_px"] < scale_trades[1]["exit_px"]


def test_scale_out_does_not_start_the_reentry_cooldown():
    """A partial leaves the position OPEN, so it must not be treated as a
    round-trip completion — the position keeps running to its final exit."""
    doc = scale_backtest_doc(
        [{"target": {"type": "percent", "value": 5}, "percent": 50}],
        {"type": "percent", "value": 50})
    closes = [100, 99, 100, 101, 106, 108, 110, 112, 114, 116]
    res = run_backtest(doc, CFG, {"SPY": frame(closes)})
    assert any(t["exit_reason"].startswith("scale_out") for t in res.trades)
    # the remainder never hit +50%, so it's still open at the end
    assert res.metrics["open_positions_at_end"] == 1
    assert res.open_positions[0]["qty"] == 50


def test_no_scale_outs_leaves_behavior_unchanged():
    doc = scale_backtest_doc([], {"type": "percent", "value": 10})
    closes = [100, 99, 100, 101, 105, 112, 120, 130]
    res = run_backtest(doc, CFG, {"SPY": frame(closes)})
    assert all(t["partial"] is False for t in res.trades)
    assert all(not t["exit_reason"].startswith("scale_out") for t in res.trades)


def _expr(data: dict):
    """Parse a raw condition dict through the real schema."""
    doc = StrategyDocument.model_validate({
        **copy.deepcopy(EXAMPLE),
        "indicators": [{"id": "rsi14", "type": "rsi", "params": {"period": 14}},
                       {"id": "vsma", "type": "volume_sma", "params": {"period": 3}},
                       {"id": "sma3", "type": "sma", "params": {"period": 3}},
                       {"id": "sma2", "type": "sma", "params": {"period": 2}}],
        "entry": {"long": {"when": data}},
    })
    return doc.entry.long.when
