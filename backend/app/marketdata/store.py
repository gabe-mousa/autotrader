"""Candle store: SQLite-backed. 1m and 1d are stored natively; so are the
intraday timeframes Schwab serves directly (5m/15m/30m), because Schwab's
lookback wall is per-frequency — 1m reaches back ~46 days but 5m reaches ~203
days and 15/30m ~260 days, so aggregating them from 1m would throw that depth
away. 1d reaches the symbol's full listed history (1985+).
Timeframes Schwab does not serve (1h) stay aggregated from 1m on read.

For a Schwab-native tf, read() is a hybrid: stored bars supply the depth, and
anything newer than the last stored bar is aggregated from 1m so live streaming
(which only writes 1m) still produces an up-to-the-minute tail.

Upserts idempotent; REST wins over stream for a given bar."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..db import Database

# timeframes with their own rows in the candles table
NATIVE_TFS = {"1m", "1d"}
# ...of those, the intraday ones Schwab's /pricehistory serves directly
# (frequencyType=minute supports frequency 1,5,10,15,30 — 1h has no equivalent)
SCHWAB_INTRADAY_TFS = {"5m", "15m", "30m"}
NATIVE_TFS |= SCHWAB_INTRADAY_TFS
STORED_TFS = NATIVE_TFS
AGG_RULES = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "60min"}
# Non-native tfs aggregate from 1m by default. 1h is the exception: 30m divides
# evenly into it and reaches ~260 days back vs 1m's ~46, so sourcing 1h from 1m
# would cap hourly backtests at the shallowest wall Schwab has. Bucket edges are
# identical either way (both land on the hour), so this only adds depth.
AGG_SOURCE = {"1h": "30m"}
TF_MS = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000}
# source precedence: higher wins on conflict
_SOURCE_RANK = {"stream": 1, "aggregated": 1, "schwab_rest": 2}


@dataclass(frozen=True)
class Candle:
    ts: int  # epoch ms UTC (bar open time)
    open: float
    high: float
    low: float
    close: float
    volume: int


def base_tf(tf: str) -> str:
    """The natively-stored timeframe a request maps to. Schwab-native intraday
    tfs map to themselves (they have their own rows); 1h maps to 30m, and
    anything else aggregated maps to 1m."""
    if tf in NATIVE_TFS:
        return tf
    if tf in AGG_RULES:
        return AGG_SOURCE.get(tf, "1m")
    raise ValueError(f"unsupported timeframe: {tf}")


def _aggregate(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = (
        df.resample(rule, origin="start_day", label="left", closed="left")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna(subset=["open"])
    )
    out["volume"] = out["volume"].astype(int)
    return out


class CandleStore:
    def __init__(self, db: Database):
        self._db = db

    async def upsert(self, symbol: str, tf: str, candles: list[Candle], source: str) -> int:
        if tf not in NATIVE_TFS:
            raise ValueError(f"only native tfs are stored, got {tf}")
        rank = _SOURCE_RANK[source]
        n = 0
        for c in candles:
            # keep the higher-precedence source; equal precedence overwrites (latest wins)
            await self._db.conn.execute(
                """INSERT INTO candles (symbol, tf, ts, open, high, low, close, volume, source)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol, tf, ts) DO UPDATE SET
                     open=excluded.open, high=excluded.high, low=excluded.low,
                     close=excluded.close, volume=excluded.volume, source=excluded.source
                   WHERE ? >= (CASE candles.source WHEN 'schwab_rest' THEN 2 ELSE 1 END)""",
                (symbol, tf, c.ts, c.open, c.high, c.low, c.close, c.volume, source, rank),
            )
            n += 1
        await self._db.conn.commit()
        return n

    async def _raw(self, symbol: str, tf: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """Stored rows for a native tf as an OHLCV frame (may be empty)."""
        rows = await self._db.fetch_all(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND tf=? AND ts>=? AND ts<=? ORDER BY ts",
            (symbol, tf, start_ms, end_ms),
        )
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        if df.empty:
            return df.set_index(pd.DatetimeIndex([], name="ts", tz="UTC"))
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("ts")

    async def read(self, symbol: str, tf: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """OHLCV DataFrame indexed by UTC timestamp.

        1m/1d read straight through. Schwab-native intraday tfs (5/15/30m) read
        their stored bars and splice on a 1m-aggregated tail for anything newer
        than the last stored bar, so live streaming stays current. 1h rolls up
        that same hybrid 30m frame — inheriting both its depth and its live
        tail; anything else aggregates from 1m."""
        if tf in SCHWAB_INTRADAY_TFS:
            return await self._read_hybrid(symbol, tf, start_ms, end_ms)

        btf = base_tf(tf)
        if btf in SCHWAB_INTRADAY_TFS:
            df = await self._read_hybrid(symbol, btf, start_ms, end_ms)
        else:
            df = await self._raw(symbol, btf, start_ms, end_ms)
        if df.empty or tf == btf:
            return df
        return _aggregate(df, AGG_RULES[tf])

    async def _read_hybrid(
        self, symbol: str, tf: str, start_ms: int, end_ms: int
    ) -> pd.DataFrame:
        native = await self._raw(symbol, tf, start_ms, end_ms)
        if native.empty:
            # nothing backfilled yet — behave exactly as before (aggregate 1m)
            one_min = await self._raw(symbol, "1m", start_ms, end_ms)
            if one_min.empty:
                return one_min
            return _aggregate(one_min, AGG_RULES[tf])

        # aggregate 1m only for the window past the last stored bar, so a
        # partially-formed live bar never overwrites a completed Schwab bar
        tail_start = int(native.index[-1].timestamp() * 1000) + TF_MS[tf]
        if tail_start > end_ms:
            return native
        tail_1m = await self._raw(symbol, "1m", tail_start, end_ms)
        if tail_1m.empty:
            return native
        tail = _aggregate(tail_1m, AGG_RULES[tf])
        tail = tail[tail.index > native.index[-1]]
        if tail.empty:
            return native
        return pd.concat([native, tail])

    async def coverage(self, symbol: str, tf: str) -> tuple[int, int, int] | None:
        """(first_ts, last_ts, count) for a natively-stored tf, or None."""
        row = await self._db.fetch_one(
            "SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM candles WHERE symbol=? AND tf=?",
            (symbol, tf),
        )
        if not row or row["n"] == 0:
            return None
        return (row["a"], row["b"], row["n"])

    async def symbols(self) -> list[str]:
        rows = await self._db.fetch_all("SELECT DISTINCT symbol FROM candles ORDER BY symbol")
        return [r["symbol"] for r in rows]
