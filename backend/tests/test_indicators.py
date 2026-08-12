"""Indicator library vs hand-computed reference values, and no-lookahead checks."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.indicators.registry import INDICATORS, compute_indicator


def make_df(closes, highs=None, lows=None, vols=None, freq="1D"):
    n = len(closes)
    idx = pd.date_range("2024-01-02 14:30", periods=n, freq=freq, tz="UTC")
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "open": closes,  # simplification for tests
        "high": np.asarray(highs, dtype=float) if highs is not None else closes + 1,
        "low": np.asarray(lows, dtype=float) if lows is not None else closes - 1,
        "close": closes,
        "volume": np.asarray(vols, dtype=float) if vols is not None else np.full(n, 100.0),
    }, index=idx)


def test_sma_reference():
    df = make_df([1, 2, 3, 4, 5])
    out = compute_indicator(df, "sma", {"period": 3})
    assert np.isnan(out["value"].iloc[1])
    assert out["value"].iloc[2] == pytest.approx(2.0)
    assert out["value"].iloc[4] == pytest.approx(4.0)


def test_ema_reference():
    # span=3 -> alpha=0.5; ema: 1, 1.5, 2.25, 3.125
    df = make_df([1, 2, 3, 4])
    out = compute_indicator(df, "ema", {"period": 3})
    assert out["value"].iloc[3] == pytest.approx(3.125)


def test_rsi_extremes_and_range():
    up = make_df(np.arange(1, 40, dtype=float))
    out = compute_indicator(up, "rsi", {"period": 14})
    assert out["value"].iloc[-1] == pytest.approx(100.0)
    down = make_df(np.arange(40, 1, -1, dtype=float))
    out2 = compute_indicator(down, "rsi", {"period": 14})
    assert out2["value"].iloc[-1] == pytest.approx(0.0, abs=1e-6)
    mixed = make_df([44, 44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                     45.84, 46.08, 45.89, 46.03, 45.61, 46.28])
    out3 = compute_indicator(mixed, "rsi", {"period": 14})
    assert 0 < out3["value"].iloc[-1] < 100


def test_macd_zero_on_constant_series():
    df = make_df([50.0] * 60)
    out = compute_indicator(df, "macd", {})
    assert out["macd"].iloc[-1] == pytest.approx(0.0)
    assert out["histogram"].iloc[-1] == pytest.approx(0.0)


def test_bollinger_reference():
    df = make_df([2, 4, 6, 8, 10])
    out = compute_indicator(df, "bollinger", {"period": 5, "stddev": 2.0})
    # mean 6, population std = sqrt(8) ≈ 2.8284
    assert out["middle"].iloc[4] == pytest.approx(6.0)
    assert out["upper"].iloc[4] == pytest.approx(6 + 2 * np.sqrt(8))
    assert out["percent_b"].iloc[4] == pytest.approx((10 - (6 - 2 * np.sqrt(8))) / (4 * np.sqrt(8)))


def test_atr_constant_range():
    # high-low = 2 every bar, no gaps -> ATR converges to 2
    closes = [10.0] * 30
    df = make_df(closes, highs=[11.0] * 30, lows=[9.0] * 30)
    out = compute_indicator(df, "atr", {"period": 14})
    assert out["value"].iloc[-1] == pytest.approx(2.0, rel=1e-3)


def test_stochastic_at_top_of_range():
    closes = list(np.linspace(10, 20, 30))
    df = make_df(closes, highs=[c + 0.5 for c in closes], lows=[c - 0.5 for c in closes])
    out = compute_indicator(df, "stochastic", {})
    assert out["k"].iloc[-1] > 85  # closing near the top of the rolling range


def test_roc_reference():
    df = make_df([100, 101, 102, 110])
    out = compute_indicator(df, "roc", {"period": 3})
    assert out["value"].iloc[3] == pytest.approx(10.0)


def test_donchian_reference():
    closes = [5, 6, 7, 8, 9]
    df = make_df(closes, highs=[6, 7, 8, 9, 10], lows=[4, 5, 6, 7, 8])
    out = compute_indicator(df, "donchian", {"period": 3})
    assert out["upper"].iloc[4] == 10 and out["lower"].iloc[4] == 6


def test_vwap_session_reset():
    # two ET sessions of 1-min bars; vwap must reset at the day boundary
    idx1 = pd.date_range("2024-01-02 14:30", periods=3, freq="1min", tz="UTC")
    idx2 = pd.date_range("2024-01-03 14:30", periods=3, freq="1min", tz="UTC")
    df = pd.DataFrame({
        "open": [10, 10, 10, 20, 20, 20], "high": [10, 10, 10, 20, 20, 20],
        "low": [10, 10, 10, 20, 20, 20], "close": [10, 10, 10, 20, 20, 20],
        "volume": [100] * 6,
    }, index=idx1.append(idx2))
    out = compute_indicator(df, "vwap", {})
    assert out["value"].iloc[2] == pytest.approx(10.0)
    assert out["value"].iloc[3] == pytest.approx(20.0)  # fresh session anchor


def test_obv_reference():
    df = make_df([10, 11, 10.5, 10.5, 12], vols=[100, 200, 300, 400, 500])
    out = compute_indicator(df, "obv", {})
    assert out["value"].iloc[-1] == pytest.approx(200 - 300 + 0 + 500)


def test_highest_lowest_volume_sma():
    df = make_df([1, 5, 3, 2, 4], vols=[10, 20, 30, 40, 50])
    assert compute_indicator(df, "highest", {"period": 3})["value"].iloc[4] == 4
    assert compute_indicator(df, "lowest", {"period": 3})["value"].iloc[4] == 2
    assert compute_indicator(df, "volume_sma", {"period": 5})["value"].iloc[4] == 30


def test_opening_range_anchors_to_session_open():
    # two ET sessions of 5-min bars starting 09:30 ET (14:30 UTC)
    idx1 = pd.date_range("2024-01-02 14:30", periods=6, freq="5min", tz="UTC")
    idx2 = pd.date_range("2024-01-03 14:30", periods=6, freq="5min", tz="UTC")
    highs = [10, 12, 11, 20, 30, 40] + [5, 5, 5, 9, 9, 9]
    lows = [8, 9, 7, 6, 5, 4] + [1, 2, 3, 0, 0, 0]
    df = pd.DataFrame({
        "open": highs, "high": highs, "low": lows, "close": highs,
        "volume": [100] * 12,
    }, index=idx1.append(idx2), dtype=float)
    out = compute_indicator(df, "opening_range", {"minutes": 15})

    # bars 0,1 are inside the range but it isn't sealed yet
    assert np.isnan(out["high"].iloc[0]) and np.isnan(out["high"].iloc[1])
    # bar 2 closes at 09:45 -> range complete: high=12, low=7
    assert out["high"].iloc[2] == pytest.approx(12.0)
    assert out["low"].iloc[2] == pytest.approx(7.0)
    assert out["mid"].iloc[2] == pytest.approx(9.5)
    # stays pinned for the rest of the day despite higher highs / lower lows
    assert out["high"].iloc[5] == pytest.approx(12.0)
    assert out["low"].iloc[5] == pytest.approx(7.0)
    # fresh anchor next session
    assert np.isnan(out["high"].iloc[6])
    assert out["high"].iloc[8] == pytest.approx(5.0)
    assert out["low"].iloc[11] == pytest.approx(1.0)


def test_opening_range_partial_bar_multiple():
    """A 20-minute range on 15-minute bars seals at the next bar close (09:45+)."""
    idx = pd.date_range("2024-01-02 14:30", periods=4, freq="15min", tz="UTC")
    df = pd.DataFrame({
        "open": [1, 2, 3, 4], "high": [10, 11, 12, 13], "low": [1, 2, 3, 4],
        "close": [1, 2, 3, 4], "volume": [100] * 4,
    }, index=idx, dtype=float)
    out = compute_indicator(df, "opening_range", {"minutes": 20})
    assert np.isnan(out["high"].iloc[0])
    assert out["high"].iloc[1] == pytest.approx(11.0)  # bars 09:30 + 09:45
    assert out["high"].iloc[3] == pytest.approx(11.0)


def test_no_lookahead_all_indicators():
    """Truncating the input must not change earlier values — proves value at t
    never uses data after t."""
    rng = np.random.default_rng(42)
    closes = 100 + np.cumsum(rng.normal(0, 1, 120))
    df = make_df(closes, freq="1min")
    for ind_type in INDICATORS:
        full = compute_indicator(df, ind_type, {})
        trunc = compute_indicator(df.iloc[:80], ind_type, {})
        pd.testing.assert_frame_equal(full.iloc[:80], trunc, check_freq=False)
