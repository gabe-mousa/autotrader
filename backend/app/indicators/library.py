"""Vectorized indicator implementations. Contract: every function returns a
DataFrame aligned to df.index whose value at row t uses ONLY data up to and
including row t (no lookahead). NaN during warm-up."""

from __future__ import annotations

import numpy as np
import pandas as pd


def _out(index, **cols) -> pd.DataFrame:
    return pd.DataFrame(cols, index=index)


def sma(df, p, src):
    return _out(df.index, value=src.rolling(int(p["period"])).mean())


def ema(df, p, src):
    return _out(df.index, value=src.ewm(span=int(p["period"]), adjust=False).mean())


def rsi(df, p, src):
    period = int(p["period"])
    delta = src.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    val = 100 - 100 / (1 + rs)
    val = val.where(loss != 0, 100.0)          # all gains -> RSI 100
    val[delta.isna()] = np.nan                  # first bar undefined
    return _out(df.index, value=val)


def macd(df, p, src):
    fast = src.ewm(span=int(p["fast"]), adjust=False).mean()
    slow = src.ewm(span=int(p["slow"]), adjust=False).mean()
    line = fast - slow
    signal = line.ewm(span=int(p["signal"]), adjust=False).mean()
    return _out(df.index, macd=line, signal=signal, histogram=line - signal)


def bollinger(df, p, src):
    period, k = int(p["period"]), float(p["stddev"])
    mid = src.rolling(period).mean()
    sd = src.rolling(period).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower).replace(0, np.nan)
    return _out(df.index, upper=upper, middle=mid, lower=lower,
                percent_b=(src - lower) / width)


def _true_range(df) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"],
         (df["high"] - prev_close).abs(),
         (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df, p, src):
    period = int(p["period"])
    return _out(df.index, value=_true_range(df).ewm(alpha=1 / period, adjust=False).mean())


def stochastic(df, p, src):
    k_p, d_p, smooth = int(p["k"]), int(p["d"]), int(p["smooth"])
    ll = df["low"].rolling(k_p).min()
    hh = df["high"].rolling(k_p).max()
    raw_k = 100 * (df["close"] - ll) / (hh - ll).replace(0, np.nan)
    k = raw_k.rolling(smooth).mean()
    d = k.rolling(d_p).mean()
    return _out(df.index, k=k, d=d)


def roc(df, p, src):
    return _out(df.index, value=src.pct_change(int(p["period"])) * 100)


def donchian(df, p, src):
    """Rolling channel. `shift` (default 0) moves the window back N bars, which
    is what a breakout rule almost always wants: with shift=0 the channel
    INCLUDES the current bar, so `close > donchian.upper` can never be true and
    the rule silently never fires. shift=1 gives the level as of the prior bar."""
    period, sh = int(p["period"]), int(p.get("shift", 0))
    high, low = df["high"], df["low"]
    if sh:
        high, low = high.shift(sh), low.shift(sh)
    upper = high.rolling(period).max()
    lower = low.rolling(period).min()
    return _out(df.index, upper=upper, lower=lower, middle=(upper + lower) / 2)


def prior_day(df, p, src):
    """Previous ET trading day's high/low/close, held flat for the whole of the
    current day. The classic intraday support/resistance levels.

    Distinct from `highest`/`lowest`, which roll continuously — these stay
    pinned to the prior session, so they describe the same level all day.
    NaN on the first day of data (no prior session)."""
    dates = df.index.tz_convert("America/New_York").date
    day_hi = df["high"].groupby(dates).max()
    day_lo = df["low"].groupby(dates).min()
    day_cl = df["close"].groupby(dates).last()
    ordered = list(day_hi.index)
    prev = {d: ordered[i - 1] for i, d in enumerate(ordered) if i > 0}
    idx = pd.Series(dates, index=df.index)

    def _map(agg):
        return idx.map(lambda d: agg.get(prev[d], np.nan) if d in prev else np.nan)

    return _out(df.index, high=_map(day_hi), low=_map(day_lo), close=_map(day_cl))


def gap(df, p, src):
    """Overnight gap: (today's first open − prior day's close) / prior close,
    in percent, held flat for the whole day. NaN on the first day."""
    dates = df.index.tz_convert("America/New_York").date
    day_open = df["open"].groupby(dates).first()
    day_close = df["close"].groupby(dates).last()
    ordered = list(day_open.index)
    prev = {d: ordered[i - 1] for i, d in enumerate(ordered) if i > 0}
    idx = pd.Series(dates, index=df.index)

    def _pct(d):
        if d not in prev:
            return np.nan
        pc = day_close.get(prev[d], np.nan)
        if pc is None or pc != pc or pc == 0:
            return np.nan
        return (day_open.get(d, np.nan) - pc) / pc * 100

    return _out(df.index, value=idx.map(_pct))


def vwap(df, p, src):
    """Session-anchored VWAP: resets each ET trading day."""
    et_dates = df.index.tz_convert("America/New_York").date
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    cum_pv = pv.groupby(et_dates).cumsum()
    cum_vol = df["volume"].groupby(et_dates).cumsum().replace(0, np.nan)
    return _out(df.index, value=cum_pv / cum_vol)


def opening_range(df, p, src):
    """Session-anchored opening range: the high/low of the first `minutes` of
    each ET trading day, held flat for the rest of that day.

    Unlike `highest`/`lowest` (rolling windows that keep sliding forward), this
    stays pinned to the session open — the classic ORB level. Values are NaN
    until the range is complete (the close of the last bar inside it), so a
    breakout rule can never see a level built from bars it hasn't reached.
    """
    minutes = int(p["minutes"])
    local = df.index.tz_convert("America/New_York")
    dates = local.date
    ts = pd.Series(local, index=df.index)
    day_open = ts.groupby(dates).transform("min")
    offset = (ts - day_open).dt.total_seconds() / 60.0

    bar_min = _bar_minutes(df)
    in_range = offset < minutes
    complete = offset + bar_min >= minutes     # range is sealed at this bar's close

    hi = df["high"].where(in_range).groupby(dates).cummax().groupby(dates).ffill()
    lo = df["low"].where(in_range).groupby(dates).cummin().groupby(dates).ffill()
    hi, lo = hi.where(complete), lo.where(complete)
    return _out(df.index, high=hi, low=lo, mid=(hi + lo) / 2)


def _bar_minutes(df) -> float:
    """Bar duration in minutes, inferred from the index (median spacing within
    a day, so the overnight gap doesn't skew it). Falls back to 1."""
    if len(df.index) < 2:
        return 1.0
    deltas = pd.Series(df.index).diff().dt.total_seconds() / 60.0
    intraday = deltas[(deltas > 0) & (deltas <= 60 * 8)]
    return float(intraday.median()) if len(intraday) else 1.0


def obv(df, p, src):
    sign = np.sign(df["close"].diff()).fillna(0)
    return _out(df.index, value=(sign * df["volume"]).cumsum())


def highest(df, p, src):
    """Rolling maximum. See `donchian` for why `shift` matters to breakouts."""
    sh = int(p.get("shift", 0))
    s = src.shift(sh) if sh else src
    return _out(df.index, value=s.rolling(int(p["period"])).max())


def lowest(df, p, src):
    sh = int(p.get("shift", 0))
    s = src.shift(sh) if sh else src
    return _out(df.index, value=s.rolling(int(p["period"])).min())


def volume_sma(df, p, src):
    return _out(df.index, value=df["volume"].astype(float).rolling(int(p["period"])).mean())


# --- round-4 additions (research3/FINDINGS2.md) ----------------------------
# Each of these encodes a conditioning variable that a 42-year, 650k-row event
# study found to carry a real and era-stable forward edge, and that the v1
# vocabulary could not express.


def atr_percentile(df, p, src):
    """Where does CURRENT volatility sit within this symbol's own recent
    history? Returns the rolling percentile rank (0..1) of ATR-as-%-of-price
    over `lookback` bars.

    Why it needs to exist: the strongest single conditioner found in the round-4
    study is the volatility REGIME, and it must be measured relative to the
    symbol itself. A 2% ATR is quiet for a leveraged ETF and extreme for a
    staple, so an absolute `atr > close * k` threshold cannot express it and
    behaves completely differently across a basket.
    """
    period, lookback = int(p["period"]), int(p["lookback"])
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    atrpct = atr / c.replace(0, np.nan) * 100
    return _out(df.index, value=atrpct.rolling(lookback).rank(pct=True))


def down_streak(df, p, src):
    """Number of CONSECUTIVE lower closes ending at this bar (0 if this bar
    closed up).

    Why it needs to exist: `falling(close, bars=4)` compares close[t] to
    close[t-4], which is a NET 4-bar decline — a completely different (and much
    weaker) condition than four successive down days. The study found the
    consecutive form carries the edge and the net form largely does not.
    """
    c = df["close"]
    down = (c < c.shift(1))
    grp = (down != down.shift()).cumsum()
    streak = down.groupby(grp).cumcount() + 1
    return _out(df.index, value=streak.where(down, 0).astype(float))


def up_streak(df, p, src):
    """Consecutive HIGHER closes ending at this bar (mirror of down_streak)."""
    c = df["close"]
    up = (c > c.shift(1))
    grp = (up != up.shift()).cumsum()
    streak = up.groupby(grp).cumcount() + 1
    return _out(df.index, value=streak.where(up, 0).astype(float))


def close_location(df, p, src):
    """Where in the bar's own range did it close? 0 = on the low, 1 = on the
    high. Distinguishes a bar that sold off all day from one that reversed,
    which no combination of v1 operands could express (the grammar cannot
    build `(close-low)/(high-low)` from price operands)."""
    h, l, c = df["high"], df["low"], df["close"]
    rng = (h - l).replace(0, np.nan)
    return _out(df.index, value=((c - l) / rng))
