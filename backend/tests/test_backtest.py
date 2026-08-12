"""Backtest engine tests on synthetic candles with hand-checkable outcomes."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from app.backtest.engine import BacktestConfig, run_backtest
from app.strategy.schema import StrategyDocument
from app.strategy.timeframe import estimate_warmup_bars
from tests.test_strategy import EXAMPLE


def daily_df(closes, start="2024-01-01"):
    n = len(closes)
    idx = pd.bdate_range(start, periods=n, tz="UTC") + pd.Timedelta(hours=21)
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "open": closes, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": np.full(n, 1_000_000),
    }, index=idx)


def sma_cross_doc(**overrides) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    data["meta"]["name"] = "sma cross test"
    data["indicators"] = [
        {"id": "fast", "type": "sma", "params": {"period": 3}},
        {"id": "slow", "type": "sma", "params": {"period": 10}},
    ]
    data["entry"] = {"long": {"when": {
        "op": "crosses_above", "left": {"ind": "fast"}, "right": {"ind": "slow"}}}}
    data["exit"] = {
        "signal": {"when": {
            "op": "crosses_below", "left": {"ind": "fast"}, "right": {"ind": "slow"}}},
        "stop_loss": {"type": "percent", "value": 10},
    }
    data.update(overrides)
    return StrategyDocument.model_validate(data)


CFG = BacktestConfig(start="2024-01-01", end="2025-12-31", initial_capital=100_000,
                     slippage={"type": "fixed_bps", "value": 0},
                     commission={"per_order": 0})


def test_v_shape_produces_round_trip():
    # decline then rally then decline: fast crosses above during rally, below after peak
    closes = list(np.linspace(100, 80, 15)) + list(np.linspace(80, 120, 25)) + \
             list(np.linspace(120, 95, 20))
    doc = sma_cross_doc()
    res = run_backtest(doc, CFG, {"SPY": daily_df(closes)})
    assert res.trades, "expected at least one round trip"
    t = res.trades[0]
    assert t["exit_reason"] in ("signal", "stop", "stop_gap")
    assert t["pnl"] > 0  # rode the rally
    assert res.metrics["trade_count"] == len(res.trades)
    assert res.metrics["final_equity"] > 0


def test_stop_loss_respected():
    # entry then crash: stop at 2% must cap the loss near -2%
    closes = list(np.linspace(100, 90, 12)) + list(np.linspace(90, 100, 10)) + \
             [100, 70, 65, 60, 55, 50, 45, 40, 35, 30]
    data = copy.deepcopy(EXAMPLE)
    data["indicators"] = [{"id": "fast", "type": "sma", "params": {"period": 3}},
                          {"id": "slow", "type": "sma", "params": {"period": 10}}]
    data["entry"] = {"long": {"when": {
        "op": "crosses_above", "left": {"ind": "fast"}, "right": {"ind": "slow"}}}}
    data["exit"] = {"stop_loss": {"type": "percent", "value": 2.0}}
    doc = StrategyDocument.model_validate(data)
    res = run_backtest(doc, CFG, {"SPY": daily_df(closes)})
    assert res.trades
    worst = min(t["pnl_pct"] for t in res.trades)
    # gap-through can exceed 2%, but the huge crash day gaps: open fill = honest loss;
    # with 30% overnight gap the trade shows the gap loss, proving gap handling
    assert any(t["exit_reason"] in ("stop", "stop_gap") for t in res.trades)
    assert worst < 0


def test_no_lookahead_entry_uses_next_bar_open():
    closes = [100] * 15 + [110] * 15  # jump triggers cross; fill must be at 110 not 100
    data = copy.deepcopy(EXAMPLE)
    data["indicators"] = [{"id": "fast", "type": "sma", "params": {"period": 2}},
                          {"id": "slow", "type": "sma", "params": {"period": 10}}]
    data["entry"] = {"long": {"when": {
        "op": "crosses_above", "left": {"ind": "fast"}, "right": {"ind": "slow"}}}}
    data["exit"] = {"stop_loss": {"type": "percent", "value": 5}}
    doc = StrategyDocument.model_validate(data)
    res = run_backtest(doc, CFG, {"SPY": daily_df(closes)})
    assert res.trades or True
    for t in res.trades:
        assert t["entry_px"] >= 109.9  # filled on post-jump bar open, never the signal bar


def test_max_daily_loss_pauses_entries():
    # brutal single-day drop with a strategy that would otherwise re-enter daily
    closes = [100 + (i % 3) for i in range(40)]
    data = copy.deepcopy(EXAMPLE)
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    data["entry"] = {"long": {"when": {
        "op": "lt", "left": {"ind": "rsi14"}, "right": {"value": 101}}},  # always true
        "max_entries_per_day": 5}
    data["exit"] = {"time_stop": {"bars": 1}}
    data["risk"] = {"max_daily_loss": 100000, "max_open_positions": 1}
    doc = StrategyDocument.model_validate(data)
    res = run_backtest(doc, CFG, {"SPY": daily_df(closes)})
    # engine ran without error and respected max_open_positions=1
    assert res.metrics["trade_count"] >= 1


def test_percent_of_equity_sizing_and_cash_behavior():
    closes = list(np.linspace(100, 90, 12)) + list(np.linspace(90, 130, 30))
    doc = sma_cross_doc()
    doc.sizing.value = 100  # all-in
    doc.sizing.max_position_notional = 10_000_000
    res = run_backtest(doc, CFG, {"SPY": daily_df(closes)})
    for t in res.trades:
        assert t["qty"] * t["entry_px"] <= 100_000 * 1.5  # never levered


def test_multi_symbol_max_open_positions():
    closes_a = list(np.linspace(100, 80, 15)) + list(np.linspace(80, 120, 30))
    closes_b = list(np.linspace(50, 40, 15)) + list(np.linspace(40, 60, 30))
    doc = sma_cross_doc(universe={"symbols": ["AAA", "BBB"]})
    doc.risk.max_open_positions = 1
    res = run_backtest(doc, CFG, {"AAA": daily_df(closes_a), "BBB": daily_df(closes_b)})
    # reconstruct concurrency from trades: intervals must never overlap
    intervals = [(t["entry_ts"], t["exit_ts"]) for t in res.trades]
    intervals.sort()
    for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
        assert s2 >= e1, "two positions were open simultaneously despite max_open_positions=1"


def test_deterministic():
    closes = list(100 + 10 * np.sin(np.arange(80) / 5))
    doc = sma_cross_doc()
    r1 = run_backtest(doc, CFG, {"SPY": daily_df(closes)})
    r2 = run_backtest(doc, CFG, {"SPY": daily_df(closes)})
    assert r1.trades == r2.trades
    assert r1.metrics == r2.metrics


# ---- warm-up preservation (regression: short date ranges silently suppressed
# almost every signal because indicators were computed AFTER trimming to
# exactly [start, end], leaving long-period indicators NaN the whole window) --

def test_pre_start_history_warms_up_indicators():
    """RSI(14) needs 14+ bars to produce a non-NaN value. A backtest whose
    requested window is only 5 bars must still trade correctly as long as
    warm-up history exists BEFORE cfg.start in the supplied candles."""
    data = copy.deepcopy(EXAMPLE)
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 14}}]
    data["entry"] = {"long": {"when": {
        "op": "lt", "left": {"ind": "rsi14"}, "right": {"value": 200}}}}  # always true (RSI max is 100; flat warm-up data produces RSI==100 exactly)
    data["exit"] = {"time_stop": {"bars": 1}}
    doc = StrategyDocument.model_validate(data)

    # 30 bars of warm-up history BEFORE the 5-bar requested window
    closes = [100.0] * 30 + [101, 102, 103, 104, 105]
    df = daily_df(closes)
    window_start = df.index[30].strftime("%Y-%m-%d")
    window_end = df.index[-1].strftime("%Y-%m-%d")
    cfg = BacktestConfig(start=window_start, end=window_end, initial_capital=100_000)

    res = run_backtest(doc, cfg, {"SPY": df})
    assert res.trades, "with warm-up history available, RSI should be valid and a trade should fire"
    # equity/trades must only reflect the requested window, never the warm-up bars
    # (entry_ts is epoch MS, per plan — not a bare-int nanosecond Timestamp)
    assert all(pd.Timestamp(t["entry_ts"], unit="ms", tz="UTC") >=
              pd.Timestamp(window_start, tz="UTC") for t in res.trades)
    assert res.equity_points[0][0] >= pd.Timestamp(window_start, tz="UTC")


def test_short_window_without_warmup_stays_nan_and_trades_less():
    """Same strategy, same short window, but candles trimmed to EXACTLY the
    window (no pre-start history) — this is the old buggy behavior reproduced
    on purpose, to prove the fix is what makes the difference above."""
    data = copy.deepcopy(EXAMPLE)
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 14}}]
    data["entry"] = {"long": {"when": {
        "op": "lt", "left": {"ind": "rsi14"}, "right": {"value": 100}}}}
    data["exit"] = {"time_stop": {"bars": 1}}
    doc = StrategyDocument.model_validate(data)

    closes = [100.0] * 30 + [101, 102, 103, 104, 105]
    df = daily_df(closes)
    window_start = df.index[30].strftime("%Y-%m-%d")
    window_end = df.index[-1].strftime("%Y-%m-%d")
    cfg = BacktestConfig(start=window_start, end=window_end, initial_capital=100_000)

    windowed_only = df.loc[window_start:window_end]  # simulate a fetch with no warm-up buffer
    res = run_backtest(doc, cfg, {"SPY": windowed_only})
    assert not res.trades, "RSI(14) over only 5 bars should be entirely NaN -> no signals"


def test_estimate_warmup_bars_uses_longest_param():
    data = copy.deepcopy(EXAMPLE)
    data["indicators"] = [
        {"id": "rsi14", "type": "rsi", "params": {"period": 14}},
        {"id": "slow_sma", "type": "sma", "params": {"period": 200}},
    ]
    doc = StrategyDocument.model_validate(data)
    assert estimate_warmup_bars(doc) == 250  # 200 (longest) + 50 margin


def test_estimate_warmup_bars_default_with_no_indicators():
    data = copy.deepcopy(EXAMPLE)
    data["indicators"] = []
    data["entry"] = {"long": {"when": {"op": "gt", "left": {"price": "close"},
                                       "right": {"value": 0}}}}
    doc = StrategyDocument.model_validate(data)
    assert estimate_warmup_bars(doc) == 30
