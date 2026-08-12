"""Audit C2/C3 — the run event log's two blind spots.

C2: `_place_protective` logged only on FAILURE, so the event log a user reads in
RunDetail.tsx never once confirmed that a position was protected. The absence of a
`protective_skipped` line is not evidence that a stop rests at the broker.
`_submit_entry` had always logged its own success, so the asymmetry was the bug.

C3: the entry path returned silently on `qty <= 0`, on cooldown and on
max-entries-per-day, so a user watching a signal fire and no order appear could
not tell "no signal" from "blocked". Blocks are logged only when the document's
rule ACTUALLY fired — logging every quiet bar would bury the log at 81 symbols ×
~390 bars a day — which is the property most of these tests pin down.
"""

from __future__ import annotations

import copy
import json

import pytest

from app.broker.sim import SimBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.runtime.runner import StrategyRunner
from app.strategy.schema import StrategyDocument
from tests.test_runner import MIN_MS, FakeAggregator, QuoteBook, bar_event, settle
from tests.test_runner_session import ALWAYS, NEVER, FixedMDS, et_ms, warmup_at
from tests.test_strategy import EXAMPLE

SYM = "SPY"
TS = et_ms(2026, 7, 15, 10, 30)


def vis_doc(*, fires=True, entry_window=None, cooldown_bars=1,
            max_entries_per_day=1, sizing_pct=10, take_profit=5.0,
            scale_outs=None) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    data["universe"] = {"symbols": [SYM]}
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    data["entry"] = {"long": {"when": ALWAYS if fires else NEVER},
                     "cooldown_bars": cooldown_bars,
                     "max_entries_per_day": max_entries_per_day}
    data["exit"] = {"stop_loss": {"type": "percent", "value": 2.0},
                    "take_profit": {"type": "percent", "value": take_profit}}
    if scale_outs:
        data["exit"]["scale_outs"] = scale_outs
    data["sizing"] = {"type": "percent_of_equity", "value": sizing_pct}
    data["risk"] = {"max_open_positions": 1, "max_daily_orders": 100,
                    "max_daily_loss": 1_000_000}
    session = {"flatten_eod": False}
    if entry_window:
        session["entry_window"] = entry_window
    data["session"] = session
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
    return SimBroker(book.get, starting_cash=100_000, sim_latency_ms=0, sim_slippage_bps=0)


@pytest.fixture
def gateway(db):
    return OrderGateway(db, orders_per_minute=6000)


async def start(db, broker, gateway, book, doc, *, cash=None):
    if cash is not None:
        broker = SimBroker(book.get, starting_cash=cash, sim_latency_ms=0, sim_slippage_bps=0)
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway,
                            FixedMDS({SYM: warmup_at(TS, px=100.0)}), agg, db)
    await runner.start()
    return runner, agg, broker


async def push(agg, book, ts_ms: int, px: float = 100.0) -> None:
    book.set(SYM, bid=px - 0.1, ask=px + 0.1)
    await agg.push(SYM, "1m", bar_event(SYM, ts_ms, px, px + 0.5, px - 0.5, px, 1000))
    await settle()


async def events(db, kind: str) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind=? ORDER BY id",
        (kind,))
    return [json.loads(r["payload_json"]) for r in rows]


# ------------------------------------------------------- C2: protection is logged

async def test_a_successful_stop_placement_logs_an_event(db, broker, gateway, book):
    """THE C2 gap: a protected position produced no evidence that it was."""
    runner, agg, broker = await start(db, broker, gateway, book, vis_doc())
    await push(agg, book, TS)

    placed = await events(db, "order")
    stops = [e for e in placed if e.get("event") == "protective_placed"
             and e["purpose"] == "stop"]
    assert len(stops) == 1
    stop = stops[0]
    assert stop["symbol"] == SYM
    assert stop["order_side"] == "sell"        # closing a long
    assert stop["order_type"] == "stop"
    assert stop["qty"] == runner._positions[SYM].qty
    assert stop["price"] == pytest.approx(runner._positions[SYM].avg_price * 0.98)
    assert stop["broker_order_id"] == runner._positions[SYM].stop_broker_order_id


async def test_the_take_profit_placement_is_logged_too(db, broker, gateway, book):
    runner, agg, broker = await start(db, broker, gateway, book, vis_doc())
    await push(agg, book, TS)

    targets = [e for e in await events(db, "order")
               if e.get("event") == "protective_placed" and e["purpose"] == "target"]
    assert len(targets) == 1
    assert targets[0]["order_type"] == "limit"
    assert targets[0]["price"] == pytest.approx(runner._positions[SYM].avg_price * 1.05)


async def test_every_scale_out_level_is_logged(db, broker, gateway, book):
    doc = vis_doc(scale_outs=[{"target": {"type": "percent", "value": 2.0}, "percent": 50.0}])
    runner, agg, broker = await start(db, broker, gateway, book, doc)
    await push(agg, book, TS)

    scale = [e for e in await events(db, "order")
             if e.get("event") == "protective_placed" and e["purpose"] == "scale_out"]
    assert len(scale) == 1
    assert scale[0]["qty"] > 0


# ----------------------------------------------------- C3: blocked, not silent

async def test_a_fired_signal_blocked_by_cooldown_says_so(db, broker, gateway, book):
    """Enter on the first bar, close the position, then have the cooldown block
    the next signal — the user must be able to see WHY nothing happened."""
    doc = vis_doc(cooldown_bars=50, max_entries_per_day=5)
    runner, agg, broker = await start(db, broker, gateway, book, doc)
    await push(agg, book, TS)
    await runner.flatten()
    await settle()
    assert SYM not in runner._positions

    await push(agg, book, TS + MIN_MS)

    blocked = await events(db, "entry_blocked")
    assert [(b["reason"], b["side"]) for b in blocked] == [("cooldown", "long")]


async def test_max_entries_per_day_is_reported(db, broker, gateway, book):
    doc = vis_doc(cooldown_bars=0, max_entries_per_day=1)
    runner, agg, broker = await start(db, broker, gateway, book, doc)
    await push(agg, book, TS)
    await runner.flatten()
    await settle()

    await push(agg, book, TS + MIN_MS)

    reasons = [b["reason"] for b in await events(db, "entry_blocked")]
    assert reasons == ["max_entries_per_day"]


async def test_an_unaffordable_position_is_reported(db, broker, gateway, book):
    """qty<=0 is the one block that indicates a CONFIGURATION problem rather than
    routine throttling: the strategy cannot afford a single share."""
    runner, agg, broker = await start(db, broker, gateway, book,
                                      vis_doc(sizing_pct=1), cash=10.0)
    await push(agg, book, TS)

    blocked = await events(db, "entry_blocked")
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "qty_zero"
    assert blocked[0]["side"] == "long"
    assert blocked[0]["entry_px"] == pytest.approx(100.0)
    assert "available" in blocked[0] and "equity" in blocked[0]
    assert runner._positions == {}


async def test_a_signal_outside_the_entry_window_is_reported(db, broker, gateway, book):
    doc = vis_doc(entry_window={"start": "09:30", "end": "09:45"})
    runner, agg, broker = await start(db, broker, gateway, book, doc)

    await push(agg, book, TS)      # 10:30 ET, outside the window

    assert [b["reason"] for b in await events(db, "entry_blocked")] == ["entry_window"]
    assert runner._positions == {}


async def test_a_quiet_bar_logs_nothing_at_all(db, broker, gateway, book):
    """The property that keeps this feature usable. A document whose rule does
    NOT fire must produce no entry_blocked rows, however many bars arrive —
    otherwise an 81-symbol run would write tens of thousands of rows a day and
    the log would be useless for the case it exists to explain."""
    runner, agg, broker = await start(db, broker, gateway, book,
                                      vis_doc(fires=False, cooldown_bars=50))
    for n in range(5):
        await push(agg, book, TS + n * MIN_MS)

    assert await events(db, "entry_blocked") == []


async def test_a_signal_that_is_not_blocked_logs_no_block(db, broker, gateway, book):
    runner, agg, broker = await start(db, broker, gateway, book, vis_doc())
    await push(agg, book, TS)

    assert SYM in runner._positions
    assert await events(db, "entry_blocked") == []
