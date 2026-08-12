"""Indicator registry: the single source of truth for the v1 primitive catalog.

The metadata here drives (a) strategy-schema validation, (b) the UI levers
panel, (c) the AI prompt catalog, and (d) in-app docs. Adding an indicator =
one function + one spec entry; nothing else changes."""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import library as lib

SOURCES = ("open", "high", "low", "close", "hl2", "hlc3", "ohlc4", "volume")


@dataclass(frozen=True)
class ParamSpec:
    name: str
    default: float
    min: float
    max: float
    step: float = 1
    doc: str = ""


@dataclass(frozen=True)
class IndicatorSpec:
    type: str
    doc: str
    params: tuple[ParamSpec, ...] = ()
    outputs: tuple[str, ...] = ("value",)
    uses_source: bool = True        # accepts a `source` price series
    intraday_only: bool = False     # e.g. session-anchored vwap
    fn: object = None               # (df, params, source) -> DataFrame[outputs]


def _p(name: str, default: float, lo: float, hi: float, step: float = 1, doc: str = "") -> ParamSpec:
    return ParamSpec(name, default, lo, hi, step, doc)


INDICATORS: dict[str, IndicatorSpec] = {
    s.type: s
    for s in [
        IndicatorSpec("sma", "Simple moving average of the source series.",
                      (_p("period", 20, 2, 500),), fn=lib.sma),
        IndicatorSpec("ema", "Exponential moving average (span = period).",
                      (_p("period", 20, 2, 500),), fn=lib.ema),
        IndicatorSpec("rsi", "Relative Strength Index (Wilder). 0-100; <30 oversold, >70 overbought.",
                      (_p("period", 14, 2, 200),), fn=lib.rsi),
        IndicatorSpec("macd", "MACD: EMA(fast)-EMA(slow), signal EMA, histogram.",
                      (_p("fast", 12, 2, 100), _p("slow", 26, 3, 200), _p("signal", 9, 2, 100)),
                      outputs=("macd", "signal", "histogram"), fn=lib.macd),
        IndicatorSpec("bollinger", "Bollinger bands: SMA ± stddev*k; percent_b = position within bands.",
                      (_p("period", 20, 2, 200), _p("stddev", 2.0, 0.5, 5.0, 0.1)),
                      outputs=("upper", "middle", "lower", "percent_b"), fn=lib.bollinger),
        IndicatorSpec("atr", "Average True Range (Wilder) — volatility in price units.",
                      (_p("period", 14, 2, 200),), uses_source=False, fn=lib.atr),
        IndicatorSpec("stochastic", "Stochastic oscillator %K/%D (0-100).",
                      (_p("k", 14, 2, 200), _p("d", 3, 1, 50), _p("smooth", 3, 1, 50)),
                      outputs=("k", "d"), uses_source=False, fn=lib.stochastic),
        IndicatorSpec("roc", "Rate of change: percent change vs `period` bars ago.",
                      (_p("period", 10, 1, 500),), fn=lib.roc),
        IndicatorSpec("donchian",
                      "Donchian channel: rolling extreme of highs/lows. Set shift=1 for "
                      "breakout rules — with shift=0 the channel includes the CURRENT bar, so "
                      "`close > donchian.upper` can never be true and the rule never fires.",
                      (_p("period", 20, 2, 500),
                       _p("shift", 0, 0, 100, 1,
                          "bars to shift the window back; use 1 for breakout levels")),
                      outputs=("upper", "lower", "middle"), uses_source=False, fn=lib.donchian),
        IndicatorSpec("prior_day",
                      "Previous ET trading day's high/low/close, held flat all day — the "
                      "classic intraday support/resistance levels. Unlike highest/lowest these "
                      "stay pinned to the prior session instead of rolling. NaN on day 1.",
                      (), outputs=("high", "low", "close"), uses_source=False,
                      intraday_only=True, fn=lib.prior_day),
        IndicatorSpec("gap",
                      "Overnight gap in percent: (today's open - prior close) / prior close, "
                      "held flat for the day. Positive = gap up. NaN on day 1.",
                      (), uses_source=False, intraday_only=True, fn=lib.gap),
        IndicatorSpec("vwap", "Session-anchored volume-weighted average price (intraday bars only).",
                      (), uses_source=False, intraday_only=True, fn=lib.vwap),
        IndicatorSpec("opening_range",
                      "Session-anchored opening range: high/low/mid of the first `minutes` of "
                      "each ET session, held flat for the rest of the day (intraday bars only). "
                      "Use this — not rolling highest/lowest — for opening-range breakouts; the "
                      "level stays pinned to the open instead of rolling forward. NaN until the "
                      "range completes.",
                      (_p("minutes", 15, 1, 390, 1,
                          "length of the opening range in minutes from the session open"),),
                      outputs=("high", "low", "mid"), uses_source=False,
                      intraday_only=True, fn=lib.opening_range),
        IndicatorSpec("obv", "On-balance volume: cumulative signed volume.",
                      (), uses_source=False, fn=lib.obv),
        IndicatorSpec("highest",
                      "Rolling maximum of the source over `period` bars (breakout levels). "
                      "Set shift=1 for breakout rules — with shift=0 the window includes the "
                      "CURRENT bar, so `close > highest` can never be true.",
                      (_p("period", 20, 1, 500),
                       _p("shift", 0, 0, 100, 1,
                          "bars to shift the window back; use 1 for breakout levels")),
                      fn=lib.highest),
        IndicatorSpec("lowest",
                      "Rolling minimum of the source over `period` bars. See `highest` for the "
                      "shift parameter.",
                      (_p("period", 20, 1, 500),
                       _p("shift", 0, 0, 100, 1,
                          "bars to shift the window back; use 1 for breakdown levels")),
                      fn=lib.lowest),
        IndicatorSpec("volume_sma", "SMA of volume (volume filters).",
                      (_p("period", 20, 2, 500),), uses_source=False, fn=lib.volume_sma),
        IndicatorSpec("atr_percentile",
                      "Volatility REGIME: percentile rank (0-1) of ATR-as-%-of-price within "
                      "this symbol's own last `lookback` bars. 0.8 means the symbol is more "
                      "volatile right now than it was on 80% of those bars; 0.2 means calmer "
                      "than 80% of them. Prefer this over `atr > close*k`: an absolute ATR "
                      "threshold means something different for every symbol (2% ATR is quiet "
                      "for a leveraged ETF and extreme for a staple), so it cannot express one "
                      "regime across a multi-symbol universe.",
                      (_p("period", 14, 2, 200),
                       _p("lookback", 252, 20, 1000, 1,
                          "bars of history the percentile is measured against")),
                      uses_source=False, fn=lib.atr_percentile),
        IndicatorSpec("down_streak",
                      "Number of CONSECUTIVE lower closes ending at this bar (0 if this bar "
                      "closed up). Not the same as `falling(close, bars=n)`, which only "
                      "compares close[t] to close[t-n] — a net decline, not a run.",
                      (), uses_source=False, fn=lib.down_streak),
        IndicatorSpec("up_streak",
                      "Number of CONSECUTIVE higher closes ending at this bar (0 if this bar "
                      "closed down). Mirror of down_streak.",
                      (), uses_source=False, fn=lib.up_streak),
        IndicatorSpec("close_location",
                      "Where the bar closed within its own high-low range: 0 = on the low, "
                      "1 = on the high. Separates a bar that sold off into the close from one "
                      "that reversed off the low.",
                      (), uses_source=False, fn=lib.close_location),
    ]
}


def source_series(df: pd.DataFrame, source: str) -> pd.Series:
    if source in ("open", "high", "low", "close", "volume"):
        return df[source].astype(float)
    if source == "hl2":
        return (df["high"] + df["low"]) / 2
    if source == "hlc3":
        return (df["high"] + df["low"] + df["close"]) / 3
    if source == "ohlc4":
        return (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    raise ValueError(f"unknown source {source}")


def compute_indicator(df: pd.DataFrame, ind_type: str, params: dict, source: str = "close") -> pd.DataFrame:
    """Compute one indicator over OHLCV df -> DataFrame with the spec's output
    columns, aligned to df.index. Values at bar t use data through bar t only."""
    spec = INDICATORS[ind_type]
    merged = {p.name: params.get(p.name, p.default) for p in spec.params}
    src = source_series(df, source) if spec.uses_source else None
    out = spec.fn(df, merged, src)  # type: ignore[operator]
    assert list(out.columns) == list(spec.outputs), f"{ind_type} outputs mismatch"
    return out


def catalog() -> list[dict]:
    """JSON-friendly catalog for UI / AI prompt / docs."""
    return [
        {
            "type": s.type,
            "doc": s.doc,
            "outputs": list(s.outputs),
            "uses_source": s.uses_source,
            "intraday_only": s.intraday_only,
            "params": [
                {"name": p.name, "default": p.default, "min": p.min, "max": p.max,
                 "step": p.step, "doc": p.doc}
                for p in s.params
            ],
        }
        for s in INDICATORS.values()
    ]
