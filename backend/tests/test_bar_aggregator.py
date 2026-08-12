"""BarAggregator tests: 1m passthrough, N-minute bucketing, provisional-close
watchdog, ref-counted subscription, and a cross-check that live bucketing
produces IDENTICAL boundaries to CandleStore's backtest-side pandas resample
(the property the whole design depends on)."""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pandas as pd
import pytest

from app.runtime.bar_aggregator import BarAggregator, BarEvent

MIN_MS = 60_000


class FakeStreamer:
    def __init__(self):
        self.handler = None
        self.subscribed: list[tuple[str, list[str]]] = []
        self.unsubscribed: list[tuple[str, list[str]]] = []

    def on_data(self, service, handler):
        self.handler = handler

    async def subscribe(self, service, symbols):
        self.subscribed.append((service, list(symbols)))

    async def unsubscribe(self, service, symbols):
        self.unsubscribed.append((service, list(symbols)))

    async def push(self, content):
        await self.handler(content)


def bar(symbol, ts, o, h, lo, c, v, seq=1):
    # field layout matches the real wire shape confirmed in
    # chart_equity_raw (2026-07-24), NOT Schwab's published field table —
    # see streamer/recorder.py's field-map comment for the full writeup.
    return {"key": symbol, "1": seq, "2": o, "3": h, "4": lo, "5": c, "6": v, "7": ts}


class Collector:
    def __init__(self):
        self.events: list[BarEvent] = []

    async def __call__(self, event: BarEvent) -> None:
        self.events.append(event)


@pytest.fixture
def streamer():
    return FakeStreamer()


# ---- 1m passthrough ---------------------------------------------------------

async def test_1m_passthrough_emits_immediately(streamer):
    agg = BarAggregator(streamer)
    coll = Collector()
    await agg.register("SPY", "1m", coll)
    await streamer.push([bar("SPY", 60_000, 100, 101, 99, 100.5, 500)])
    assert len(coll.events) == 1
    e = coll.events[0]
    assert (e.symbol, e.tf, e.ts) == ("SPY", "1m", 60_000)
    assert not e.provisional


async def test_bar_with_invalid_ohlc_is_rejected_not_emitted(streamer):
    """Regression: found live 2026-07-23 at market close (same incident as
    streamer/recorder.py's identical guard) — a CHART_EQUITY item with
    open=540 while high/low were both ~738 (low>high) would otherwise have
    been fed straight into a running strategy's rule evaluation/stop checks
    as a real BarClosed event. A real bar always has low <= {open,close} <=
    high; anything that fails that must never reach a callback."""
    agg = BarAggregator(streamer)
    coll = Collector()
    await agg.register("SPY", "1m", coll)
    await streamer.push([bar("SPY", 60_000, 540.0, 738.22, 738.98, 738.18, 738)])
    assert coll.events == []
    # a valid bar right after still comes through fine
    await streamer.push([bar("SPY", 120_000, 100, 101, 99, 100.5, 500)])
    assert len(coll.events) == 1


async def test_real_captured_chart_equity_payload_is_parsed_correctly(streamer):
    """Regression for the chart_bar_rejected_invalid_ohlc incident (2026-07-23/24):
    every CHART_EQUITY bar was being rejected because the code assumed
    Schwab's PUBLISHED field map (1=open,2=high,3=low,4=close,5=volume,
    6=sequence), but real wire payloads captured via
    streamer/chart_raw.py's chart_equity_raw are shifted one field later
    (1=sequence, 2=open, 3=high, 4=low, 5=close, 6=volume) — field "1" was
    landing in "open" and was actually a minute-index counter (identical
    across symbols in the same push, +1/minute), which is why every bar
    failed the low<=open<=high sanity check. This is a verbatim capture
    (backend/.autotrader/autotrader.db, chart_equity_raw id=3) from a real
    Schwab CHART_EQUITY push for WEN."""
    agg = BarAggregator(streamer)
    coll = Collector()
    await agg.register("WEN", "1m", coll)
    real_payload = {
        "seq": 0, "key": "WEN",
        "1": 531, "2": 6.98, "3": 6.99, "4": 6.98, "5": 6.99,
        "6": 18747, "7": 1784922660000, "8": 20658,
    }
    await streamer.push([real_payload])

    assert len(coll.events) == 1
    e = coll.events[0]
    assert (e.open, e.high, e.low, e.close, e.volume) == (6.98, 6.99, 6.98, 6.99, 18747)
    assert e.ts == 1784922660000


# ---- N-minute bucketing ------------------------------------------------------

async def test_5m_bucket_closes_on_next_window_bar(streamer):
    agg = BarAggregator(streamer)
    coll = Collector()
    base = (int(time.time() * 1000) // (5 * MIN_MS)) * (5 * MIN_MS)
    await agg.register("SPY", "5m", coll)

    for i, (o, h, lo, c, v) in enumerate([
        (100, 101, 99, 100.5, 100), (100.5, 102, 100, 101.5, 150),
        (101.5, 103, 101, 102.5, 120), (102.5, 104, 102, 103.5, 90),
        (103.5, 105, 103, 104.5, 200),
    ]):
        await streamer.push([bar("SPY", base + i * MIN_MS, o, h, lo, c, v)])
    assert coll.events == []  # window not closed yet — no bar for the NEXT window seen

    await streamer.push([bar("SPY", base + 5 * MIN_MS, 200, 201, 199, 200, 1)])
    assert len(coll.events) == 1
    e = coll.events[0]
    assert e.ts == base
    assert e.open == 100 and e.close == 104.5
    assert e.high == 105 and e.low == 99
    assert e.volume == 100 + 150 + 120 + 90 + 200
    assert not e.provisional


async def test_multiple_callbacks_same_symbol_tf(streamer):
    agg = BarAggregator(streamer)
    c1, c2 = Collector(), Collector()
    await agg.register("SPY", "1m", c1)
    await agg.register("SPY", "1m", c2)
    await streamer.push([bar("SPY", 60_000, 1, 2, 0, 1, 10)])
    assert len(c1.events) == 1 and len(c2.events) == 1


async def test_unregister_stops_only_that_callback(streamer):
    agg = BarAggregator(streamer)
    c1, c2 = Collector(), Collector()
    await agg.register("SPY", "1m", c1)
    await agg.register("SPY", "1m", c2)
    await agg.unregister("SPY", "1m", c1)
    await streamer.push([bar("SPY", 60_000, 1, 2, 0, 1, 10)])
    assert len(c1.events) == 0 and len(c2.events) == 1


# ---- ref-counted subscription -------------------------------------------------

async def test_subscribe_once_per_symbol_unsubscribe_when_last_consumer_gone(streamer):
    agg = BarAggregator(streamer)
    c1, c2 = Collector(), Collector()
    await agg.register("SPY", "1m", c1)
    await agg.register("SPY", "5m", c2)  # same symbol, different tf
    assert streamer.subscribed == [("CHART_EQUITY", ["SPY"])]  # only subscribed once

    await agg.unregister("SPY", "1m", c1)
    assert streamer.unsubscribed == []  # 5m consumer still active
    await agg.unregister("SPY", "5m", c2)
    assert streamer.unsubscribed == [("CHART_EQUITY", ["SPY"])]


# ---- watchdog -----------------------------------------------------------------

async def test_watchdog_force_closes_stalled_bucket(streamer):
    agg = BarAggregator(streamer, provisional_wait_s=0.05, watchdog_interval_s=0.02)
    coll = Collector()
    now = int(time.time() * 1000)
    # a window whose END is already safely in the past (not just its start) —
    # e.g. two windows back — so the watchdog's overdue check fires promptly
    base = (now // (5 * MIN_MS)) * (5 * MIN_MS) - 2 * 5 * MIN_MS
    await agg.register("SPY", "5m", coll)
    await streamer.push([bar("SPY", base, 100, 101, 99, 100.5, 500)])
    assert coll.events == []
    await asyncio.sleep(0.3)  # well past bucket end + wait_s
    assert len(coll.events) == 1
    assert coll.events[0].provisional is True
    await agg.stop()


async def test_no_premature_watchdog_close(streamer):
    """A bucket whose window hasn't even ended yet must never be force-closed,
    regardless of how long the watchdog has been running."""
    agg = BarAggregator(streamer, provisional_wait_s=0.05, watchdog_interval_s=0.02)
    coll = Collector()
    # bucket for the CURRENT still-open 5-minute window (not yet ended)
    now = int(time.time() * 1000)
    base = (now // (5 * MIN_MS)) * (5 * MIN_MS)
    if now - base > 4 * MIN_MS:  # avoid flaky edge-of-window test runs
        base += 5 * MIN_MS
    await agg.register("SPY", "5m", coll)
    await streamer.push([bar("SPY", base, 100, 101, 99, 100.5, 500)])
    await asyncio.sleep(0.15)
    assert coll.events == [], "bucket closed before its window even ended"
    await agg.stop()


# ---- consistency with the backtest-side resample ------------------------------

async def test_matches_candlestore_resample_boundaries(streamer):
    """The property the whole design depends on: live epoch-based bucketing
    must produce the SAME open/high/low/close/volume as CandleStore.read's
    pandas resample(origin='start_day') for the same 1-minute data."""
    rng = np.random.default_rng(7)
    base = (int(time.time() * 1000) // (5 * MIN_MS)) * (5 * MIN_MS) - 20 * MIN_MS
    n = 37  # not a clean multiple of 5 -> exercises a trailing partial bucket
    closes = 100 + np.cumsum(rng.normal(0, 0.1, n))
    rows = []
    for i in range(n):
        o = closes[i - 1] if i else closes[0]
        c = closes[i]
        h, lo = max(o, c) + 0.05, min(o, c) - 0.05
        v = int(rng.integers(50, 200))
        rows.append((base + i * MIN_MS, o, h, lo, c, v))

    agg = BarAggregator(streamer)
    coll = Collector()
    await agg.register("SPY", "5m", coll)
    for ts, o, h, lo, c, v in rows:
        await streamer.push([bar("SPY", ts, o, h, lo, c, v)])
    # force-close the trailing partial bucket the same way the watchdog would,
    # so we can compare it too
    for key in list(agg._buckets):
        await agg._close_bucket(key, provisional=True)

    # reference: pandas resample exactly as CandleStore.read does it
    idx = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True)
    df = pd.DataFrame({"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                       "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                       "volume": [r[5] for r in rows]}, index=idx)
    ref = (df.resample("5min", origin="start_day", label="left", closed="left")
          .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
               close=("close", "last"), volume=("volume", "sum"))
          .dropna(subset=["open"]))

    assert len(coll.events) == len(ref)
    for event, (ref_ts, ref_row) in zip(coll.events, ref.iterrows()):
        assert event.ts == int(ref_ts.timestamp() * 1000)
        assert event.open == pytest.approx(ref_row["open"])
        assert event.high == pytest.approx(ref_row["high"])
        assert event.low == pytest.approx(ref_row["low"])
        assert event.close == pytest.approx(ref_row["close"])
        assert event.volume == ref_row["volume"]
