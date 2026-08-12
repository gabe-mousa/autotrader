"""Bar-duration constants and warm-up estimation — shared by the backtest
engine, the live runtime, and the backtest API (previously duplicated as
BAR_SECONDS in backtest/engine.py and BAR_MS in runtime/runner.py)."""

from __future__ import annotations

from .schema import StrategyDocument

BAR_MS: dict[str, int] = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "1d": 86_400_000,
}
BAR_SECONDS: dict[str, int] = {tf: ms // 1000 for tf, ms in BAR_MS.items()}


def estimate_warmup_bars(doc: StrategyDocument) -> int:
    """How many EXTRA bars of history to fetch before a requested start date
    so every indicator is warmed up (not NaN) by the first evaluated bar.
    Heuristic: the largest configured param value across all indicators —
    period-like values (2-500) dominate every other param an indicator takes
    (e.g. bollinger's stddev multiplier is 0.5-5, always smaller than its own
    period), so a plain max is a robust, simple proxy without needing a
    per-indicator-type lookup table. +50 bar margin for EMA/RSI's asymptotic
    (not hard-cutoff) convergence; minimum 30 bars even with no indicators."""
    longest = 0.0
    # the market-filter reference symbol has its OWN indicator list (e.g. an
    # SMA200 on SPY) and needs the same warm-up, otherwise the gate sits NaN
    # (and therefore closed) for the first stretch of the window
    inds = list(doc.indicators)
    if getattr(doc, "market_filter", None) is not None:
        inds += list(doc.market_filter.indicators)
    for ind in inds:
        if ind.params:
            longest = max(longest, max(ind.params.values()))
    return int(longest) + 50 if longest else 30
