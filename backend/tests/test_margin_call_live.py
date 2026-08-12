"""Audit A8 — maintenance margin / forced buy-in in the LIVE/paper runner.

The backtest engine force-covers a breaching short (its step 3b, reason
`margin_call`); the runner did not. A short's loss is unbounded, so a strategy
whose backtest showed a forced buy-in was, live, running with nothing but its
`exit.stop_loss` between it and an unbounded loss — and a stop is a resting order
that a gap can jump straight through. Both long-and-short strategies were
exposed, one of them an active paper run.

The condition is `equity < short_notional × maintenance_margin_pct / 100`, with
equity computed the engine's way (`cash + Σlong − Σshort`, cash already holding
the short proceeds). These tests drive a real SimBroker into a genuine breach by
shorting on a small account and then running the price UP, rather than poking
the condition's inputs directly.
"""

from __future__ import annotations

import copy
import json

import pytest

from app.broker.sim import SimBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.runtime.runner import _PendingIntent, StrategyRunner
from app.strategy.schema import StrategyDocument
from tests.test_runner import MIN_MS, FakeAggregator, QuoteBook, bar_event, settle
from tests.test_runner_session import ALWAYS, NEVER, FixedMDS, et_ms, warmup_at
from tests.test_strategy import EXAMPLE

SYM = "SPY"
TS = et_ms(2026, 7, 15, 10, 30)

# A deliberately small account: the breach condition compares equity against a
# PERCENTAGE of short market value, so a $100k account can never be brought to
# 30% of a $2k short. Real forced buy-ins happen on accounts that are small
# relative to the position, and that is what this reproduces.
CASH = 2_000.0
ENTRY_PX = 100.0
# short ~20 shares @ 100 -> cash 4,000, equity 4,000 - 2,000 = 2,000
BREACH_PX = 180.0    # equity 4,000 - 3,600 = 400 < 3,600 * 30% = 1,080  -> COVER
SAFE_PX = 110.0      # equity 4,000 - 2,200 = 1,800 > 2,200 * 30% =   660  -> hold


def margin_doc(*, short=True, maintenance_margin_pct=30.0) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    data["universe"] = {"symbols": [SYM]}
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    data["entry"] = ({"short": {"when": ALWAYS}, "long": {"when": NEVER}} if short
                     else {"long": {"when": ALWAYS}})
    # A stop far enough away that it cannot fire before the margin check does —
    # the point is to test the forced buy-in, not the stop.
    data["exit"] = {"stop_loss": {"type": "percent", "value": 100.0}}
    # 100% for the short (proceeds land in cash, so the full account can be
    # committed); less for the long, whose cost must fit inside starting cash.
    data["sizing"] = {"type": "percent_of_equity", "value": 100 if short else 50}
    data["risk"] = {"max_open_positions": 2, "max_daily_orders": 100,
                    "max_daily_loss": 1_000_000}
    if short:
        data["risk"]["short"] = {"enabled": True, "max_short_notional": 25_000,
                                 "maintenance_margin_pct": maintenance_margin_pct}
    data["session"] = {"flatten_eod": False}
    return StrategyDocument.model_validate(data)


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
    return SimBroker(book.get, starting_cash=CASH, sim_latency_ms=0, sim_slippage_bps=0)


@pytest.fixture
def gateway(db):
    return OrderGateway(db, orders_per_minute=6000)


async def open_position(db, broker, gateway, book, doc):
    """Start a run and take its position on the first pushed bar."""
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway,
                            FixedMDS({SYM: warmup_at(TS, px=ENTRY_PX)}), agg, db)
    await runner.start()
    await push(agg, book, runner, TS, ENTRY_PX)
    return runner, agg


async def push(agg, book, runner, ts_ms: int, px: float) -> None:
    book.set(SYM, bid=px - 0.1, ask=px + 0.1)
    await agg.push(SYM, "1m", bar_event(SYM, ts_ms, px, px + 0.5, px - 0.5, px, 1000))
    await settle()


async def exit_reasons(db) -> list[str]:
    rows = await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind='order' ORDER BY id")
    return [json.loads(r["payload_json"])["exit_reason"] for r in rows
            if "exit_reason" in json.loads(r["payload_json"])]


async def risk_events(db) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind='risk' ORDER BY id")
    return [json.loads(r["payload_json"]) for r in rows]


# ------------------------------------------------------------------- the breach

async def test_a_breaching_short_is_force_covered(db, broker, gateway, book):
    """THE gap: the engine covers here and the runner used to hold."""
    runner, agg = await open_position(db, broker, gateway, book, margin_doc())
    assert runner._positions[SYM].side == "short"

    await push(agg, book, runner, TS + MIN_MS, BREACH_PX)

    assert SYM not in runner._positions, "breaching short was not covered"
    assert "margin_call" in await exit_reasons(db)


async def test_the_cover_is_a_buy_to_cover_for_the_full_short(db, broker, gateway, book):
    """The STRONG property: not merely that an order went out, but that it was
    the correct instruction for the correct size. A `sell` here would double the
    short instead of closing it."""
    runner, agg = await open_position(db, broker, gateway, book, margin_doc())
    qty = runner._positions[SYM].qty

    await push(agg, book, runner, TS + MIN_MS, BREACH_PX)

    rows = await db.fetch_all(
        "SELECT side, qty, purpose FROM order_intents WHERE run_id='run-1' "
        "AND purpose='exit' ORDER BY id")
    assert [dict(r) for r in rows] == [
        {"side": "buy_to_cover", "qty": qty, "purpose": "exit"}]


async def test_the_margin_event_reports_the_numbers_it_decided_on(db, broker,
                                                                 gateway, book):
    """A forced buy-in the user cannot audit afterwards is nearly as bad as
    none, so the event carries equity, short notional and the threshold."""
    runner, agg = await open_position(db, broker, gateway, book, margin_doc())
    await push(agg, book, runner, TS + MIN_MS, BREACH_PX)

    events = [e for e in await risk_events(db) if e.get("event") == "margin_call"]
    assert len(events) == 1
    event = events[0]
    assert event["covering"] == [SYM]
    assert event["equity"] < event["maintenance_threshold"]
    # 20 shares short, marked at the breaching bar's close
    assert event["short_notional"] == pytest.approx(20 * BREACH_PX)
    assert event["maintenance_threshold"] == pytest.approx(
        event["short_notional"] * 0.30, rel=1e-6)


# ------------------------------------------------------------------- no breach

async def test_a_short_above_the_threshold_is_left_alone(db, broker, gateway, book):
    """The control. An adverse move that does NOT breach maintenance margin must
    not trigger a cover — a forced buy-in that fires early is a strategy-killer,
    not a safety feature."""
    runner, agg = await open_position(db, broker, gateway, book, margin_doc())
    await push(agg, book, runner, TS + MIN_MS, SAFE_PX)

    assert SYM in runner._positions
    assert "margin_call" not in await exit_reasons(db)
    assert await risk_events(db) == []


async def test_a_lower_maintenance_pct_tolerates_the_same_move(db, broker,
                                                              gateway, book):
    """The threshold is genuinely read from the document rather than hardcoded:
    at 5% the same price that breaches at 30% does not."""
    runner, agg = await open_position(db, broker, gateway, book,
                                     margin_doc(maintenance_margin_pct=5.0))
    await push(agg, book, runner, TS + MIN_MS, BREACH_PX)

    # equity 400 vs 3,600 * 5% = 180 -> no breach
    assert SYM in runner._positions
    assert "margin_call" not in await exit_reasons(db)


async def test_a_long_position_is_never_force_covered(db, broker, gateway, book):
    """Maintenance margin applies to the SHORT leg. A long that has lost value
    is bounded at zero and must be left to its own stop."""
    runner, agg = await open_position(db, broker, gateway, book, margin_doc(short=False))
    assert runner._positions[SYM].side == "long"

    await push(agg, book, runner, TS + MIN_MS, 51.0)   # brutal, but bounded

    assert "margin_call" not in await exit_reasons(db)
    assert await risk_events(db) == []


# ---------------------------------------------------------------- no duplicates

async def test_a_working_cover_is_not_resubmitted(db, broker, gateway, book):
    """The sweep runs on every bar across every short, so a cover that has not
    filled yet must not be sent again — that would buy back the position twice
    and flip it long."""
    runner, agg = await open_position(db, broker, gateway, book, margin_doc())
    position = runner._positions[SYM]
    # Simulate the first cover still working at the broker.
    runner._orders["working-cover"] = _PendingIntent(
        "intent-1", "working-cover", "exit", SYM, side="short")

    await push(agg, book, runner, TS + MIN_MS, BREACH_PX)

    assert runner._exit_pending(SYM)
    exits = await db.fetch_all(
        "SELECT id FROM order_intents WHERE run_id='run-1' AND purpose='exit'")
    assert exits == [], "re-submitted a cover while one was already working"
    assert runner._positions[SYM] is position
