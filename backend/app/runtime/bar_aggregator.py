"""BarAggregator — rolls live 1-minute CHART_EQUITY bars into a strategy's
configured timeframe, emitting a callback exactly when each bar closes.

Deferred from Phase 1 (charts/backtests only needed on-read aggregation via
CandleStore.read's pandas resample); needed now because live strategies need
a discrete "this bar just closed, go evaluate rules" EVENT, not a queryable
series.

Correctness-critical: bucket boundaries here MUST agree with
CandleStore.read's `resample(rule, origin="start_day", label="left",
closed="left")`, or a live-run's indicator values would diverge from what the
same strategy saw in backtesting. Because 1970-01-01 (the Unix epoch) is
itself a UTC midnight, and 1/5/15/30/60/1440-minute windows all divide 1440
evenly, flooring RAW EPOCH MILLISECONDS to a multiple of the timeframe's
interval produces IDENTICAL bucket boundaries to pandas' "start_day" origin —
no timezone conversion needed, just integer division. (For 1-day buckets this
also happens to correctly group a single US trading session, since regular
hours 13:30-21:00 UTC never cross a UTC midnight.)"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..logging import get_logger

log = get_logger("bar-aggregator")

TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}
DEFAULT_PROVISIONAL_WAIT_S = 15.0
WATCHDOG_INTERVAL_S = 5.0


@dataclass(frozen=True)
class BarEvent:
    symbol: str
    tf: str
    ts: int              # epoch ms, bucket start (bar open time) — matches Candle.ts
    open: float
    high: float
    low: float
    close: float
    volume: int
    provisional: bool = False  # True if force-closed by the watchdog (feed stall)


BarCallback = Callable[[BarEvent], Awaitable[None]]


@dataclass
class _Bucket:
    bucket_start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


class BarAggregator:
    def __init__(self, streamer, provisional_wait_s: float = DEFAULT_PROVISIONAL_WAIT_S,
                watchdog_interval_s: float = WATCHDOG_INTERVAL_S):
        self._streamer = streamer
        self._wait_s = provisional_wait_s
        self._watchdog_interval_s = watchdog_interval_s
        self._callbacks: dict[tuple[str, str], list[BarCallback]] = defaultdict(list)
        self._symbol_tfs: dict[str, set[str]] = defaultdict(set)
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._handler_registered = False

    # ---- registration (ref-counted; multiple strategies can share a symbol/tf) --
    async def register(self, symbol: str, tf: str, callback: BarCallback) -> None:
        if tf not in TF_MINUTES:
            raise ValueError(f"unsupported timeframe {tf!r}")
        key = (symbol, tf)
        first_for_symbol = symbol not in self._symbol_tfs or not self._symbol_tfs[symbol]
        self._callbacks[key].append(callback)
        self._symbol_tfs[symbol].add(tf)
        if first_for_symbol:
            await self._streamer.subscribe("CHART_EQUITY", [symbol])
        if not self._handler_registered:
            self._streamer.on_data("CHART_EQUITY", self._on_chart)
            self._handler_registered = True
        self._ensure_watchdog()

    async def unregister(self, symbol: str, tf: str, callback: BarCallback) -> None:
        key = (symbol, tf)
        callbacks = self._callbacks.get(key, [])
        if callback in callbacks:
            callbacks.remove(callback)
        if not callbacks:
            self._callbacks.pop(key, None)
            self._buckets.pop(key, None)
            self._symbol_tfs[symbol].discard(tf)
            if not self._symbol_tfs[symbol]:
                self._symbol_tfs.pop(symbol, None)
                await self._streamer.unsubscribe("CHART_EQUITY", [symbol])

    async def stop(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass

    def _ensure_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="bar-watchdog")

    # ---- ingestion ----------------------------------------------------------
    async def _on_chart(self, content: list[dict]) -> None:
        for item in content:
            symbol = item.get("key")
            ts = item.get("7")
            if not symbol or ts is None or symbol not in self._symbol_tfs:
                continue
            ts = int(ts)
            o, h, lo, c = (float(item.get(f, 0)) for f in ("2", "3", "4", "5"))
            v = int(item.get("6", 0))
            # sanity guard: a real bar always has low <= {open,close} <= high —
            # see the identical guard + full incident writeup in
            # streamer/recorder.py's _on_chart (found live, 2026-07-23 market
            # close). Feeding a bogus bar straight into a running strategy's
            # rule evaluation/stop checks here is the live-runtime equivalent
            # of the candle-store corruption found there — same root cause,
            # same fix, independently applied since this class has no
            # dependency on the recorder.
            if not (lo <= h and lo <= o <= h and lo <= c <= h):
                log.warning("chart_bar_rejected_invalid_ohlc", symbol=symbol, ts=ts,
                           open=o, high=h, low=lo, close=c)
                continue
            for tf in list(self._symbol_tfs[symbol]):
                await self._ingest_one(symbol, tf, ts, o, h, lo, c, v)

    async def _ingest_one(self, symbol: str, tf: str, ts: int, o: float, h: float,
                          lo: float, c: float, v: int) -> None:
        interval_ms = TF_MINUTES[tf] * 60_000
        key = (symbol, tf)

        if tf == "1m":
            # a 1-minute bar arriving already IS one closed bar — no buffering
            await self._emit(key, BarEvent(symbol, tf, ts, o, h, lo, c, v))
            return

        bucket_start = (ts // interval_ms) * interval_ms
        existing = self._buckets.get(key)
        if existing is None:
            self._buckets[key] = _Bucket(bucket_start, o, h, lo, c, v)
        elif existing.bucket_start_ms == bucket_start:
            existing.high = max(existing.high, h)
            existing.low = min(existing.low, lo)
            existing.close = c
            existing.volume += v
        else:
            # a bar for a NEW window arrived -> the previous window is done
            await self._close_bucket(key, provisional=False)
            self._buckets[key] = _Bucket(bucket_start, o, h, lo, c, v)

    async def _close_bucket(self, key: tuple[str, str], provisional: bool) -> None:
        bucket = self._buckets.pop(key, None)
        if bucket is None:
            return
        symbol, tf = key
        await self._emit(key, BarEvent(symbol, tf, bucket.bucket_start_ms, bucket.open,
                                       bucket.high, bucket.low, bucket.close, bucket.volume,
                                       provisional=provisional))

    async def _emit(self, key: tuple[str, str], event: BarEvent) -> None:
        for cb in list(self._callbacks.get(key, [])):
            try:
                await cb(event)
            except Exception as e:  # noqa: BLE001 — one strategy's bug can't break another's bars
                log.error("bar_callback_error", symbol=event.symbol, tf=event.tf, error=str(e))

    # ---- provisional-close watchdog ------------------------------------------
    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(self._watchdog_interval_s)
            now_ms = int(time.time() * 1000)
            for key, bucket in list(self._buckets.items()):
                _, tf = key
                interval_ms = TF_MINUTES[tf] * 60_000
                bucket_end_ms = bucket.bucket_start_ms + interval_ms
                if now_ms >= bucket_end_ms + self._wait_s * 1000:
                    log.warning("bar_provisional_close", symbol=key[0], tf=key[1],
                               bucket_start=bucket.bucket_start_ms)
                    await self._close_bucket(key, provisional=True)
