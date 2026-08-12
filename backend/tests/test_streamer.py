"""Streamer tests: SubscriptionManager refcounting, CandleRecorder parsing
(CHART_EQUITY delta messages -> store), quote merge + sanity guard."""

from __future__ import annotations

import pytest

from app.db import Database
from app.marketdata.backfill import Backfiller
from app.marketdata.service import MarketDataService
from app.marketdata.store import CandleStore
from app.streamer.client import SubscriptionManager
from app.streamer.recorder import CandleRecorder


# ---- subscription manager -------------------------------------------------

def test_refcounting_add_release():
    m = SubscriptionManager()
    assert m.acquire("CHART_EQUITY", ["SPY", "QQQ"]) == ["SPY", "QQQ"]
    assert m.acquire("CHART_EQUITY", ["SPY"]) == []          # already on the wire
    assert m.release("CHART_EQUITY", ["SPY"]) == []          # still one holder
    assert m.release("CHART_EQUITY", ["SPY"]) == ["SPY"]     # last holder gone
    assert m.active("CHART_EQUITY") == ["QQQ"]
    assert m.release("CHART_EQUITY", ["NOPE"]) == []         # unknown symbol is a no-op


def test_services_lists_only_nonempty():
    m = SubscriptionManager()
    m.acquire("CHART_EQUITY", ["SPY"])
    m.acquire("LEVELONE_EQUITIES", ["SPY"])
    m.release("LEVELONE_EQUITIES", ["SPY"])
    assert m.services() == ["CHART_EQUITY"]


# ---- recorder -------------------------------------------------------------

class FakeStreamer:
    """Captures handler registrations and subscribe calls."""

    def __init__(self):
        self.handlers = {}
        self.subscribed: list[tuple[str, list[str]]] = []

    def on_data(self, service, handler):
        self.handlers[service] = handler

    async def subscribe(self, service, symbols):
        self.subscribed.append((service, symbols))

    async def unsubscribe(self, service, symbols):
        self.subscribed.append((f"UN:{service}", symbols))


@pytest.fixture
async def rig(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    store = CandleStore(db)
    mds = MarketDataService(store, Backfiller(None, store))
    streamer = FakeStreamer()
    rec = CandleRecorder(db, store, streamer, mds)
    yield db, store, mds, streamer, rec
    await db.close()


async def test_chart_bars_recorded(rig):
    # field layout matches the real wire shape confirmed in chart_equity_raw
    # (2026-07-24): 1=sequence, 2=open, 3=high, 4=low, 5=close, 6=volume —
    # see recorder.py's field-map comment for the full writeup.
    db, store, mds, streamer, rec = rig
    await streamer.handlers["CHART_EQUITY"]([
        {"key": "SPY", "1": 1, "2": 100.0, "3": 101.0, "4": 99.5, "5": 100.5, "6": 1200,
         "7": 1_750_000_060_000},
        {"key": "SPY", "1": 2, "2": 100.5, "3": 102.0, "4": 100.0, "5": 101.5, "6": 900,
         "7": 1_750_000_120_000},
        {"key": "QQQ", "1": 1, "2": 400.0, "3": 401.0, "4": 399.0, "5": 400.5, "6": 500,
         "7": 1_750_000_060_000},
    ])
    assert rec.bars_recorded == 3
    df = await store.read("SPY", "1m", 0, 2_000_000_000_000)
    assert len(df) == 2 and df.iloc[1]["close"] == 101.5
    # re-delivery of the same bar (reconnect dedupe) just overwrites, no dupes
    await streamer.handlers["CHART_EQUITY"]([
        {"key": "SPY", "1": 2, "2": 100.5, "3": 102.0, "4": 100.0, "5": 101.5, "6": 900,
         "7": 1_750_000_120_000}])
    df = await store.read("SPY", "1m", 0, 2_000_000_000_000)
    assert len(df) == 2


async def test_chart_bar_with_invalid_ohlc_is_rejected(rig):
    """Regression: found live 2026-07-23 at market close — some CHART_EQUITY
    content had open far outside [low, high]. Silently upserting it poisoned
    the candle store with source='stream' rows and would have fed a live
    BarAggregator-driven strategy the same bogus bar. A real bar always has
    low <= {open,close} <= high; this must never pass that check."""
    db, store, mds, streamer, rec = rig
    await streamer.handlers["CHART_EQUITY"]([
        {"key": "SPY", "1": 1, "2": 100.0, "3": 101.0, "4": 99.5, "5": 100.5, "6": 1200,
         "7": 1_750_000_060_000},
        # low(738.98) > high(738.22): impossible bar, must be rejected
        {"key": "SPY", "1": 2, "2": 540.0, "3": 738.22, "4": 738.98, "5": 738.18, "6": 738,
         "7": 1_750_000_120_000},
    ])
    assert rec.bars_recorded == 1
    df = await store.read("SPY", "1m", 0, 2_000_000_000_000)
    assert len(df) == 1 and df.iloc[0]["close"] == 100.5


async def test_real_captured_chart_equity_payloads_are_parsed_correctly(rig):
    """Regression for the chart_bar_rejected_invalid_ohlc incident
    (2026-07-23/24): every live CHART_EQUITY bar was rejected because the
    code trusted Schwab's PUBLISHED field map (1=open,...,6=sequence)
    instead of the real wire layout (1=sequence,...,6=volume) confirmed via
    streamer/chart_raw.py's raw capture. Field "1" is a minute-index
    sequence counter identical across symbols in the same push (531 here)
    -- landing in "open" is exactly why low<=open<=high failed for every
    single bar. These are verbatim captures (chart_equity_raw ids 1-4) from
    a real Schwab CHART_EQUITY push covering QQQ/GOOG/WEN/SPY."""
    db, store, mds, streamer, rec = rig
    await streamer.handlers["CHART_EQUITY"]([
        {"seq": 1300, "key": "QQQ", "1": 531, "2": 683.065, "3": 683.2298,
         "4": 682.83, "5": 683.12, "6": 159825, "7": 1784922660000, "8": 20658},
        {"seq": 1305, "key": "GOOG", "1": 531, "2": 319.255, "3": 319.4399,
         "4": 319.2301, "5": 319.315, "6": 67878, "7": 1784922660000, "8": 20658},
        {"seq": 0, "key": "WEN", "1": 531, "2": 6.98, "3": 6.99,
         "4": 6.98, "5": 6.99, "6": 18747, "7": 1784922660000, "8": 20658},
        {"seq": 1282, "key": "SPY", "1": 531, "2": 737.85, "3": 737.86,
         "4": 737.7, "5": 737.81, "6": 158084.24503, "7": 1784922660000, "8": 20658},
    ])
    assert rec.bars_recorded == 4
    df = await store.read("WEN", "1m", 0, 2_000_000_000_000)
    assert len(df) == 1
    row = df.iloc[0]
    assert (row["open"], row["high"], row["low"], row["close"], row["volume"]) == \
        (6.98, 6.99, 6.98, 6.99, 18747)


async def test_quote_delta_merge_and_sanity(rig):
    db, store, mds, streamer, rec = rig
    q = streamer.handlers["LEVELONE_EQUITIES"]
    await q([{"key": "SPY", "1": 100.0, "2": 100.05, "3": 100.02}])
    assert mds.latest_cached_quote("SPY")["bid"] == 100.0
    # delta update: only last changes; bid/ask carried forward
    await q([{"key": "SPY", "3": 100.04}])
    quote = mds.latest_cached_quote("SPY")
    assert quote["last"] == 100.04 and quote["ask"] == 100.05
    # crossed market rejected (cache retains previous good quote)
    await q([{"key": "SPY", "1": 101.0, "2": 100.0}])
    assert mds.latest_cached_quote("SPY")["last"] == 100.04


async def test_watchlist_roundtrip_and_diffs(rig):
    db, store, mds, streamer, rec = rig
    saved = await rec.set_watchlist(["spy", "QQQ", " qqq "])
    assert saved == ["QQQ", "SPY"]
    assert ("CHART_EQUITY", ["QQQ", "SPY"]) in streamer.subscribed
    streamer.subscribed.clear()
    await rec.set_watchlist(["QQQ"])  # SPY removed
    assert ("UN:CHART_EQUITY", ["SPY"]) in streamer.subscribed
    assert await rec.get_watchlist() == ["QQQ"]
