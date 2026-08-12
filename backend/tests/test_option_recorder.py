"""ChainRecorder: daily snapshots and the LEVELONE_OPTIONS handler.

The stream handler gets particular attention because Schwab's published
CHART_EQUITY field map turned out to be WRONG on the wire (see
streamer/recorder.py's field-map comment) — so the options map is verified
against the symbol itself rather than trusted, and that guard is tested."""

from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from app.db import Database
from app.options.chain_service import ChainService
from app.options.store import OptionChainStore
from app.options.symbology import format_occ, parse_occ
from app.streamer.option_recorder import (F_ASK, F_BID, F_DELTA, F_IV, F_LAST,
                                          F_MARK,
                                          F_MULTIPLIER, F_QUOTE_TIME, F_STRIKE,
                                          F_UNDERLYING_PX, ChainRecorder,
                                          select_tick_universe, snapshot_times)

SYM = "SPY   260918C00680000"


def now_ms() -> int:
    """Computed per call, never captured at import.

    `latest_quote` deliberately rejects quotes older than a few seconds, so a
    module-level timestamp makes these tests pass in isolation and fail inside
    the full suite (which takes minutes to reach them) — a genuinely confusing
    flake. The freshness check is the behaviour under test, so the fix is a
    live clock, not a widened tolerance."""
    return int(time.time() * 1000)


NOW = now_ms()   # for chain payloads, where staleness is asserted explicitly


def _contract(symbol=SYM, *, put_call="CALL", strike=680.0, bid=6.40, ask=6.50,
              delta=0.35, quote_ts=None):
    return {
        # `bid`/`ask`/`last` — the real /chains wire names (the /quotes
        # endpoint is the one that says bidPrice/askPrice).
        "putCall": put_call, "symbol": symbol, "bid": bid, "ask": ask,
        "last": 6.45, "mark": (bid + ask) / 2, "bidSize": 40, "askSize": 55,
        "totalVolume": 2400, "openInterest": 15800, "volatility": 18.42,
        "delta": delta, "gamma": 0.012, "theta": -0.085, "vega": 0.61, "rho": 0.21,
        "strikePrice": strike, "daysToExpiration": 50, "multiplier": 100.0,
        "quoteTimeInLong": quote_ts if quote_ts is not None else NOW - 5_000,
    }


class FakeSchwab:
    """Records calls so the request-bounding behaviour can be asserted."""

    def __init__(self, chain=None, expirations=None, fail=False):
        self.chain = chain
        self.expirations = expirations or {"expirationList": [
            {"expiration": "2026-09-18", "expirationType": "Q",
             "settlementType": "P", "standard": True}]}
        self.fail = fail
        self.chain_calls: list[dict] = []
        self.expiration_calls: list[str] = []
        #: How old the returned quotes are. Drives the staleness tests: what
        #: counts as stale is the whole difference between a daily and an
        #: intraday snapshot.
        self.quote_age_ms = 5_000

    async def get_option_chain(self, symbol, **kw):
        self.chain_calls.append({"symbol": symbol, **kw})
        if self.fail:
            raise RuntimeError("schwab is down")
        qt = int(time.time() * 1000) - self.quote_age_ms
        return self.chain or {
            "symbol": symbol, "status": "SUCCESS", "underlyingPrice": 672.30,
            "callExpDateMap": {"2026-09-18:50": {"680.0": [
                _contract(quote_ts=qt)]}},
            "putExpDateMap": {"2026-09-18:50": {"660.0": [
                _contract("SPY   260918P00660000", put_call="PUT", strike=660.0,
                          delta=-0.30, quote_ts=qt)]}},
        }

    async def get_expiration_chain(self, symbol):
        self.expiration_calls.append(symbol)
        if self.fail:
            raise RuntimeError("schwab is down")
        return self.expirations


class FakeStreamer:
    def __init__(self):
        self.handlers = {}
        self.subscribed: list[tuple[str, list[str]]] = []
        self.unsubscribed: list[tuple[str, list[str]]] = []

    def on_data(self, service, handler):
        self.handlers[service] = handler

    async def subscribe(self, service, symbols):
        self.subscribed.append((service, list(symbols)))

    async def unsubscribe(self, service, symbols):
        self.unsubscribed.append((service, list(symbols)))


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def make_recorder(db):
    def _make(schwab=None, streamer=None, calendar=None, **kw):
        schwab = schwab or FakeSchwab()
        store = OptionChainStore(db)
        svc = ChainService(schwab, store)
        rec = ChainRecorder(db, store, svc, streamer or FakeStreamer(),
                            calendar=calendar, **kw)
        return rec, store, schwab
    return _make


# ---- watchlist ------------------------------------------------------------

async def test_watchlist_round_trip_and_normalisation(make_recorder):
    rec, _, _ = make_recorder()
    assert await rec.get_watchlist() == []
    saved = await rec.set_watchlist([" spy ", "qqq", "SPY", ""])
    assert saved == ["QQQ", "SPY"]          # deduped, uppercased, sorted
    assert await rec.get_watchlist() == ["QQQ", "SPY"]


async def test_watchlist_is_separate_from_the_equity_one(db, make_recorder):
    """Recording SPY 1-minute bars and recording SPY's option chain are
    different decisions with very different costs."""
    rec, _, _ = make_recorder()
    await db.state_set("recording_watchlist", ["AAPL"])
    await rec.set_watchlist(["SPY"])
    assert await db.state_get("recording_watchlist") == ["AAPL"]
    assert await rec.get_watchlist() == ["SPY"]


# ---- snapshots ------------------------------------------------------------

async def test_snapshot_one_writes_rows_and_diagnostics(make_recorder):
    rec, store, _ = make_recorder()
    summary = await rec.snapshot_one("SPY")
    assert summary["written"] == 2 and summary["rejected"] == 0
    assert summary["underlying_px"] == pytest.approx(672.30)

    chain = await store.read_chain("SPY", int(time.time() * 1000))
    assert len(chain) == 2
    runs = await store.recent_runs()
    assert runs[0]["written"] == 2 and runs[0]["underlying"] == "SPY"


async def test_all_rows_in_one_snapshot_share_a_timestamp(make_recorder):
    """Otherwise `read_chain` returns a smear across seconds instead of one
    coherent chain."""
    rec, store, _ = make_recorder()
    await rec.snapshot_one("SPY")
    chain = await store.read_chain("SPY", int(time.time() * 1000))
    assert chain["ts"].nunique() == 1


async def test_snapshot_bounds_the_request(make_recorder):
    """An unfiltered SPY chain is ~5,000 contracts. from/to date plus
    strike_count are what keep a nightly snapshot to a few hundred rows."""
    rec, _, schwab = make_recorder()
    await rec.snapshot_one("SPY")
    call = schwab.chain_calls[0]
    assert call["strike_count"] == 60
    assert call["from_date"] == dt.date.today().isoformat()
    assert call["to_date"] == (dt.date.today() + dt.timedelta(days=180)).isoformat()
    assert call["include_underlying_quote"] is True


async def test_snapshot_failure_is_recorded_not_raised(make_recorder):
    """One bad symbol must never abort the sweep."""
    rec, store, _ = make_recorder(schwab=FakeSchwab(fail=True))
    summary = await rec.snapshot_one("SPY")
    assert summary["written"] == 0 and summary["error"]
    runs = await store.recent_runs()
    assert runs[0]["error"]


async def test_snapshot_is_idempotent(make_recorder):
    rec, store, _ = make_recorder()
    ts = int(time.time() * 1000)
    await rec.snapshot_one("SPY", ts=ts)
    await rec.snapshot_one("SPY", ts=ts)
    chain = await store.read_chain("SPY", ts)
    assert len(chain) == 2


async def test_snapshot_all_walks_the_watchlist_and_refreshes_expirations(make_recorder):
    rec, store, schwab = make_recorder()
    await rec.set_watchlist(["SPY", "QQQ"])
    results = await rec.snapshot_all()
    assert [r["underlying"] for r in results] == ["QQQ", "SPY"]
    assert set(schwab.expiration_calls) == {"SPY", "QQQ"}
    exps = await store.read_expirations("SPY", 0)
    assert [e.expiry for e in exps] == [dt.date(2026, 9, 18)]


async def test_snapshot_all_is_a_noop_with_an_empty_watchlist(make_recorder):
    """The default state. The whole options stack must cost nothing until the
    user opts in."""
    rec, _, schwab = make_recorder()
    assert await rec.snapshot_all() == []
    assert schwab.chain_calls == []


async def test_scheduled_snapshot_skips_weekends(make_recorder, monkeypatch):
    """A snapshot of a closed market is full of stale quotes that would poison
    every calibration reading them."""
    import app.streamer.option_recorder as mod
    rec, _, schwab = make_recorder()
    await rec.set_watchlist(["SPY"])

    class FakeDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 8, 1, 15, 50, tzinfo=tz)   # a Saturday

    monkeypatch.setattr(mod.dt, "datetime", FakeDT)
    assert await rec.scheduled_snapshot() == []
    assert schwab.chain_calls == []


async def test_scheduled_snapshot_never_raises(make_recorder):
    rec, _, _ = make_recorder(schwab=FakeSchwab(fail=True))
    await rec.set_watchlist(["SPY"])
    assert await rec.scheduled_snapshot() is not None


# ---- LEVELONE_OPTIONS -----------------------------------------------------

def _tick(**fields):
    base = {"key": SYM, F_BID: 6.40, F_ASK: 6.50, F_MARK: 6.45, F_IV: 18.42,
            F_DELTA: 0.35, F_STRIKE: 680.0, F_MULTIPLIER: 100.0,
            F_UNDERLYING_PX: 672.30, F_QUOTE_TIME: now_ms()}
    base.update(fields)
    return base


async def test_levelone_handler_registers_and_parses(make_recorder):
    streamer = FakeStreamer()
    rec, _, _ = make_recorder(streamer=streamer)
    assert "LEVELONE_OPTIONS" in streamer.handlers

    await streamer.handlers["LEVELONE_OPTIONS"]([_tick()])
    q = rec.latest_quote(SYM)
    assert q is not None
    assert q.bid == pytest.approx(6.40) and q.ask == pytest.approx(6.50)
    assert q.iv == pytest.approx(0.1842)     # percent -> decimal
    assert q.greeks.delta == pytest.approx(0.35)
    assert q.source == "live"


async def test_levelone_is_a_change_service_and_must_merge(make_recorder):
    """Only changed fields are sent. A handler that does not merge sees a bid
    with no ask and concludes the market is broken."""
    streamer = FakeStreamer()
    rec, _, _ = make_recorder(streamer=streamer)
    h = streamer.handlers["LEVELONE_OPTIONS"]
    await h([_tick()])
    await h([{"key": SYM, F_BID: 6.45, F_QUOTE_TIME: now_ms()}])   # bid only
    q = rec.latest_quote(SYM)
    assert q is not None
    assert q.bid == pytest.approx(6.45)
    assert q.ask == pytest.approx(6.50)      # retained from the earlier tick


async def test_merged_tick_that_crosses_the_market_is_rejected(make_recorder):
    """Merging must not manufacture a valid-looking quote: if the new bid
    lands above the retained ask, that is a crossed market and unusable."""
    streamer = FakeStreamer()
    rec, _, _ = make_recorder(streamer=streamer)
    h = streamer.handlers["LEVELONE_OPTIONS"]
    await h([_tick()])
    await h([{"key": SYM, F_BID: 6.60, F_QUOTE_TIME: now_ms()}])   # above ask 6.50
    assert rec.latest_quote(SYM) is None


async def test_levelone_field_map_mismatch_is_caught(make_recorder):
    """The cheap guard that would have caught the CHART_EQUITY field shift
    immediately: if the map is off by one, the 'strike' field will not match
    the strike encoded in the symbol we subscribed to."""
    streamer = FakeStreamer()
    rec, _, _ = make_recorder(streamer=streamer)
    await streamer.handlers["LEVELONE_OPTIONS"]([_tick(**{F_STRIKE: 655.0})])
    assert rec.latest_quote(SYM) is None


@pytest.mark.parametrize("bad", [
    {F_BID: 7.0, F_ASK: 6.0},      # crossed
    {F_BID: 0.0, F_ASK: 0.0},      # no market
    {F_ASK: -1.0},
])
async def test_levelone_unusable_quotes_return_none(make_recorder, bad):
    streamer = FakeStreamer()
    rec, _, _ = make_recorder(streamer=streamer)
    await streamer.handlers["LEVELONE_OPTIONS"]([_tick(**bad)])
    assert rec.latest_quote(SYM) is None


async def test_levelone_stale_quote_returns_none(make_recorder):
    """None is a MEANINGFUL answer: the runner blocks the entry rather than
    falling back to a modelled price."""
    streamer = FakeStreamer()
    rec, _, _ = make_recorder(streamer=streamer)
    old = now_ms() - 120_000
    await streamer.handlers["LEVELONE_OPTIONS"]([_tick(**{F_QUOTE_TIME: old})])
    assert rec.latest_quote(SYM, max_age_s=15) is None
    assert rec.latest_quote(SYM, max_age_s=0) is not None   # age check disabled


async def test_levelone_raw_capture(db, make_recorder):
    """Raw payloads are persisted before the field map is trusted — the same
    diagnostic that finally pinned down the CHART_EQUITY bug."""
    streamer = FakeStreamer()
    rec, _, _ = make_recorder(streamer=streamer)
    await streamer.handlers["LEVELONE_OPTIONS"]([_tick()])
    rows = await db.fetch_all("SELECT symbol, raw_json FROM option_levelone_raw")
    assert len(rows) == 1
    assert rows[0]["symbol"] == SYM
    assert json.loads(rows[0]["raw_json"])[F_BID] == 6.40


async def test_subscribe_and_unsubscribe_are_idempotent(make_recorder):
    streamer = FakeStreamer()
    rec, _, _ = make_recorder(streamer=streamer)
    await rec.subscribe([SYM, SYM, "SPY"])          # equity symbol ignored
    assert streamer.subscribed == [("LEVELONE_OPTIONS", [SYM])]
    await rec.subscribe([SYM])                      # already subscribed
    assert len(streamer.subscribed) == 1
    await rec.unsubscribe([SYM])
    assert streamer.unsubscribed == [("LEVELONE_OPTIONS", [SYM])]
    assert rec.latest_quote(SYM) is None             # cached quote dropped


async def test_recorder_works_without_a_streamer(db):
    """Backtest-only deployments and tests must not need a streamer."""
    store = OptionChainStore(db)
    rec = ChainRecorder(db, store, ChainService(FakeSchwab(), store), None)
    await rec.subscribe([SYM])       # no-op, no crash
    await rec.unsubscribe([SYM])
    assert (await rec.snapshot_one("SPY"))["written"] == 2


# ---- intraday schedule ----------------------------------------------------
#
# The cadence is the whole ballgame for intraday backtests: an option chain
# cannot be backfilled from any source, so the interval running today is a
# permanent ceiling on the time resolution of every backtest that will ever be
# run against this data.

def test_snapshot_times_default_cadence():
    slots = snapshot_times(30, "09:45", "15:50")
    assert slots[0] == (9, 45)
    assert slots[-1] == (15, 50)
    assert len(slots) == 13
    # strictly increasing, no duplicates
    assert slots == sorted(set(slots))


def test_zero_interval_means_close_only():
    """The pre-existing behaviour, and what makes the new default safe to ship:
    setting the interval to 0 restores exactly one 15:50 snapshot."""
    assert snapshot_times(0) == [(15, 50)]


def test_close_anchor_is_always_included():
    """15:50 is the slot every historical snapshot shares. A cadence that
    skipped it would break continuity with every day already recorded."""
    for interval in (7, 13, 30, 45, 60, 90, 200):
        assert snapshot_times(interval, "09:45", "15:50")[-1] == (15, 50)


def test_no_slot_crowds_the_close_anchor():
    """A 30m grid ends at 15:45; recording that AND 15:50 spends a whole extra
    sweep to observe five minutes of the same market."""
    slots = snapshot_times(30, "09:45", "15:50")
    assert (15, 45) not in slots


def test_snapshot_times_rejects_malformed_input():
    for bad in ("9h45", "", "25:00", "09:99"):
        with pytest.raises(ValueError):
            snapshot_times(30, bad)


def test_schedule_et_reports_the_configured_slots(make_recorder):
    rec, _, _ = make_recorder(snapshot_slots=snapshot_times(60, "09:45", "15:50"))
    assert rec.schedule_et()[0] == "09:45"
    assert rec.schedule_et()[-1] == "15:50"


# ---- market-open guard ----------------------------------------------------

class FakeCalendar:
    def __init__(self, open_now=True, raises=False):
        self._open, self._raises = open_now, raises
        self.checked = 0

    async def is_open_now(self):
        self.checked += 1
        if self._raises:
            raise RuntimeError("calendar unavailable")
        return self._open


async def test_scheduled_snapshot_skips_market_holidays(make_recorder, monkeypatch):
    """At 13 fires a day a holiday is 13 junk snapshots, all of them quotes
    frozen at the previous close — which reads downstream as a full session of
    exactly zero volatility."""
    import app.streamer.option_recorder as mod
    cal = FakeCalendar(open_now=False)
    rec, _, schwab = make_recorder(calendar=cal)
    await rec.set_watchlist(["SPY"])

    class FakeDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 7, 3, 12, 15, tzinfo=tz)   # a Friday holiday

    monkeypatch.setattr(mod.dt, "datetime", FakeDT)
    assert await rec.scheduled_snapshot() == []
    assert schwab.chain_calls == []
    assert cal.checked == 1


async def test_calendar_outage_does_not_disable_recording(make_recorder, monkeypatch):
    """A missed snapshot is unrecoverable; a junk one is at least detectable
    afterwards from its quote_ts. So a calendar failure must fail OPEN."""
    import app.streamer.option_recorder as mod
    rec, _, schwab = make_recorder(calendar=FakeCalendar(raises=True))
    await rec.set_watchlist(["SPY"])

    class FakeDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 8, 3, 12, 15, tzinfo=tz)   # a Monday

    monkeypatch.setattr(mod.dt, "datetime", FakeDT)
    assert await rec.scheduled_snapshot() != []
    assert schwab.chain_calls


# ---- intraday staleness ---------------------------------------------------

async def test_intraday_snapshot_uses_a_tight_staleness_bound(make_recorder):
    """At a 30-minute cadence, a 30-minute-old quote is just the previous
    snapshot wearing a new timestamp. Letting it through would manufacture
    intraday movement that never happened."""
    rec, store, _ = make_recorder(intraday_stale_quote_s=300)
    ts = now_ms()
    summary = await rec.snapshot_one("SPY", ts=ts)
    assert summary["written"] == 2      # the fixture's quotes are 5s old

    stale = FakeSchwab()
    stale.quote_age_ms = 10 * 60_000    # 10 minutes: fine at 30m, stale at 5m
    rec2, _, _ = make_recorder(schwab=stale, intraday_stale_quote_s=300)
    summary2 = await rec2.snapshot_one("SPY", ts=now_ms())
    assert summary2["written"] == 0
    assert summary2["reject_reasons"].get("stale_quote") == 2


async def test_intraday_coverage_reports_snapshots_per_day(make_recorder):
    """The number that decides whether an intraday backtest is defensible: a
    day showing 1 snapshot is a day the pricer extrapolated through."""
    rec, store, _ = make_recorder()
    base = now_ms()
    for i in range(3):
        await rec.snapshot_one("SPY", ts=base + i * 1_800_000)
    by_day = await store.intraday_coverage("SPY", days=2)
    assert by_day and by_day[0]["snapshots"] == 3


# ---- streamed tick recording ----------------------------------------------
#
# The point of this path: a 30-minute snapshot is a POINT SAMPLE, so a contract
# that goes 6.40 -> 9.80 -> 6.50 between two slots records as a flat 6.40 ->
# 6.50. The move is not just missing, it is invisible — the series looks calm,
# and every stop that would have triggered intrabar never triggers.

def _rows_for_universe():
    """Chain rows spanning 6 expiries x 41 strikes, both rights."""
    from app.options.store import SnapshotRow
    rows = []
    for e in range(6):
        expiry = 1_800_000_000_000 + e * 7 * 86_400_000
        for k in range(660, 701):     # 41 strikes, $1 apart, spot 680
            for right in ("C", "P"):
                rows.append(SnapshotRow(
                    underlying="SPY", ts=NOW,
                    symbol=format_occ("SPY", dt.date(2026, 9, 18 - 0), right, k)
                    .replace("260918", f"2609{10 + e:02d}"),
                    expiry=expiry, strike=float(k), opt_right=right,
                    bid=1.0, ask=1.1, last=1.05, mark=1.05,
                    bid_size=1, ask_size=1, volume=1, open_interest=1,
                    iv=0.2, delta=0.5, gamma=0.0, theta=0.0, vega=0.0, rho=0.0,
                    underlying_px=680.0, dte=30, multiplier=100.0,
                    is_non_standard=0, quote_ts=NOW))
    return rows


def test_universe_is_atm_band_on_front_expiries():
    """10 strikes either side = 21 strikes, both rights, front 4 expiries."""
    picked = select_tick_universe(_rows_for_universe(), 680.0,
                                  strikes=10, expiries=4)
    assert len(picked) == 21 * 2 * 4
    strikes = {parse_occ(s).strike for s in picked}
    assert min(strikes) == 670 and max(strikes) == 690
    # only the four nearest expiries
    assert len({parse_occ(s).expiry for s in picked}) == 4


def test_universe_is_ordered_nearest_the_money_first():
    """Load-bearing: the caller truncates this list against a global symbol
    cap, so what survives truncation must be what a strategy would trade."""
    picked = select_tick_universe(_rows_for_universe(), 680.0,
                                  strikes=10, expiries=4)
    first_strikes = [float(parse_occ(s).strike) for s in picked[:2]]
    assert all(abs(k - 680.0) < 1.0 for k in first_strikes)
    dists = [abs(float(parse_occ(s).strike) - 680.0) for s in picked]
    assert dists[0] <= dists[-1]


def test_universe_band_follows_spot():
    """The band is relative to spot; a universe pinned at the open would be
    recording wings by the afternoon."""
    picked = select_tick_universe(_rows_for_universe(), 695.0,
                                  strikes=5, expiries=1)
    strikes = {float(parse_occ(s).strike) for s in picked}
    assert 695.0 in strikes
    assert 660.0 not in strikes


def test_universe_is_empty_without_a_spot_price():
    """No spot means no defensible centre for the band — recording an arbitrary
    slice would be worse than recording nothing."""
    assert select_tick_universe(_rows_for_universe(), None) == []
    assert select_tick_universe([], 680.0) == []


async def test_universe_refresh_subscribes_and_diffs(make_recorder):
    rec, _, _ = make_recorder(tick_recording=True, tick_strikes=2, tick_expiries=1)
    streamer = rec._streamer
    await rec.set_tick_universe(["SPY   260918C00680000", "SPY   260918C00681000"])
    assert len(streamer.subscribed[-1][1]) == 2

    # A refresh that shifts the band by one strike must change one subscription,
    # not churn the whole set.
    out = await rec.set_tick_universe(["SPY   260918C00681000",
                                       "SPY   260918C00682000"])
    assert out == {"universe": 2, "added": 1, "removed": 1}
    assert streamer.unsubscribed[-1][1] == ["SPY   260918C00680000"]
    assert streamer.subscribed[-1][1] == ["SPY   260918C00682000"]


async def test_universe_refresh_never_unsubscribes_a_live_position(make_recorder):
    """A universe refresh that dropped the contract a live stop is watching
    would blind the stop. Position subscriptions outrank recording ones."""
    rec, _, _ = make_recorder(tick_recording=True)
    streamer = rec._streamer
    await rec.set_tick_universe([SYM])
    await rec.subscribe([SYM])              # a run opens a position on it
    await rec.set_tick_universe([])         # band moves away
    assert all(SYM not in syms for _svc, syms in streamer.unsubscribed)


async def test_position_unsubscribe_keeps_it_if_still_in_the_universe(make_recorder):
    rec, _, _ = make_recorder(tick_recording=True)
    streamer = rec._streamer
    await rec.set_tick_universe([SYM])
    await rec.subscribe([SYM])
    await rec.unsubscribe([SYM])            # position closed
    assert all(SYM not in syms for _svc, syms in streamer.unsubscribed)
    assert SYM in rec._universe


async def test_ticks_are_persisted_with_greeks(make_recorder):
    rec, store, _ = make_recorder(tick_recording=True, tick_min_interval_ms=0)
    streamer = rec._streamer
    await streamer.handlers["LEVELONE_OPTIONS"]([_tick()])
    assert await rec.flush_ticks() == 1

    df = await store.read_ticks(SYM, 0, now_ms() + 10_000)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["bid"] == pytest.approx(6.40)
    assert row["ask"] == pytest.approx(6.50)
    assert row["underlying"] == "SPY"       # canonical, not the OCC root
    assert row["delta"] == pytest.approx(0.35)
    assert row["iv"] == pytest.approx(0.1842)   # percent -> decimal


async def test_unchanged_quotes_are_not_written(make_recorder):
    """A repeated bid/ask carries no information at any interval."""
    rec, _, _ = make_recorder(tick_recording=True, tick_min_interval_ms=0)
    streamer = rec._streamer
    for _ in range(5):
        await streamer.handlers["LEVELONE_OPTIONS"]([_tick()])
    assert await rec.flush_ticks() == 1
    assert rec.ticks_dropped_unchanged == 4


async def test_conflation_bounds_a_fast_moving_contract(make_recorder):
    """The one filter that DOES lose real information, so it is counted
    separately and surfaced rather than folded into a single 'dropped' number."""
    rec, _, _ = make_recorder(tick_recording=True, tick_min_interval_ms=1000)
    streamer = rec._streamer
    t = now_ms()
    for i in range(5):                       # five genuine moves, 100ms apart
        await streamer.handlers["LEVELONE_OPTIONS"](
            [_tick(**{F_BID: 6.40 + i * 0.05, F_ASK: 6.50 + i * 0.05,
                      F_QUOTE_TIME: t + i * 100})])
    assert await rec.flush_ticks() == 1
    assert rec.ticks_dropped_conflated == 4
    assert rec.ticks_dropped_unchanged == 0

    # ... and a move past the interval is kept
    await streamer.handlers["LEVELONE_OPTIONS"](
        [_tick(**{F_BID: 7.0, F_ASK: 7.1, F_QUOTE_TIME: t + 2000})])
    assert await rec.flush_ticks() == 1


async def test_unusable_quotes_are_never_written(make_recorder):
    """Same bar as latest_quote: a crossed or one-sided market is not data."""
    rec, _, _ = make_recorder(tick_recording=True, tick_min_interval_ms=0)
    streamer = rec._streamer
    await streamer.handlers["LEVELONE_OPTIONS"]([_tick(**{F_BID: 7.0, F_ASK: 6.0})])
    await streamer.handlers["LEVELONE_OPTIONS"]([_tick(**{F_ASK: 0.0})])
    assert await rec.flush_ticks() == 0


async def test_tick_recording_off_writes_nothing(make_recorder):
    """Default-off for existing deployments: the stream still marks positions,
    it just does not persist."""
    rec, _, _ = make_recorder(tick_recording=False)
    streamer = rec._streamer
    await streamer.handlers["LEVELONE_OPTIONS"]([_tick()])
    assert await rec.flush_ticks() == 0
    assert rec.latest_quote(SYM) is not None    # marking still works


async def test_handler_never_writes_inline(make_recorder):
    """The websocket read loop is shared with the equity services. A DB write
    inside the handler would back up every one of them."""
    rec, store, _ = make_recorder(tick_recording=True, tick_min_interval_ms=0)
    streamer = rec._streamer
    await streamer.handlers["LEVELONE_OPTIONS"]([_tick()])
    assert (await store.read_ticks(SYM, 0, now_ms() + 10_000)).empty
    await rec.flush_ticks()
    assert not (await store.read_ticks(SYM, 0, now_ms() + 10_000)).empty


async def test_stop_flusher_drains_the_buffer(make_recorder):
    """A restart must not silently discard a second of quotes."""
    rec, store, _ = make_recorder(tick_recording=True, tick_min_interval_ms=0)
    streamer = rec._streamer
    await rec.start_tick_flusher()
    await streamer.handlers["LEVELONE_OPTIONS"]([_tick()])
    await rec.stop_tick_flusher()
    assert len(await store.read_ticks(SYM, 0, now_ms() + 10_000)) == 1


async def test_ticks_prune_by_retention(make_recorder):
    rec, store, _ = make_recorder(tick_recording=True, tick_min_interval_ms=0)
    old = now_ms() - 100 * 86_400_000
    await store.insert_ticks([{"symbol": SYM, "ts": old, "underlying": "SPY",
                               "bid": 1.0, "ask": 1.1}])
    assert await store.prune_ticks(now_ms() - 90 * 86_400_000) == 1
    assert (await store.read_ticks(SYM, 0, now_ms())).empty


async def test_tick_universe_refresh_is_a_noop_when_disabled(make_recorder):
    rec, _, _ = make_recorder(tick_recording=False)
    assert await rec.refresh_tick_universe() == {"skipped": "disabled"}


async def test_tick_universe_refresh_drops_everything_when_closed(make_recorder):
    """Outside the session there is nothing to record, and holding the
    subscription open just spends symbol budget."""
    rec, _, _ = make_recorder(tick_recording=True,
                              calendar=FakeCalendar(open_now=False))
    await rec.set_tick_universe([SYM])
    assert await rec.refresh_tick_universe() == {"skipped": "market_closed"}
    assert rec._universe == set()


def test_levelone_options_fields_are_actually_subscribed():
    """A service missing from SERVICE_FIELDS subscribes with fields="0" — the
    symbol and nothing else — so the feed arrives technically working and
    completely empty. LEVELONE_OPTIONS was in exactly that state: this module
    declared the field list and nothing ever put it on the wire.

    Asserted against the client's map, not against a copy, because a copy is
    what allowed the two to diverge in the first place."""
    from app.streamer.client import SERVICE_FIELDS
    assert "LEVELONE_OPTIONS" in SERVICE_FIELDS
    fields = set(SERVICE_FIELDS["LEVELONE_OPTIONS"].split(","))
    # every field the parse path actually reads must be subscribed
    for f in (F_BID, F_ASK, F_LAST, F_MARK, F_IV, F_STRIKE, F_MULTIPLIER,
              F_DELTA, F_QUOTE_TIME, F_UNDERLYING_PX):
        assert f in fields, f"field {f} is read but never subscribed"
