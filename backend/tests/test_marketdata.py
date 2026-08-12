"""Phase 1 tests: candle store upsert precedence + aggregation, backfill
chunking against a fake Schwab client, service auto-backfill."""

from __future__ import annotations

import pandas as pd
import pytest

from app.db import Database
from app.marketdata.backfill import (
    DAILY_HISTORY_START_MS,
    INTRADAY_CHUNK_DAYS,
    Backfiller,
)
from app.marketdata.service import MarketDataService
from app.marketdata.store import (
    SCHWAB_INTRADAY_TFS,
    Candle,
    CandleStore,
    base_tf,
)

import time

MIN_MS = 60_000
DAY_MS = 86_400_000
T0 = 1_750_000_000_000  # fixed anchor for pure store tests
NOW = int(time.time() * 1000)  # real-clock anchor for backfill tests
# (backfill_minute_max uses the real clock, so fake data windows must too)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


def mk(ts: int, px: float = 100.0, vol: int = 10) -> Candle:
    return Candle(ts=ts, open=px, high=px + 1, low=px - 1, close=px + 0.5, volume=vol)


# ---- store ----------------------------------------------------------------

async def test_upsert_idempotent_and_rest_wins(db):
    store = CandleStore(db)
    await store.upsert("SPY", "1m", [mk(T0, 100)], "stream")
    # REST overwrites stream
    await store.upsert("SPY", "1m", [mk(T0, 200)], "schwab_rest")
    df = await store.read("SPY", "1m", T0, T0)
    assert df.iloc[0]["open"] == 200
    # stream does NOT overwrite REST
    await store.upsert("SPY", "1m", [mk(T0, 300)], "stream")
    df = await store.read("SPY", "1m", T0, T0)
    assert df.iloc[0]["open"] == 200
    cov = await store.coverage("SPY", "1m")
    assert cov == (T0, T0, 1)


async def test_aggregation_5m(db):
    store = CandleStore(db)
    # 10 consecutive 1-min bars, rising close
    base = (T0 // (5 * MIN_MS)) * (5 * MIN_MS)  # align to a 5m boundary
    bars = [mk(base + i * MIN_MS, 100 + i) for i in range(10)]
    await store.upsert("SPY", "1m", bars, "schwab_rest")
    df = await store.read("SPY", "5m", base, base + 10 * MIN_MS)
    assert len(df) == 2
    first = df.iloc[0]
    assert first["open"] == 100          # first bar's open
    assert first["high"] == 105          # max high of bars 0-4 (104+1)
    assert first["low"] == 99            # min low (100-1)
    assert first["close"] == 104.5       # last bar's close (104+0.5)
    assert first["volume"] == 50


async def test_native_30m_preferred_over_aggregating_1m(db):
    """Stored 30m bars win over 1m aggregation — that's the whole point of
    storing them (Schwab serves 30m back ~8.5 months vs ~6 weeks for 1m)."""
    store = CandleStore(db)
    base = (T0 // (30 * MIN_MS)) * (30 * MIN_MS)
    # 1m bars covering the same slot, but with a distinctive open
    await store.upsert(
        "SPY", "1m", [mk(base + i * MIN_MS, 100) for i in range(30)], "schwab_rest"
    )
    await store.upsert("SPY", "30m", [mk(base, 777)], "schwab_rest")
    df = await store.read("SPY", "30m", base, base + 30 * MIN_MS)
    assert len(df) == 1
    assert df.iloc[0]["open"] == 777


async def test_30m_falls_back_to_aggregation_when_no_native_bars(db):
    """Symbols backfilled before native 30m existed still work offline."""
    store = CandleStore(db)
    base = (T0 // (30 * MIN_MS)) * (30 * MIN_MS)
    await store.upsert(
        "SPY", "1m", [mk(base + i * MIN_MS, 100 + i) for i in range(60)], "schwab_rest"
    )
    df = await store.read("SPY", "30m", base, base + 60 * MIN_MS)
    assert len(df) == 2
    assert df.iloc[0]["open"] == 100


async def test_30m_hybrid_splices_live_1m_tail(db):
    """Native bars give depth; anything newer than the last native bar is
    aggregated from streamed 1m so live runs stay current."""
    store = CandleStore(db)
    base = (T0 // (30 * MIN_MS)) * (30 * MIN_MS)
    await store.upsert("SPY", "30m", [mk(base, 777)], "schwab_rest")
    # streamed 1m bars for the *next* 30m slot only
    nxt = base + 30 * MIN_MS
    await store.upsert(
        "SPY", "1m", [mk(nxt + i * MIN_MS, 50 + i) for i in range(30)], "stream"
    )
    df = await store.read("SPY", "30m", base, nxt + 30 * MIN_MS)
    assert len(df) == 2
    assert df.iloc[0]["open"] == 777  # native bar untouched
    assert df.iloc[1]["open"] == 50   # tail aggregated from 1m
    assert df.index.is_monotonic_increasing


async def test_30m_hybrid_tail_never_overwrites_a_completed_native_bar(db):
    """1m bars inside an already-stored native slot must not re-aggregate it."""
    store = CandleStore(db)
    base = (T0 // (30 * MIN_MS)) * (30 * MIN_MS)
    await store.upsert("SPY", "30m", [mk(base, 777)], "schwab_rest")
    await store.upsert(
        "SPY", "1m", [mk(base + i * MIN_MS, 1) for i in range(30)], "stream"
    )
    df = await store.read("SPY", "30m", base, base + 30 * MIN_MS)
    assert len(df) == 1
    assert df.iloc[0]["open"] == 777


async def test_1h_falls_back_to_1m_when_no_30m_stored(db):
    """Schwab has no 1h frequency. With no 30m rows it still derives from 1m."""
    store = CandleStore(db)
    base = (T0 // (60 * MIN_MS)) * (60 * MIN_MS)
    await store.upsert(
        "SPY", "1m", [mk(base + i * MIN_MS, 100 + i) for i in range(60)], "schwab_rest"
    )
    df = await store.read("SPY", "1h", base, base + 60 * MIN_MS)
    assert len(df) == 1
    assert df.iloc[0]["open"] == 100


async def test_1h_sources_from_30m_for_depth_past_the_1m_wall(db):
    """1h must roll up 30m, not 1m: Schwab serves 30m ~260 days back but 1m only
    ~46, so sourcing 1h from 1m would throw away most of the usable history."""
    store = CandleStore(db)
    base = (T0 // (60 * MIN_MS)) * (60 * MIN_MS)
    old = base - 120 * 24 * 60 * MIN_MS  # 120 days back — far past the 1m wall
    old -= old % (60 * MIN_MS)
    await store.upsert(
        "SPY", "30m", [mk(old, 500), mk(old + 30 * MIN_MS, 600)], "schwab_rest"
    )
    # only recent 1m exists, as it would in reality
    await store.upsert(
        "SPY", "1m", [mk(base + i * MIN_MS, 100 + i) for i in range(60)], "stream"
    )
    df = await store.read("SPY", "1h", old, base + 60 * MIN_MS)
    assert base_tf("1h") == "30m"
    # the deep bar is present and built from the two 30m halves
    assert df.index[0] == pd.Timestamp(old, unit="ms", tz="UTC")
    assert df.iloc[0]["open"] == 500      # first half's open
    assert df.iloc[0]["high"] == 601      # max across both halves (600 + 1)
    assert df.iloc[0]["close"] == 600.5   # second half's close (600 + 0.5)


async def test_1h_from_30m_keeps_the_live_1m_tail(db):
    """Depth must not cost liveness: bars newer than the last stored 30m bar
    still come from streamed 1m."""
    store = CandleStore(db)
    base = (T0 // (60 * MIN_MS)) * (60 * MIN_MS)
    await store.upsert(
        "SPY", "30m", [mk(base, 777), mk(base + 30 * MIN_MS, 778)], "schwab_rest"
    )
    nxt = base + 60 * MIN_MS  # the following hour, streamed only
    await store.upsert(
        "SPY", "1m", [mk(nxt + i * MIN_MS, 42 + i) for i in range(60)], "stream"
    )
    df = await store.read("SPY", "1h", base, nxt + 60 * MIN_MS)
    assert len(df) == 2
    assert df.iloc[0]["open"] == 777  # completed native-derived hour
    assert df.iloc[1]["open"] == 42   # live tail from 1m


async def test_unsupported_tf_rejected(db):
    store = CandleStore(db)
    with pytest.raises(ValueError):
        await store.read("SPY", "2m", 0, 1)


# ---- backfill -------------------------------------------------------------

class FakeSchwab:
    """Serves synthetic 1-min bars for a fixed available window, and 1d bars."""

    def __init__(self, min_window: tuple[int, int]):
        self.min_window = min_window
        self.calls: list[dict] = []

    async def get_price_history(self, symbol, *, period_type=None, period=None,
                                frequency_type, frequency, start_ms=None, end_ms=None,
                                extended_hours=False):
        self.calls.append({"ft": frequency_type, "freq": frequency,
                           "start": start_ms, "end": end_ms,
                           "period_type": period_type, "period": period})
        if frequency_type == "daily":
            n = 5 if period_type == "month" else 100
            return {"candles": [
                {"datetime": NOW - i * DAY_MS, "open": 1, "high": 2, "low": 0.5,
                 "close": 1.5, "volume": 100} for i in range(n)]}
        # minute: clip request to the available window
        lo, hi = self.min_window
        s = max(start_ms or lo, lo)
        e = min(end_ms or hi, hi)
        if s > e:
            return {"candles": []}
        return {"candles": [
            {"datetime": ts, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 9}
            for ts in range(s - s % MIN_MS + MIN_MS, e, 30 * MIN_MS)]}


class TieredFakeSchwab(FakeSchwab):
    """Mimics Schwab's real behaviour: the lookback wall is per-frequency, so
    1m history is much shallower than 5/15/30m history."""

    def __init__(self, minute_days: int, intraday_days: int):
        super().__init__((NOW - minute_days * DAY_MS, NOW))
        self.minute_days = minute_days
        self.intraday_days = intraday_days

    async def get_price_history(self, symbol, *, frequency_type, frequency, **kw):
        days = self.minute_days if frequency == 1 else self.intraday_days
        self.min_window = (NOW - days * DAY_MS, NOW)
        return await super().get_price_history(
            symbol, frequency_type=frequency_type, frequency=frequency, **kw
        )


async def test_minute_backfill_chunks_to_10_days(db):
    fake = FakeSchwab((NOW - 30 * DAY_MS, NOW))
    bf = Backfiller(fake, CandleStore(db))
    await bf.backfill_minute_range("SPY", NOW - 25 * DAY_MS, NOW)
    minute_calls = [c for c in fake.calls if c["ft"] == "minute"]
    assert len(minute_calls) == 3  # 25 days -> ceil(25/10)
    for c in minute_calls:
        assert c["end"] - c["start"] < 10 * DAY_MS


async def test_daily_full_asks_for_entire_listed_history(db):
    """`period` caps at 20 years; periodType=year + explicit dates bypasses it.
    Regression guard — passing `period` here silently truncates deep history."""
    fake = FakeSchwab((NOW - 30 * DAY_MS, NOW))
    bf = Backfiller(fake, CandleStore(db))
    await bf.backfill_daily_full("SPY")
    call = next(c for c in fake.calls if c["ft"] == "daily")
    assert call["period_type"] == "year"  # required; explicit dates alone 400
    assert call["period"] is None
    assert call["start"] <= DAILY_HISTORY_START_MS
    assert call["end"] >= NOW - DAY_MS


async def test_minute_backfill_exact_multiple_no_degenerate_chunk(db):
    """Range = exact multiple of the chunk size must not emit a start==end call
    (Schwab 400s on it)."""
    fake = FakeSchwab((NOW - 30 * DAY_MS, NOW))
    bf = Backfiller(fake, CandleStore(db))
    await bf.backfill_minute_range("SPY", NOW - 20 * DAY_MS, NOW)
    for c in [c for c in fake.calls if c["ft"] == "minute"]:
        assert c["end"] > c["start"]


async def test_intraday_backfill_uses_right_frequency_and_chunks(db):
    fake = FakeSchwab((NOW - 300 * DAY_MS, NOW))
    bf = Backfiller(fake, CandleStore(db))
    await bf.backfill_intraday_range("SPY", "30m", NOW - 180 * DAY_MS, NOW)
    calls = [c for c in fake.calls if c["ft"] == "minute"]
    assert calls, "expected minute-frequency requests"
    assert all(c["freq"] == 30 for c in calls)
    assert len(calls) == 2  # 180 days -> ceil(180/90)
    for c in calls:
        assert c["end"] > c["start"]  # never a degenerate start==end call
        assert c["end"] - c["start"] < INTRADAY_CHUNK_DAYS * DAY_MS


async def test_intraday_backfill_rejects_non_schwab_tf(db):
    bf = Backfiller(FakeSchwab((0, 0)), CandleStore(db))
    with pytest.raises(ValueError):
        await bf.backfill_intraday_range("SPY", "1h", 0, 1)


async def test_intraday_reaches_further_back_than_1m(db):
    """The reason this exists: 30m history outruns the 1m lookback wall."""
    store = CandleStore(db)
    # 1m available only for the last 45 days; 30m for the last 250
    fake = TieredFakeSchwab(minute_days=45, intraday_days=250)
    bf = Backfiller(fake, store)
    await bf.backfill_minute_max("SPY")
    await bf.backfill_intraday_max("SPY", "30m")
    cov_1m = await store.coverage("SPY", "1m")
    cov_30m = await store.coverage("SPY", "30m")
    assert cov_1m and cov_30m
    assert cov_30m[0] < cov_1m[0] - 100 * DAY_MS


async def test_ensure_symbol_bootstraps_all_native_tfs(db):
    fake = FakeSchwab((NOW - 30 * DAY_MS, NOW))
    store = CandleStore(db)
    bf = Backfiller(fake, store)
    await bf.ensure_symbol("QQQ")
    assert (await store.coverage("QQQ", "1d"))[2] == 100
    assert (await store.coverage("QQQ", "1m"))[2] > 0
    for tf in SCHWAB_INTRADAY_TFS:
        assert (await store.coverage("QQQ", tf))[2] > 0, f"{tf} not bootstrapped"
    # second call is a no-op (no new REST calls)
    before = len(fake.calls)
    await bf.ensure_symbol("QQQ")
    assert len(fake.calls) == before


# ---- service --------------------------------------------------------------

async def test_service_get_candles_autobackfills(db):
    fake = FakeSchwab((NOW - 30 * DAY_MS, NOW))
    store = CandleStore(db)
    svc = MarketDataService(store, Backfiller(fake, store))
    df = await svc.get_candles("IWM", "5m", NOW - 5 * DAY_MS, NOW)
    assert not df.empty
    assert len(fake.calls) > 0  # backfill happened


async def test_quote_cache_staleness(db):
    fake = FakeSchwab((0, 0))
    store = CandleStore(db)
    svc = MarketDataService(store, Backfiller(fake, store))
    svc.cache_quote("SPY", {"bid": 1, "ask": 2, "last": 1.5})
    assert svc.latest_cached_quote("SPY") is not None
    assert svc.latest_cached_quote("SPY", max_age_s=-1) is None
