"""Audit A5 — `session.entry_window` / `session.flatten_eod` in the LIVE runner.

Why this file exists: both fields were schema-validated and honoured by the
backtest engine, but the runtime ignored them. 52 of the user's strategies set a
window and 44 set `flatten_eod: true` (including every active run), so an
intraday strategy was holding positions OVERNIGHT in paper/live and taking gap
risk its backtest never modelled, and was entering at times its backtest
refused. Every property pinned here is a parity property: the assertions are
written against the backtest engine's semantics (`in_entry_window`,
`is_last_bar_of_day`), not against whatever the runner happens to do.

Timestamps are anchored to REAL ET wall-clock dates — including one pair of
tests that use the same UTC hour in January and July — because the whole point
of converting through `America/New_York` instead of a fixed offset is that DST
must not shift a window by an hour.

Reuses test_runner.py's harness (real Database + OrderGateway + SimBroker,
fake aggregator/MDS) so the session gates are exercised through the same wiring
as every other runner behaviour.
"""

from __future__ import annotations

import copy
import json

import numpy as np
import pandas as pd
import pytest

from app.broker.sim import SimBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.runtime.runner import StrategyRunner
from app.strategy.schema import StrategyDocument
from tests.test_runner import MIN_MS, FakeAggregator, QuoteBook, bar_event, settle
from tests.test_strategy import EXAMPLE

ALWAYS = {"op": "lt", "left": {"ind": "rsi14"}, "right": {"value": 200}}
NEVER = {"op": "gt", "left": {"ind": "rsi14"}, "right": {"value": 200}}


def et_ms(y: int, m: int, d: int, hh: int, mm: int) -> int:
    """Epoch ms for an ET wall-clock instant (DST handled by the zone itself)."""
    return int(pd.Timestamp(year=y, month=m, day=d, hour=hh, minute=mm,
                            tz="America/New_York").timestamp() * 1000)


def session_doc(*, entry_window=None, flatten_eod=False, bar="1m", short=False,
                **overrides) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    data["timeframe"] = {"bar": bar}
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    data["entry"] = ({"short": {"when": ALWAYS}, "long": {"when": NEVER}} if short
                     else {"long": {"when": ALWAYS}})
    data["exit"] = {"stop_loss": {"type": "percent", "value": 2.0},
                    "take_profit": {"type": "percent", "value": 5.0}}
    data["sizing"] = {"type": "percent_of_equity", "value": 10}
    data["risk"] = {"max_open_positions": 1, "max_daily_orders": 50, "max_daily_loss": 100000}
    if short:
        data["risk"]["short"] = {"enabled": True, "max_short_notional": 25_000}
    session: dict = {"flatten_eod": flatten_eod}
    if entry_window:
        session["entry_window"] = {"start": entry_window[0], "end": entry_window[1]}
    data["session"] = session
    data.update(overrides)
    return StrategyDocument.model_validate(data)


def warmup_at(end_ms: int, n: int = 10, px: float = 100.0) -> pd.DataFrame:
    """n flat 1m bars ending on the bar BEFORE `end_ms`, so the next pushed bar
    lands exactly on the ET wall-clock instant a test cares about."""
    base = end_ms - n * MIN_MS
    idx = pd.to_datetime([base + i * MIN_MS for i in range(n)], unit="ms", utc=True)
    closes = np.full(n, px)
    return pd.DataFrame({"open": closes, "high": closes + 0.1, "low": closes - 0.1,
                         "close": closes, "volume": np.full(n, 100)}, index=idx)


class FixedMDS:
    """Like test_runner.FakeMDS but WITHOUT range filtering: these tests anchor
    their history to a chosen ET date, which is not the window the runner asks
    for relative to `time.time()`."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames

    async def get_candles(self, symbol, tf, start_ms, end_ms):
        return self.frames.get(symbol, pd.DataFrame())


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    await d.execute(
        "INSERT INTO runs (id, strategy_id, strategy_version_hash, mode, state) "
        "VALUES ('run-1','strat-1','hash','paper','STOPPED')")
    yield d
    await d.close()


@pytest.fixture
def book():
    return QuoteBook()


@pytest.fixture
def broker(book):
    return SimBroker(book.get, starting_cash=100_000, sim_latency_ms=0, sim_slippage_bps=0)


@pytest.fixture
def gateway(db):
    return OrderGateway(db, orders_per_minute=6000)


async def start_at(db, broker, gateway, doc, book, first_bar_ms: int, px: float = 100.0):
    """Start a runner whose candle history stops immediately before
    `first_bar_ms`, so the first pushed bar is at that exact ET time."""
    mds = FixedMDS({"SPY": warmup_at(first_bar_ms, px=px)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    await runner.start()
    book.set("SPY", bid=px - 0.1, ask=px + 0.1)
    return runner, agg


async def push_at(agg, ts_ms: int, px: float = 100.0):
    await agg.push("SPY", "1m", bar_event("SPY", ts_ms, px, px + 0.5, px - 0.5, px, 1000))
    await settle()


async def exit_reasons(db) -> list[str]:
    rows = await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind='order' ORDER BY id")
    return [json.loads(r["payload_json"])["exit_reason"] for r in rows
            if "exit_reason" in json.loads(r["payload_json"])]


# ---------------------------------------------------------- entry_window

async def test_entry_outside_window_is_blocked(db, broker, gateway, book):
    """09:31 ET with a 10:00-15:00 window: the rule fires, the window does not."""
    ts = et_ms(2026, 7, 15, 9, 31)
    doc = session_doc(entry_window=("10:00", "15:00"))
    runner, agg = await start_at(db, broker, gateway, doc, book, ts)
    await push_at(agg, ts)

    assert runner._positions == {}, "entered outside session.entry_window"
    assert runner._pending_entries == set()
    await runner.stop()


async def test_entry_inside_window_is_allowed(db, broker, gateway, book):
    """Same document, same rule, one hour later — proves the block above came
    from the window and not from a broken entry path."""
    ts = et_ms(2026, 7, 15, 10, 30)
    doc = session_doc(entry_window=("10:00", "15:00"))
    runner, agg = await start_at(db, broker, gateway, doc, book, ts)
    await push_at(agg, ts)

    assert "SPY" in runner._positions
    await runner.stop()


async def test_window_edges_are_inclusive(db, broker, gateway, book):
    """The engine compares `start <= hhmm <= end`; the boundary minute trades."""
    ts = et_ms(2026, 7, 15, 15, 0)
    doc = session_doc(entry_window=("10:00", "15:00"))
    runner, agg = await start_at(db, broker, gateway, doc, book, ts)
    await push_at(agg, ts)

    assert "SPY" in runner._positions, "end of the window must be inclusive"
    await runner.stop()


@pytest.mark.parametrize("month,day,expect_entry", [(1, 15, False), (7, 15, True)])
async def test_window_follows_dst_not_a_fixed_offset(db, broker, gateway, book,
                                                     month, day, expect_entry):
    """THE DST regression. 14:30 UTC is 09:30 ET in January (EST, UTC-5) and
    10:30 ET in July (EDT, UTC-4). With a 10:00-15:00 window the same UTC
    instant must be REFUSED in winter and ACCEPTED in summer — a hardcoded
    offset silently shifts the window by an hour for half the year."""
    ts = int(pd.Timestamp(year=2026, month=month, day=day, hour=14, minute=30,
                          tz="UTC").timestamp() * 1000)
    doc = session_doc(entry_window=("10:00", "15:00"))
    runner, agg = await start_at(db, broker, gateway, doc, book, ts)
    await push_at(agg, ts)

    assert ("SPY" in runner._positions) is expect_entry
    await runner.stop()


async def test_no_window_means_always_open(db, broker, gateway, book):
    ts = et_ms(2026, 7, 15, 9, 31)
    runner, agg = await start_at(db, broker, gateway, session_doc(), book, ts)
    await push_at(agg, ts)

    assert "SPY" in runner._positions
    await runner.stop()


# ---------------------------------------------------------- flatten_eod

async def test_position_is_flattened_at_end_of_day(db, broker, gateway, book):
    """The dangerous gap: an intraday `flatten_eod` strategy was holding
    overnight. The 15:59->16:00 bar must close the position with reason `eod`."""
    open_ts = et_ms(2026, 7, 15, 15, 30)
    doc = session_doc(flatten_eod=True)
    runner, agg = await start_at(db, broker, gateway, doc, book, open_ts)
    await push_at(agg, open_ts)
    assert "SPY" in runner._positions, "precondition: a position to flatten"

    await push_at(agg, et_ms(2026, 7, 15, 15, 59))

    assert runner._positions == {}, "flatten_eod did not close the position"
    assert "eod" in await exit_reasons(db)
    assert await db.fetch_one("SELECT 1 FROM positions WHERE run_id='run-1'") is None
    await runner.stop()


async def test_short_is_flattened_with_buy_to_cover(db, broker, gateway, book):
    open_ts = et_ms(2026, 7, 15, 15, 30)
    doc = session_doc(flatten_eod=True, short=True)
    runner, agg = await start_at(db, broker, gateway, doc, book, open_ts)
    await push_at(agg, open_ts)
    assert runner._positions["SPY"].side == "short"

    await push_at(agg, et_ms(2026, 7, 15, 15, 59))

    assert runner._positions == {}
    row = await db.fetch_one(
        "SELECT side FROM order_intents WHERE run_id='run-1' AND purpose='exit'")
    assert row["side"] == "buy_to_cover", "a short must be covered, not sold"
    assert (await broker.get_positions()).get("SPY", 0) == 0
    await runner.stop()


async def test_flatten_cancels_resting_protective_orders(db, broker, gateway, book):
    open_ts = et_ms(2026, 7, 15, 15, 30)
    doc = session_doc(flatten_eod=True)
    runner, agg = await start_at(db, broker, gateway, doc, book, open_ts)
    await push_at(agg, open_ts)
    pos = runner._positions["SPY"]
    protective = [pos.stop_broker_order_id, pos.target_broker_order_id]
    assert all(protective), "precondition: both protective orders resting"

    await push_at(agg, et_ms(2026, 7, 15, 15, 59))

    working = broker.working_order_ids()
    assert not [oid for oid in protective if oid in working], \
        "an eod flatten left protective orders resting on a closed position"
    await runner.stop()


async def test_no_entry_on_the_final_bar_when_flatten_eod(db, broker, gateway, book):
    """`flatten_eod` also blocks OPENING on the bar it would instantly close —
    same gate the engine applies, and the difference between squaring the book
    and churning a pointless round trip every day."""
    ts = et_ms(2026, 7, 15, 15, 59)
    doc = session_doc(flatten_eod=True)
    runner, agg = await start_at(db, broker, gateway, doc, book, ts)
    await push_at(agg, ts)

    assert runner._positions == {} and runner._pending_entries == set()
    await runner.stop()


async def test_final_bar_entry_is_allowed_without_flatten_eod(db, broker, gateway, book):
    """Isolates the block above to `flatten_eod` — the last bar of the day is
    not otherwise special."""
    ts = et_ms(2026, 7, 15, 15, 59)
    runner, agg = await start_at(db, broker, gateway, session_doc(), book, ts)
    await push_at(agg, ts)

    assert "SPY" in runner._positions
    await runner.stop()


async def test_overnight_survivor_is_swept_on_the_next_session(db, broker, gateway, book):
    """The documented divergence from the engine: with no market calendar the
    runner cannot recognise a 13:00 early close, so a position can survive the
    session. It is then swept on the first bar of the NEXT ET day with reason
    `eod_late` — one overnight hold, loudly logged, not an unbounded one."""
    open_ts = et_ms(2026, 11, 27, 12, 30)      # day after Thanksgiving, 13:00 close
    doc = session_doc(flatten_eod=True)
    runner, agg = await start_at(db, broker, gateway, doc, book, open_ts)
    await push_at(agg, open_ts)
    assert "SPY" in runner._positions

    await push_at(agg, et_ms(2026, 11, 27, 12, 59))
    assert "SPY" in runner._positions, "no 16:00 bar exists on a half day"

    await push_at(agg, et_ms(2026, 11, 30, 9, 31))

    assert runner._positions == {}
    assert "eod_late" in await exit_reasons(db)
    await runner.stop()


# ---------------------------------------------------------- daily bars

def test_daily_documents_ignore_both_session_gates(db, broker):
    """Lint tells the user a window has no effect on daily bars and rejects
    `flatten_eod` on them outright, so the runtime must not enforce either —
    a 1d runner would otherwise refuse every entry whose bar opens at midnight."""
    doc = session_doc(entry_window=("10:00", "15:00"), bar="1d")
    runner = StrategyRunner("run-1", doc, "paper", broker, None, None, None, db)
    ts = et_ms(2026, 7, 15, 0, 0)

    assert runner._intraday is False
    assert runner._in_entry_window(ts) is True
    assert runner._is_last_bar_of_day(ts) is False
    assert runner._flatten_eod() is False


def test_last_bar_of_day_matches_the_engines_choice(db, broker):
    """Unit-level parity: for a 15m document the engine's next-row-date rule
    selects the 15:45->16:00 bar. The clock rule must select the same one and
    no earlier bar."""
    doc = session_doc(flatten_eod=True, bar="15m")
    runner = StrategyRunner("run-1", doc, "paper", broker, None, None, None, db)

    assert runner._is_last_bar_of_day(et_ms(2026, 7, 15, 15, 45)) is True
    assert runner._is_last_bar_of_day(et_ms(2026, 7, 15, 15, 30)) is False
    assert runner._is_last_bar_of_day(et_ms(2026, 7, 15, 9, 30)) is False
    # same wall-clock bar in winter — the ET conversion, not a fixed offset
    assert runner._is_last_bar_of_day(et_ms(2026, 1, 15, 15, 45)) is True
