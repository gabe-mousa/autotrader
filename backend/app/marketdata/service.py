"""MarketDataService — the one interface every consumer reads through.
Backtests call get_candles only; live consumers add subscriptions (Phase 1
streamer wiring). Auto-backfills on miss."""

from __future__ import annotations

import time

import pandas as pd

from ..logging import get_logger
from .backfill import INTRADAY_LOOKBACK_DAYS, Backfiller
from .store import SCHWAB_INTRADAY_TFS, CandleStore, base_tf

log = get_logger("marketdata")

_DAY_MS = 86_400_000


class MarketDataService:
    def __init__(self, store: CandleStore, backfiller: Backfiller):
        self.store = store
        self.backfill = backfiller
        self._quote_cache: dict[str, dict] = {}  # symbol -> last quote (stream/REST)

    async def get_candles(
        self, symbol: str, tf: str, start_ms: int, end_ms: int
    ) -> pd.DataFrame:
        """Serve from the local store, backfilling when the requested range
        extends beyond current coverage."""
        btf = base_tf(tf)
        cov = await self.store.coverage(symbol, btf)
        if cov is None:
            if btf in SCHWAB_INTRADAY_TFS and await self.store.coverage(symbol, "1m"):
                # no native bars yet, but 1m is present — try to deepen from
                # Schwab, and fall back to aggregating 1m if that isn't possible
                # (offline / not connected). read() handles the fallback.
                try:
                    await self.backfill.backfill_intraday_max(symbol, btf)
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "intraday_backfill_failed", symbol=symbol, tf=btf, error=str(e)
                    )
            else:
                await self.backfill.ensure_symbol(symbol)
        else:
            first, last, _ = cov
            now = int(time.time() * 1000)
            if btf == "1m":
                # Schwab serves only ~6 weeks of minute history; don't chase gaps
                # beyond that (older 1m data only exists if we recorded it)
                lookback_floor = now - 200 * _DAY_MS
                want_start = max(start_ms, lookback_floor)
                if want_start < first - _DAY_MS:
                    await self.backfill.backfill_minute_range(symbol, want_start, first)
                if end_ms > last + 90 * 60 * 1000:  # >90min past coverage
                    await self.backfill.backfill_minute_range(symbol, last, end_ms)
            elif btf in SCHWAB_INTRADAY_TFS:
                # native 5/15/30m reach ~8.5 months back — much further than 1m
                lookback_floor = now - INTRADAY_LOOKBACK_DAYS * _DAY_MS
                want_start = max(start_ms, lookback_floor)
                if want_start < first - _DAY_MS:
                    await self.backfill.backfill_intraday_range(
                        symbol, btf, want_start, first
                    )
                if end_ms > last + _DAY_MS:
                    await self.backfill.backfill_intraday_range(symbol, btf, last, end_ms)
            else:  # 1d
                if end_ms > last + _DAY_MS:
                    await self.backfill.nightly_topup([symbol])
        return await self.store.read(symbol, tf, start_ms, end_ms)

    # ---- quotes ---------------------------------------------------------
    def cache_quote(self, symbol: str, quote: dict) -> None:
        quote["cached_at"] = time.time()
        self._quote_cache[symbol] = quote

    def latest_cached_quote(self, symbol: str, max_age_s: float = 10.0) -> dict | None:
        q = self._quote_cache.get(symbol)
        if q and time.time() - q["cached_at"] <= max_age_s:
            return q
        return None
