"""Backtest metrics (docs/plan/05). Every metric here gets an in-app doc entry."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def compute_metrics(equity: pd.Series, trades: list[dict], bars_in_market: int,
                    total_bars: int, bar_seconds: float) -> dict:
    """equity: equity value indexed by bar timestamp (UTC)."""
    out: dict = {}
    if len(equity) < 2:
        return {"error": "not enough data"}

    initial, final = float(equity.iloc[0]), float(equity.iloc[-1])
    out["initial_capital"] = initial
    out["final_equity"] = round(final, 2)
    out["total_return_pct"] = round((final / initial - 1) * 100, 4)

    years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 86400), 1e-9)
    # Annualizing a decent gain over a very short window explodes: a 3x over two
    # hours raises to the power of ~4400 and overflows a float outright. The
    # number is meaningless at that point anyway, so clamp rather than crash —
    # a backtest must never fail because its result was too good.
    try:
        cagr = ((final / initial) ** (1 / years) - 1) * 100 if final > 0 else -100.0
    except (OverflowError, ZeroDivisionError):
        cagr = float("inf")
    if cagr != cagr or cagr in (float("inf"), float("-inf")):
        cagr = None      # undefined/absurd — surfaced as null rather than a fake number
    out["cagr_pct"] = round(cagr, 4) if cagr is not None else None

    # per-bar returns annualized by bar duration
    rets = equity.pct_change().dropna()
    periods_per_year = (365.25 * 86400) / bar_seconds if bar_seconds else TRADING_DAYS
    # cap: daily bars -> 252 trading periods
    if bar_seconds >= 86_400:
        periods_per_year = TRADING_DAYS
    if len(rets) > 1 and rets.std() > 0:
        vol = rets.std() * math.sqrt(periods_per_year)
        out["volatility_pct"] = round(vol * 100, 4)
        out["sharpe"] = round((rets.mean() * periods_per_year) / vol, 4)
        downside = rets[rets < 0]
        if len(downside) > 0 and downside.std() > 0:
            out["sortino"] = round(
                (rets.mean() * periods_per_year) / (downside.std() * math.sqrt(periods_per_year)), 4)
        else:
            out["sortino"] = None
    else:
        out["volatility_pct"] = 0.0
        out["sharpe"] = None
        out["sortino"] = None

    # drawdown
    peak = equity.cummax()
    dd = equity / peak - 1
    out["max_drawdown_pct"] = round(float(dd.min()) * 100, 4)
    if dd.min() < 0:
        trough_ts = dd.idxmin()
        peak_ts = equity.loc[:trough_ts].idxmax()
        rec = dd.loc[trough_ts:]
        recovered = rec[rec >= -1e-12]
        out["max_drawdown_start"] = str(peak_ts)
        out["max_drawdown_trough"] = str(trough_ts)
        out["max_drawdown_recovered"] = str(recovered.index[0]) if len(recovered) else None
    cagr = out["cagr_pct"]
    out["calmar"] = round(cagr / abs(out["max_drawdown_pct"]), 4) if out["max_drawdown_pct"] else None

    # trade stats
    out["trade_count"] = len(trades)
    if trades:
        pnls = np.array([t["pnl"] for t in trades])
        wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
        out["win_rate_pct"] = round(len(wins) / len(pnls) * 100, 2)
        out["avg_win"] = round(float(wins.mean()), 2) if len(wins) else None
        out["avg_loss"] = round(float(losses.mean()), 2) if len(losses) else None
        gross_win, gross_loss = float(wins.sum()), abs(float(losses.sum()))
        out["profit_factor"] = round(gross_win / gross_loss, 4) if gross_loss > 0 else None
        out["expectancy"] = round(float(pnls.mean()), 2)
        streak = worst = 0
        for p in pnls:
            streak = streak + 1 if p <= 0 else 0
            worst = max(worst, streak)
        out["longest_losing_streak"] = worst
        out["avg_bars_held"] = round(float(np.mean([t["bars_held"] for t in trades])), 1)
    out["exposure_pct"] = round(bars_in_market / total_bars * 100, 2) if total_bars else 0.0

    # monthly / yearly return tables
    out["monthly_returns"] = _period_returns(equity, "ME")
    out["yearly_returns"] = _period_returns(equity, "YE")
    return out


def _period_returns(equity: pd.Series, rule: str) -> dict[str, float]:
    """Percent return of each calendar period, keyed by the period's end date.

    The first period is measured from the opening equity rather than dropped, so
    no part of the window goes unreported and the yearly table compounds to the
    same total as the monthly one.
    """
    closes = equity.resample(rule).last().dropna()
    if closes.empty:
        return {}
    rets = closes.div(closes.shift(1).fillna(float(equity.iloc[0]))).sub(1)
    return {str(k.date()): round(float(v) * 100, 2) for k, v in rets.items()}
