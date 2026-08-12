"""Plan 15 §C7 — entry.pyramid (scale-IN).

The mirror of scale-outs: add to a position that is working. The properties
that matter are that adds are SPACED (not clustered on one bar), that the
position merges at a volume-weighted average, and that the stop travels with
that new average rather than leaving a distant stop protecting a bigger size.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtest.engine import BacktestConfig, run_backtest
from app.strategy.schema import StrategyDocument


def bars(opens, highs, lows, closes):
    n = len(closes)
    idx = pd.date_range("2024-01-02 14:30", periods=n, freq="30min", tz="UTC")
    return pd.DataFrame({
        "open": np.asarray(opens, float), "high": np.asarray(highs, float),
        "low": np.asarray(lows, float), "close": np.asarray(closes, float),
        "volume": np.full(n, 1000.0)}, index=idx)


def doc(*, side="long", pyramid=None, stop=50.0, time_stop=None, pct=10.0, trigger=100.0):
    d = {
        "meta": {"name": "pyr"}, "universe": {"symbols": ["X"]},
        "timeframe": {"bar": "30m"}, "indicators": [],
        "entry": {"cooldown_bars": 0, "max_entries_per_day": 50},
        "exit": {"stop_loss": {"type": "percent", "value": stop}},
        "sizing": {"type": "percent_of_equity", "value": pct, "max_position_notional": 1e9},
        "risk": {"max_open_positions": 1, "max_daily_loss": 1e9, "max_daily_orders": 200},
    }
    cond = ({"op": "gt", "left": {"price": "close"}, "right": {"value": trigger}}
            if side == "long"
            else {"op": "lt", "left": {"price": "close"}, "right": {"value": trigger}})
    d["entry"]["long" if side == "long" else "short"] = {"when": cond}
    if side == "short":
        d["risk"]["short"] = {"enabled": True, "initial_margin_pct": 50.0,
                              "maintenance_margin_pct": 30.0, "max_short_notional": 1e9}
    if pyramid is not None:
        d["entry"]["pyramid"] = pyramid
    if time_stop is not None:
        d["exit"]["time_stop"] = {"bars": time_stop}
    return StrategyDocument.model_validate(d)


def run(document, df, capital=100_000.0):
    cfg = BacktestConfig(start="2024-01-02", end="2024-01-31", initial_capital=capital,
                         slippage={"type": "fixed_bps", "value": 0.0})
    return run_backtest(document, cfg, {"X": df})


PYR = {"max_adds": 2, "spacing": {"type": "percent", "value": 10.0}, "size_pct": 100.0}


def test_long_adds_as_price_moves_favourably_and_averages_up():
    # entry at 100 (bar1 open); adds at +10% (110) and then +10% off that (121)
    df = bars(opens=[101, 100, 112, 125, 125], highs=[101, 100, 112, 125, 125],
              lows=[101, 100, 108, 120, 125], closes=[101, 100, 112, 125, 125])
    res = run(doc(pyramid=PYR, time_stop=4), df)
    t = res.trades[0]
    # 3 fills (1 entry + 2 adds) merged into one position
    assert t["qty"] > 0
    # average entry is between the first fill and the last add
    assert 100.0 < t["entry_px"] < 121.0


def test_adds_are_capped_by_max_adds():
    df = bars(opens=[101, 100, 130, 200, 300], highs=[101, 100, 130, 200, 300],
              lows=[101, 100, 130, 200, 300], closes=[101, 100, 130, 200, 300])
    one = run(doc(pyramid={**PYR, "max_adds": 1}, time_stop=4), df).trades[0]
    two = run(doc(pyramid={**PYR, "max_adds": 2}, time_stop=4), df).trades[0]
    assert two["qty"] > one["qty"], "a higher max_adds must accumulate more shares"


def test_no_pyramid_block_means_size_never_grows():
    df = bars(opens=[101, 100, 130, 200, 300], highs=[101, 100, 130, 200, 300],
              lows=[101, 100, 130, 200, 300], closes=[101, 100, 130, 200, 300])
    plain = run(doc(time_stop=4), df).trades[0]
    pyr = run(doc(pyramid=PYR, time_stop=4), df).trades[0]
    assert pyr["qty"] > plain["qty"]


def test_size_pct_scales_each_add():
    df = bars(opens=[101, 100, 130, 200, 300], highs=[101, 100, 130, 200, 300],
              lows=[101, 100, 130, 200, 300], closes=[101, 100, 130, 200, 300])
    half = run(doc(pyramid={**PYR, "size_pct": 50.0}, time_stop=4), df).trades[0]
    full = run(doc(pyramid={**PYR, "size_pct": 100.0}, time_stop=4), df).trades[0]
    assert half["qty"] < full["qty"]


def test_adds_do_not_fire_when_price_moves_against_the_position():
    df = bars(opens=[101, 100, 95, 92, 90], highs=[101, 100, 96, 93, 91],
              lows=[101, 100, 94, 91, 89], closes=[101, 100, 95, 92, 90])
    plain = run(doc(time_stop=4), df).trades[0]
    pyr = run(doc(pyramid=PYR, time_stop=4), df).trades[0]
    assert pyr["qty"] == plain["qty"], "a losing position must never be added to"


def test_stop_travels_with_the_new_average_entry():
    """After adding, the stop is recomputed from the higher average — it must
    not stay at the original (now far away) level protecting a bigger size.

    Entry 100 with a 20% stop -> 80. Adding at 110 lifts the average to ~105,
    moving the stop to ~84. The final bar dips to 84, which trips the NEW stop
    but would have left the ORIGINAL one untouched — so the two configurations
    must disagree."""
    df = bars(opens=[101, 100, 110, 100, 100], highs=[101, 100, 112, 100, 100],
              lows=[101, 100, 108, 100, 84], closes=[101, 100, 110, 100, 90])
    pyr = {"max_adds": 1, "spacing": {"type": "percent", "value": 10.0}, "size_pct": 100.0}

    with_pyr = run(doc(pyramid=pyr, stop=20.0), df)
    assert with_pyr.trades, "the raised stop should have been hit"
    assert with_pyr.trades[0]["exit_reason"] in ("stop", "stop_gap")

    # same bars, no pyramid: the original 80 stop survives a dip to 84
    without = run(doc(stop=20.0), df)
    assert not [t for t in without.trades if t["exit_reason"].startswith("stop")], \
        "the un-averaged stop at 80 must NOT trigger on a low of 84"


def test_short_pyramids_downward():
    """A short adds as the price FALLS — the favourable direction."""
    df = bars(opens=[99, 100, 88, 78, 78], highs=[99, 100, 92, 82, 78],
              lows=[99, 100, 88, 78, 78], closes=[99, 100, 88, 78, 78])
    plain = run(doc(side="short", time_stop=4), df).trades[0]
    pyr = run(doc(side="short", pyramid=PYR, time_stop=4), df).trades[0]
    assert pyr["qty"] > plain["qty"]
    assert pyr["side"] == "short"
    # averaged entry sits between the first fill and the lower adds
    assert 78.0 < pyr["entry_px"] <= 100.0


def test_adds_stop_when_buying_power_is_exhausted():
    """An add that cannot be afforded is skipped, not filled on credit."""
    df = bars(opens=[101, 100, 130, 200, 300], highs=[101, 100, 130, 200, 300],
              lows=[101, 100, 130, 200, 300], closes=[101, 100, 130, 200, 300])
    res = run(doc(pyramid={"max_adds": 5, "spacing": {"type": "percent", "value": 1.0},
                           "size_pct": 100.0}, pct=100.0, time_stop=4), df,
              capital=1_000.0)
    if res.trades:
        t = res.trades[0]
        assert t["entry_px"] * t["qty"] <= 1_000.0 * 1.05   # never spent beyond cash
