"""Plan 15 §C1/C5 — cross-symbol regime filter and cross-sectional selection.

The regime filter is the gap the July-2026 research identified as costliest:
rules could only see the symbol being evaluated, so strategies bought dips into
a falling market with no awareness of the tape.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtest.engine import BacktestConfig, run_backtest
from app.strategy.schema import StrategyDocument


def frame(closes, start="2024-01-02 14:30", freq="30min"):
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    c = np.asarray(closes, float)
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c,
                         "volume": np.full(n, 1000.0)}, index=idx)


def doc_with(market_filter=None, selection=None, symbols=("A",)):
    d = {
        "meta": {"name": "t"}, "universe": {"symbols": list(symbols)},
        "timeframe": {"bar": "30m"}, "indicators": [],
        # always-true entry so the only thing gating trades is the filter
        "entry": {"long": {"when": {"op": "gt", "left": {"price": "close"},
                                    "right": {"value": 0}}},
                  "cooldown_bars": 0, "max_entries_per_day": 50},
        "exit": {"stop_loss": {"type": "percent", "value": 90}, "time_stop": {"bars": 1}},
        "sizing": {"type": "percent_of_equity", "value": 10, "max_position_notional": 1e9},
        "risk": {"max_open_positions": 5, "max_daily_loss": 1e9, "max_daily_orders": 200},
    }
    if market_filter is not None:
        d["market_filter"] = market_filter
    if selection is not None:
        d["selection"] = selection
    return StrategyDocument.model_validate(d)


def run(doc, candles):
    cfg = BacktestConfig(start="2024-01-02", end="2024-01-31", initial_capital=100_000,
                         slippage={"type": "fixed_bps", "value": 0.0})
    return run_backtest(doc, cfg, candles)


MF_ABOVE_SMA = {
    "symbol": "SPY",
    "indicators": [{"id": "sma3", "type": "sma", "params": {"period": 3}}],
    "when": {"op": "gt", "left": {"price": "close"}, "right": {"ind": "sma3"}},
}


def test_entries_are_blocked_while_the_reference_symbol_is_weak():
    """SPY falling -> close is below its own SMA -> no entries at all."""
    a = frame([10] * 8)
    spy_down = frame([100, 99, 98, 97, 96, 95, 94, 93])
    res = run(doc_with(market_filter=MF_ABOVE_SMA), {"A": a, "SPY": spy_down})
    assert res.trades == []


def test_entries_are_allowed_while_the_reference_symbol_is_strong():
    a = frame([10] * 8)
    spy_up = frame([90, 91, 92, 93, 94, 95, 96, 97])
    res = run(doc_with(market_filter=MF_ABOVE_SMA), {"A": a, "SPY": spy_up})
    assert res.trades, "rising reference symbol should permit entries"


def test_filter_fails_closed_when_reference_data_is_missing():
    """A configured gate with no data must block, not silently trade unfiltered."""
    a = frame([10] * 8)
    res = run(doc_with(market_filter=MF_ABOVE_SMA), {"A": a})   # no SPY at all
    assert res.trades == []


def test_applies_to_restricts_the_gate_to_one_side():
    """A long-only gate must not block when it is configured for shorts."""
    a = frame([10] * 8)
    spy_down = frame([100, 99, 98, 97, 96, 95, 94, 93])
    mf = dict(MF_ABOVE_SMA, applies_to="short")
    res = run(doc_with(market_filter=mf), {"A": a, "SPY": spy_down})
    assert res.trades, "a short-side gate must not block long entries"


def test_no_market_filter_leaves_behaviour_unchanged():
    a = frame([10] * 8)
    assert run(doc_with(), {"A": a}).trades


def test_reference_bar_is_never_ahead_of_the_traded_bar():
    """The gate reads the reference bar at or BEFORE the current timestamp."""
    a = frame([10] * 6)
    # SPY only starts trading later; before that there is no reference bar and
    # the gate must fail closed rather than reach forward for one
    spy = frame([90, 91, 92], start="2024-01-02 16:30")
    res = run(doc_with(market_filter=MF_ABOVE_SMA), {"A": a, "SPY": spy})
    for t in res.trades:
        assert t["entry_ts"] >= int(spy.index[0].timestamp() * 1000)


# ------------------------------------------------------- cross-sectional pick

SEL = {"metric": {"ind": "roc2"}, "top_n": 1, "order": "desc"}


def doc_with_selection(order="desc", top_n=1):
    d = doc_with(symbols=("A", "B", "C")).model_dump()
    d["indicators"] = [{"id": "roc2", "type": "roc", "params": {"period": 2},
                        "source": "close"}]
    d["selection"] = {"metric": {"ind": "roc2"}, "top_n": top_n, "order": order}
    d["risk"]["max_open_positions"] = 1
    d["entry"]["max_entries_per_day"] = 1
    return StrategyDocument.model_validate(d)


def test_selection_trades_only_the_strongest_symbol():
    candles = {
        "A": frame([10, 10, 10, 10, 10, 10]),      # flat
        "B": frame([10, 11, 13, 16, 20, 25]),      # strongest
        "C": frame([10, 10, 9, 9, 8, 8]),          # weakest
    }
    res = run(doc_with_selection(order="desc"), candles)
    assert res.trades, "expected at least one trade"
    assert {t["symbol"] for t in res.trades} == {"B"}


def test_selection_can_pick_the_weakest_for_short_style_screens():
    candles = {
        "A": frame([10, 10, 10, 10, 10, 10]),
        "B": frame([10, 11, 13, 16, 20, 25]),
        "C": frame([10, 10, 9, 9, 8, 8]),
    }
    res = run(doc_with_selection(order="asc"), candles)
    assert {t["symbol"] for t in res.trades} == {"C"}


def test_selection_is_deterministic_on_ties():
    """Identical metrics must resolve by symbol name, not dict order."""
    candles = {s: frame([10, 11, 12, 13, 14, 15]) for s in ("A", "B", "C")}
    first = run(doc_with_selection(), candles).trades
    second = run(doc_with_selection(), candles).trades
    assert [t["symbol"] for t in first] == [t["symbol"] for t in second]
    if first:
        assert first[0]["symbol"] == "A"      # lowest name wins a tie


def test_top_n_widens_the_eligible_set():
    candles = {
        "A": frame([10, 10, 10, 10, 10, 10]),
        "B": frame([10, 11, 13, 16, 20, 25]),
        "C": frame([10, 10, 9, 9, 8, 8]),
    }
    d = doc_with_selection(top_n=3).model_dump()
    d["risk"]["max_open_positions"] = 3
    res = run(StrategyDocument.model_validate(d), candles)
    assert len({t["symbol"] for t in res.trades}) > 1


# ---------------------------------------------------------------------------
# exit_when (round 6, research4/FINDINGS4-LEVERAGED-TREND.md)
#
# A regime gate that can only block entries is half a gate: it cannot de-risk a
# position it already holds. Round 6's strategy holds a 3x ETF while the
# UNDERLYING index is in an uptrend, so its EXIT has to read the underlying too.
# Keeping the exit a SEPARATE expression (rather than `not when`) is also what
# allows hysteresis. Absent `exit_when`, behaviour is unchanged.
# ---------------------------------------------------------------------------

def _doc_hold(exit_when=None):
    """Enter on bar 0 while SPY is strong, then hold — no stop, no time stop, no
    exit signal. The ONLY thing that can close the position is the gate."""
    mf = dict(MF_ABOVE_SMA)
    if exit_when is not None:
        mf["exit_when"] = exit_when
    d = {
        "meta": {"name": "t"}, "universe": {"symbols": ["A"]},
        "timeframe": {"bar": "30m"}, "indicators": [],
        "entry": {"long": {"when": {"op": "gt", "left": {"price": "close"},
                                    "right": {"value": 0}}},
                  "cooldown_bars": 0, "max_entries_per_day": 50},
        "exit": {"stop_loss": {"type": "percent", "value": 99}},
        "sizing": {"type": "percent_of_equity", "value": 10, "max_position_notional": 1e9},
        "risk": {"max_open_positions": 1, "max_daily_loss": 1e9, "max_daily_orders": 200},
        "session": {"hours": "regular", "flatten_eod": False},
        "market_filter": mf,
    }
    return StrategyDocument.model_validate(d)


# SPY rises for 6 bars (gate open, position entered) then falls hard for 6.
# "A" is flat throughout, so nothing else could trigger an exit.
_SPY_TURN = [90, 91, 92, 93, 94, 95, 88, 81, 74, 67, 60, 53]
# exit below the SMA, i.e. the mirror of the entry condition
_EXIT_BELOW_SMA = {"op": "lt", "left": {"price": "close"}, "right": {"ind": "sma3"}}


def test_gate_closes_the_open_position_when_exit_when_fires():
    res = run(_doc_hold(_EXIT_BELOW_SMA), {"A": frame([10] * 12), "SPY": frame(_SPY_TURN)})
    assert res.trades, "should have entered while the reference symbol was strong"
    assert any(t["exit_reason"] == "market_filter" for t in res.trades), (
        f"expected a market_filter exit, got {[t['exit_reason'] for t in res.trades]}")


def test_default_is_unchanged_gate_does_not_close_positions():
    """Regression guard: with no exit_when the position must survive the same
    reference-symbol collapse, exactly as before round 6."""
    res = run(_doc_hold(None), {"A": frame([10] * 12), "SPY": frame(_SPY_TURN)})
    assert not any(t["exit_reason"] == "market_filter" for t in res.trades)


# SPY pushes up, then drifts DOWN through its own SMA but stays above SMA*0.95.
_SPY_DRIFT = [80, 90, 100, 120, 121, 122, 118, 116, 114, 112, 110, 108]


def _mf_band(exit_mul: float):
    return {"symbol": "SPY",
            "indicators": [{"id": "sma3", "type": "sma", "params": {"period": 3}}],
            "when": {"op": "gt", "left": {"price": "close"}, "right": {"ind": "sma3"}},
            "exit_when": {"op": "lt", "left": {"price": "close"},
                          "right": {"ind": "sma3", "mul": exit_mul}}}


def _run_band(exit_mul: float):
    base = _doc_hold(None).model_dump(mode="json")
    doc = StrategyDocument.model_validate({**base, "market_filter": _mf_band(exit_mul)})
    return run(doc, {"A": frame([10] * 12), "SPY": frame(_SPY_DRIFT)})


def test_exit_when_supports_hysteresis_distinct_from_not_when():
    """Same entry threshold, two exit thresholds, one price path.

    With exit_when = `close < sma3` the gate is a single threshold and the drift
    closes the position. Widening the exit to `close < sma3*0.95` leaves it
    open — the band between the two conditions is exactly the hysteresis that a
    `not when` exit could never express."""
    tight = _run_band(1.00)
    wide = _run_band(0.95)
    assert any(t["exit_reason"] == "market_filter" for t in tight.trades), (
        "a single-threshold gate should close on the drift below the SMA")
    assert not any(t["exit_reason"] == "market_filter" for t in wide.trades), (
        "drifting inside the band must NOT trigger a gate exit")
