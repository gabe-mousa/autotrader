"""Plan 15 Part C — strategy expressiveness gaps found by the July-2026 research
campaign (see research/README.md):

  C2  indicator-valued operand offsets  (`close < vwap - 1.5*ATR`)
  C3  prior_day / gap indicators
  C4  the highest/donchian current-bar trap that silently produced 0 trades
  C6  per-symbol and ATR-relative slippage
  C7  raised expression caps
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import SlippageCfg
from app.indicators.registry import compute_indicator
from app.strategy.evaluator import StrategyEvaluator
from app.strategy.lint import is_warning, lint
from app.strategy.schema import StrategyDocument


def intraday_df(closes, highs=None, lows=None, opens=None, days=1, freq="30min"):
    """Intraday frame starting at 09:30 ET (14:30 UTC)."""
    n = len(closes)
    idx = pd.date_range("2024-01-02 14:30", periods=n, freq=freq, tz="UTC")
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "open": np.asarray(opens, dtype=float) if opens is not None else closes,
        "high": np.asarray(highs, dtype=float) if highs is not None else closes + 1,
        "low": np.asarray(lows, dtype=float) if lows is not None else closes - 1,
        "close": closes,
        "volume": np.full(n, 100.0),
    }, index=idx)


# ---------------------------------------------------------------- C3 prior_day

def test_prior_day_holds_previous_session_levels_flat():
    # two ET days of 4 bars each; day 1 range 10..14, day 2 elsewhere
    idx = list(pd.date_range("2024-01-02 14:30", periods=4, freq="1h", tz="UTC")) + \
          list(pd.date_range("2024-01-03 14:30", periods=4, freq="1h", tz="UTC"))
    df = pd.DataFrame({
        "open": [10, 11, 12, 13, 20, 21, 22, 23],
        "high": [11, 12, 13, 14, 21, 22, 23, 24],
        "low": [9, 10, 11, 12, 19, 20, 21, 22],
        "close": [10.5, 11.5, 12.5, 13.5, 20.5, 21.5, 22.5, 23.5],
        "volume": [100] * 8,
    }, index=pd.DatetimeIndex(idx)).astype(float)
    out = compute_indicator(df, "prior_day", {})
    # day 1 has no prior session
    assert out["high"].iloc[:4].isna().all()
    # day 2 sees day 1's high(14) / low(9) / close(13.5), flat all day
    assert (out["high"].iloc[4:] == 14).all()
    assert (out["low"].iloc[4:] == 9).all()
    assert (out["close"].iloc[4:] == 13.5).all()


def test_gap_percent_uses_prior_close_and_todays_open():
    idx = list(pd.date_range("2024-01-02 14:30", periods=2, freq="1h", tz="UTC")) + \
          list(pd.date_range("2024-01-03 14:30", periods=2, freq="1h", tz="UTC"))
    df = pd.DataFrame({
        "open": [100, 101, 110, 111], "high": [102, 103, 112, 113],
        "low": [99, 100, 109, 110], "close": [101, 100, 111, 112],
        "volume": [100] * 4,
    }, index=pd.DatetimeIndex(idx)).astype(float)
    out = compute_indicator(df, "gap", {})
    assert out["value"].iloc[:2].isna().all()
    # day 2 open 110 vs day 1 close 100 -> +10%
    assert out["value"].iloc[2] == pytest.approx(10.0)
    assert out["value"].iloc[3] == pytest.approx(10.0)  # held flat


# ------------------------------------------------------- C4 the breakout trap

def test_highest_without_shift_includes_current_bar():
    """The bug: close can never exceed highest(n) because the bar is in the window."""
    df = intraday_df([1, 2, 3, 4, 5])
    same_bar = compute_indicator(df, "highest", {"period": 3}, "close")["value"]
    assert same_bar.iloc[4] == 5.0            # equals the current close
    assert not (df["close"] > same_bar).any()  # -> comparison is never true

    shifted = compute_indicator(df, "highest", {"period": 3, "shift": 1}, "close")["value"]
    assert shifted.iloc[4] == 4.0              # prior bar's level
    assert (df["close"] > shifted).iloc[4]     # breakout is now expressible


def test_donchian_shift_moves_channel_back():
    df = intraday_df([1, 2, 3, 4, 5], highs=[1, 2, 3, 4, 5], lows=[1, 2, 3, 4, 5])
    same = compute_indicator(df, "donchian", {"period": 2})["upper"]
    shifted = compute_indicator(df, "donchian", {"period": 2, "shift": 1})["upper"]
    assert same.iloc[4] == 5.0
    assert shifted.iloc[4] == 4.0


def _doc_with_breakout(shift: int) -> dict:
    return {
        "meta": {"name": "brk"}, "universe": {"symbols": ["SPY"]},
        "timeframe": {"bar": "5m"},
        "indicators": [{"id": "hi20", "type": "highest",
                        "params": {"period": 20, "shift": shift}, "source": "high"},
                       {"id": "atr14", "type": "atr", "params": {"period": 14}}],
        "entry": {"long": {"when": {"op": "gt", "left": {"price": "close"},
                                    "right": {"ind": "hi20"}}}},
        "exit": {"stop_loss": {"type": "atr_multiple", "value": 2, "atr_indicator": "atr14"}},
        "sizing": {"type": "percent_of_equity", "value": 10},
    }


def test_lint_warns_about_unreachable_breakout_and_is_quiet_when_shifted():
    problems = lint(StrategyDocument.model_validate(_doc_with_breakout(shift=0)))
    hits = [p for p in problems if "can never be true" in p]
    assert hits and all(is_warning(p) for p in hits)

    problems = lint(StrategyDocument.model_validate(_doc_with_breakout(shift=1)))
    assert not [p for p in problems if "can never be true" in p]


def test_lint_allows_the_meaningful_opposite_comparison():
    """`close < highest` is perfectly sensible and must not be flagged."""
    doc = _doc_with_breakout(shift=0)
    doc["entry"]["long"]["when"]["op"] = "lt"
    problems = lint(StrategyDocument.model_validate(doc))
    assert not [p for p in problems if "can never be true" in p]


# ------------------------------------------------ C2 indicator-valued offsets

def _eval_doc(indicators, expr) -> StrategyDocument:
    return StrategyDocument.model_validate({
        "meta": {"name": "x"}, "universe": {"symbols": ["SPY"]},
        "timeframe": {"bar": "30m"}, "indicators": indicators,
        "entry": {"long": {"when": expr}},
        "exit": {"stop_loss": {"type": "percent", "value": 2}},
        "sizing": {"type": "percent_of_equity", "value": 10},
    })


def test_offset_indicator_makes_a_volatility_relative_level():
    """close < sma - 1.0*atr — the level moves with volatility, which a constant
    `add` cannot express. This is what forced the research to proxy with
    Bollinger bands."""
    df = intraday_df([10, 10, 10, 10, 10, 4])
    doc = _eval_doc(
        [{"id": "sma3", "type": "sma", "params": {"period": 3}},
         {"id": "atr14", "type": "atr", "params": {"period": 14}}],
        {"op": "lt", "left": {"price": "close"},
         "right": {"ind": "sma3", "off_ind": "atr14", "off_mul": -1.0}},
    )
    ev = StrategyEvaluator(doc)
    ev.compile_symbol("SPY", df)
    sym = ev.sym("SPY")
    sma = sym._series_for("sma3", None)
    atr = sym._series_for("atr14", None)
    i = 5
    expected = sma.iloc[i] - atr.iloc[i]
    assert sym.operand_at(doc.entry.long.when.right, i) == pytest.approx(expected)
    # close (4) is far below sma-atr, so the rule fires on the last bar only
    assert ev.entry_signal("SPY", i)
    assert not ev.entry_signal("SPY", 3)


def test_offset_defaults_are_identity_so_old_documents_are_unchanged():
    df = intraday_df([1, 2, 3, 4])
    doc = _eval_doc([{"id": "sma2", "type": "sma", "params": {"period": 2}}],
                    {"op": "gt", "left": {"price": "close"}, "right": {"ind": "sma2"}})
    ev = StrategyEvaluator(doc)
    ev.compile_symbol("SPY", df)
    sym = ev.sym("SPY")
    assert sym.operand_at(doc.entry.long.when.right, 3) == pytest.approx(3.5)


def test_lint_rejects_unknown_offset_indicator_and_missing_field():
    doc = _eval_doc([{"id": "sma3", "type": "sma", "params": {"period": 3}},
                     {"id": "bb", "type": "bollinger", "params": {"period": 5, "stddev": 2}}],
                    {"op": "lt", "left": {"price": "close"},
                     "right": {"ind": "sma3", "off_ind": "nope"}})
    assert any("unknown offset indicator" in p for p in lint(doc))

    doc = _eval_doc([{"id": "sma3", "type": "sma", "params": {"period": 3}},
                     {"id": "bb", "type": "bollinger", "params": {"period": 5, "stddev": 2}}],
                    {"op": "lt", "left": {"price": "close"},
                     "right": {"ind": "sma3", "off_ind": "bb"}})
    assert any("specify off_field" in p for p in lint(doc))


# ----------------------------------------------------------- C6 slippage model

def test_per_symbol_slippage_overrides_the_global_number():
    cfg = SlippageCfg(type="fixed_bps", value=5.0, per_symbol_bps={"AAPL": 1.0, "LCID": 12.0})
    assert cfg.bps_for("AAPL") == 1.0
    assert cfg.bps_for("LCID") == 12.0
    assert cfg.bps_for("MSFT") == 5.0      # falls back to the global value


def test_atr_fraction_slippage_scales_with_range():
    cfg = SlippageCfg(type="atr_fraction", value=10.0)
    # ATR = 2% of price -> 10 * 0.02 * 100 = 20bps; a calmer name costs less
    assert cfg.bps_for("X", atr_pct=0.02) == pytest.approx(20.0)
    assert cfg.bps_for("Y", atr_pct=0.005) == pytest.approx(5.0)
    # per-symbol override still wins, and NaN ATR falls back to the flat value
    cfg2 = SlippageCfg(type="atr_fraction", value=10.0, per_symbol_bps={"Z": 3.0})
    assert cfg2.bps_for("Z", atr_pct=0.02) == 3.0
    assert cfg.bps_for("W", atr_pct=float("nan")) == 10.0


# -------------------------------------------------------------- C7 raised caps

def test_expression_caps_allow_more_conditions_than_before():
    conds = [{"op": "gt", "left": {"price": "close"}, "right": {"value": float(i)}}
             for i in range(15)]          # would have failed at the old max_length=10
    doc = _eval_doc([{"id": "sma3", "type": "sma", "params": {"period": 3}}],
                    {"all": conds})
    assert len(doc.entry.long.when.all) == 15
    assert not [p for p in lint(doc) if "more than" in p]
