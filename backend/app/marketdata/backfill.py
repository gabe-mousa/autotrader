"""Backfill from GET /pricehistory into the candle store.

Period/frequency limits, measured against the live API (2026-07-27) rather
than taken from the spec text:
  - daily bars: periodType=year with an explicit startDate/endDate returns the
    symbol's FULL listed history. The documented `period` enum caps at 20 years
    ({1,2,3,5,10,15,20} — anything else 400s), but supplying startDate/endDate
    alongside periodType=year bypasses that cap: SPY comes back to its 1993
    inception, AAPL/IBM/KO to 1985 (Schwab's own data floor). periodType is
    still required — explicit dates without it 400 "Invalid frequencyType
    DAILY for periodType DAY".
  - intraday bars: explicit startDate/endDate windows. Schwab enforces a hard
    ROLLING DATE WALL per frequency; it is not a result-size cap. Sliding
    endDate backwards does not move the returned start date, and windows that
    sit entirely beyond the wall come back empty. Measured walls:
        1m            ~46 days
        5m            ~203 days
        10/15/30m     ~260 days
    No parameter combination reaches past them — periodType=month/year/ytd with
    frequencyType=minute is rejected outright, periodType=day caps at period=10,
    and needExtendedHoursData / needPreviousClose do not shift the wall.
    Intraday history deeper than the wall can only come from our own recorder.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from ..logging import get_logger
from .store import SCHWAB_INTRADAY_TFS, Candle, CandleStore

log = get_logger("backfill")

MINUTE_CHUNK_DAYS = 10
# Measured walls: 5m ~203d, 15/30m ~260d. Ask past them — the surplus window
# just comes back empty, and asking short would silently lose real depth.
INTRADAY_LOOKBACK_DAYS = 300
INTRADAY_CHUNK_DAYS = 90
# tf -> the `frequency` value for frequencyType=minute
INTRADAY_FREQ = {"5m": 5, "15m": 15, "30m": 30}
_DAY_MS = 86_400_000
# Earliest date worth asking for. Schwab's daily history floor is 1985, so any
# start at/below it yields the symbol's full listed history. Must not be
# negative — a pre-epoch startDate 400s ("StartDate must be miliseconds from
# epoch") — so use the epoch itself rather than an arbitrary early year.
DAILY_HISTORY_START_MS = 0  # 1970-01-01Z


def _to_candles(payload: dict) -> list[Candle]:
    return [
        Candle(
            ts=int(c["datetime"]),
            open=float(c["open"]),
            high=float(c["high"]),
            low=float(c["low"]),
            close=float(c["close"]),
            volume=int(c.get("volume", 0)),
        )
        for c in payload.get("candles", [])
    ]


class Backfiller:
    def __init__(self, schwab, store: CandleStore):
        self._schwab = schwab
        self._store = store
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock(self, symbol: str, tf: str) -> asyncio.Lock:
        return self._locks.setdefault((symbol, tf), asyncio.Lock())

    async def backfill_daily_full(self, symbol: str) -> int:
        """Pull the symbol's entire listed daily history.

        periodType=year + explicit start/end beats `period=20`, which would
        truncate to 20 years (see module docstring)."""
        async with self._lock(symbol, "1d"):
            end = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
            payload = await self._schwab.get_price_history(
                symbol,
                period_type="year",
                frequency_type="daily",
                frequency=1,
                start_ms=DAILY_HISTORY_START_MS,
                end_ms=end,
            )
            candles = _to_candles(payload)
            n = await self._store.upsert(symbol, "1d", candles, "schwab_rest")
            log.info("daily_backfill", symbol=symbol, bars=n)
            return n

    async def backfill_minute_range(self, symbol: str, start_ms: int, end_ms: int) -> int:
        """Pull 1-min bars for [start,end], chunked to <=10-day windows."""
        async with self._lock(symbol, "1m"):
            total = 0
            cur = start_ms
            while cur < end_ms:  # strict: Schwab 400s on start == end
                chunk_end = min(cur + MINUTE_CHUNK_DAYS * _DAY_MS - 1, end_ms)
                payload = await self._schwab.get_price_history(
                    symbol,
                    frequency_type="minute",
                    frequency=1,
                    start_ms=cur,
                    end_ms=chunk_end,
                )
                candles = _to_candles(payload)
                if candles:
                    total += await self._store.upsert(symbol, "1m", candles, "schwab_rest")
                cur = chunk_end + 1
            log.info("minute_backfill", symbol=symbol, bars=total)
            return total

    async def backfill_minute_max(self, symbol: str) -> int:
        """Pull as much 1-min history as Schwab will serve (~6 months)."""
        end = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        start = end - 200 * _DAY_MS  # generous; beyond-lookback chunks return empty
        return await self.backfill_minute_range(symbol, start, end)

    async def backfill_intraday_range(
        self, symbol: str, tf: str, start_ms: int, end_ms: int
    ) -> int:
        """Pull native 5/15/30m bars for [start,end], chunked into 90-day windows."""
        if tf not in INTRADAY_FREQ:
            raise ValueError(f"not a Schwab-native intraday tf: {tf}")
        async with self._lock(symbol, tf):
            total = 0
            cur = start_ms
            while cur < end_ms:  # strict: Schwab 400s on start == end
                chunk_end = min(cur + INTRADAY_CHUNK_DAYS * _DAY_MS - 1, end_ms)
                payload = await self._schwab.get_price_history(
                    symbol,
                    frequency_type="minute",
                    frequency=INTRADAY_FREQ[tf],
                    start_ms=cur,
                    end_ms=chunk_end,
                )
                candles = _to_candles(payload)
                if candles:
                    total += await self._store.upsert(symbol, tf, candles, "schwab_rest")
                cur = chunk_end + 1
            log.info("intraday_backfill", symbol=symbol, tf=tf, bars=total)
            return total

    async def backfill_intraday_max(self, symbol: str, tf: str) -> int:
        """Pull as much native history for `tf` as Schwab will serve (~8.5 months)."""
        end = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        start = end - INTRADAY_LOOKBACK_DAYS * _DAY_MS
        return await self.backfill_intraday_range(symbol, tf, start, end)

    async def backfill_intraday_all(self, symbol: str) -> dict[str, int]:
        """Max native history for every Schwab-native intraday tf."""
        out: dict[str, int] = {}
        for tf in sorted(SCHWAB_INTRADAY_TFS, key=lambda t: INTRADAY_FREQ[t]):
            out[tf] = await self.backfill_intraday_max(symbol, tf)
        return out

    async def ensure_symbol(self, symbol: str) -> None:
        """First-use bootstrap: full daily + max minute + max native intraday."""
        if await self._store.coverage(symbol, "1d") is None:
            await self.backfill_daily_full(symbol)
        if await self._store.coverage(symbol, "1m") is None:
            await self.backfill_minute_max(symbol)
        for tf in SCHWAB_INTRADAY_TFS:
            if await self._store.coverage(symbol, tf) is None:
                await self.backfill_intraday_max(symbol, tf)

    async def nightly_topup(self, symbols: list[str]) -> None:
        """Re-request the last 2 trading days (corrections) + today for every
        tracked symbol; refresh recent daily bars."""
        end = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        start = end - 4 * _DAY_MS  # covers 2 trading days across weekends
        for symbol in symbols:
            try:
                await self.backfill_minute_range(symbol, start, end)
                for tf in SCHWAB_INTRADAY_TFS:
                    await self.backfill_intraday_range(symbol, tf, start, end)
                payload = await self._schwab.get_price_history(
                    symbol, period_type="month", period=1, frequency_type="daily", frequency=1
                )
                await self._store.upsert(symbol, "1d", _to_candles(payload), "schwab_rest")
            except Exception as e:  # noqa: BLE001 — one bad symbol never stops the job
                log.warning("nightly_topup_failed", symbol=symbol, error=str(e))
