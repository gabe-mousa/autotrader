"""CandleRecorder + quote cache feed.

PRIMARY-GOAL component (decision log #1): every CHART_EQUITY 1-min bar that
streams in is persisted immediately — recorded days become permanent intraday
backtest depth beyond Schwab's ~6-month REST lookback.

The recording watchlist persists in app_state['recording_watchlist'] and is
subscribed on every startup, independent of any strategy."""

from __future__ import annotations

from ..db import Database
from ..logging import get_logger
from ..marketdata.service import MarketDataService
from ..marketdata.store import Candle, CandleStore
from .client import StreamerClient

log = get_logger("recorder")

# CHART_EQUITY numeric field map: Schwab's published field table (see
# schwab/market-data-production/documentation.md) says 1=open, 2=high,
# 3=low, 4=close, 5=volume, 6=sequence — but that's wrong on the wire. Real
# captured payloads (app/streamer/chart_raw.py's chart_equity_raw, gathered
# 2026-07-24 to root-cause chart_bar_rejected_invalid_ohlc) show every
# field shifted one position later: 1=sequence (a minute-index counter,
# identical across all symbols in the same push, +1/minute), 2=open,
# 3=high, 4=low, 5=close, 6=volume. 7=chart time ms and 8=chart day DO
# match the docs. Confirmed against 12 real samples across 4 symbols/3
# batches with zero exceptions — see backend/tests/test_streamer.py and
# backend/tests/test_bar_aggregator.py's real-payload fixture tests.
# 0=key(symbol via "key"), 1=sequence, 2=open, 3=high, 4=low, 5=close, 6=volume, 7=chart time ms, 8=chart day


class CandleRecorder:
    def __init__(self, db: Database, store: CandleStore, streamer: StreamerClient,
                 mds: MarketDataService):
        self._db = db
        self._store = store
        self._streamer = streamer
        self._mds = mds
        self.bars_recorded = 0
        streamer.on_data("CHART_EQUITY", self._on_chart)
        streamer.on_data("LEVELONE_EQUITIES", self._on_quote)

    # ---- watchlist ------------------------------------------------------
    async def get_watchlist(self) -> list[str]:
        return await self._db.state_get("recording_watchlist", [])

    async def set_watchlist(self, symbols: list[str]) -> list[str]:
        symbols = sorted({s.strip().upper() for s in symbols if s.strip()})
        old = await self.get_watchlist()
        await self._db.state_set("recording_watchlist", symbols)
        added = [s for s in symbols if s not in old]
        removed = [s for s in old if s not in symbols]
        if added:
            await self._streamer.subscribe("CHART_EQUITY", added)
            await self._streamer.subscribe("LEVELONE_EQUITIES", added)
        if removed:
            await self._streamer.unsubscribe("CHART_EQUITY", removed)
            await self._streamer.unsubscribe("LEVELONE_EQUITIES", removed)
        await self._db.audit("user", "watchlist", {"symbols": symbols})
        return symbols

    async def start(self) -> None:
        """Subscribe the persisted watchlist (call once at startup, after the
        streamer task is started; subscriptions flush on LOGIN)."""
        wl = await self.get_watchlist()
        if wl:
            await self._streamer.subscribe("CHART_EQUITY", wl)
            await self._streamer.subscribe("LEVELONE_EQUITIES", wl)
            log.info("recording_watchlist_active", symbols=wl)

    # ---- handlers -------------------------------------------------------
    async def _on_chart(self, content: list[dict]) -> None:
        by_symbol: dict[str, list[Candle]] = {}
        for item in content:
            sym = item.get("key")
            ts = item.get("7")
            if not sym or ts is None:
                continue
            o, h, lo, c = (float(item.get(f, 0)) for f in ("2", "3", "4", "5"))
            v = int(item.get("6", 0))
            # sanity guard: a real bar always has low <= {open,close} <= high.
            # Kept even after root-causing the 2026-07-23/24 rejections (see
            # the field-map comment above) — it's cheap insurance against
            # whatever the NEXT wire surprise turns out to be. Silently
            # upserting a bad bar would poison the candle store (source=
            # 'stream') and feed a live BarAggregator-driven strategy bogus
            # data. Reject rather than guess — same philosophy as the
            # crossed-market guard in _on_quote below.
            if not (lo <= h and lo <= o <= h and lo <= c <= h):
                log.warning("chart_bar_rejected_invalid_ohlc", symbol=sym, ts=ts,
                           open=o, high=h, low=lo, close=c)
                continue
            by_symbol.setdefault(sym, []).append(
                Candle(ts=int(ts), open=o, high=h, low=lo, close=c, volume=v))
        for sym, candles in by_symbol.items():
            self.bars_recorded += await self._store.upsert(sym, "1m", candles, "stream")

    async def _on_quote(self, content: list[dict]) -> None:
        # LEVELONE_EQUITIES: 1=bid, 2=ask, 3=last (fields are deltas — merge)
        for item in content:
            sym = item.get("key")
            if not sym:
                continue
            prev = self._mds.latest_cached_quote(sym, max_age_s=3600) or {}
            quote = {
                "bid": item.get("1", prev.get("bid")),
                "ask": item.get("2", prev.get("ask")),
                "last": item.get("3", prev.get("last")),
            }
            # sanity guard: never cache crossed/zero markets (plan 04 data-quality)
            bid, ask = quote.get("bid"), quote.get("ask")
            if bid is not None and ask is not None and (bid > ask or ask <= 0):
                continue
            self._mds.cache_quote(sym, quote)
