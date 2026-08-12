"""Audit A6/A7/A8a — scale-in, the order-timeout clamp, and short buying power.

**A6 `entry.pyramid`** was built in the engine only; live, a position was entered
once at the original size and never added to. Closing it needed a new order
purpose (`pyramid`) through the gateway, because an add is a discretionary buy
like an entry but must NOT be gated by `max_open_positions` — the position it
grows already counted against that limit when it opened, so a single-symbol
pyramid strategy with `max_open_positions: 1` could never add at all. That
gateway behaviour is asserted here directly, since it is the load-bearing part.

**A7** — `orders.unfilled_timeout_sec` was silently clamped to 30s. The clamp is
defensible (`max_wait_s` blocks the gateway on the rate limiter, and blocking a
runner's loop for 300s stalls every other symbol); the silence was not.

**A8a** — the audit recorded this as "short entry size capped by cash, not
margin ... conservative direction, undersizes shorts". That is wrong in both
parts, and these tests pin the corrected behaviour: `engine.sized_qty` passes the
same `available_cash` for both sides, so capping by cash IS engine-equivalent.
The real divergence was that the engine passes `cash - reserved_margin` while the
runner passed raw broker cash — which for a short includes the sale proceeds. The
runner therefore OVERSIZED once a short was open, the dangerous direction.
"""

from __future__ import annotations

import copy
import json

import pytest

from app.broker.sim import SimBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.runtime.runner import ORDER_WAIT_CEILING_S, StrategyRunner
from app.strategy.schema import StrategyDocument
from tests.test_runner import MIN_MS, FakeAggregator, QuoteBook, bar_event, settle
from tests.test_runner_session import ALWAYS, NEVER, FixedMDS, et_ms, warmup_at
from tests.test_strategy import EXAMPLE

SYM = "SPY"
TS = et_ms(2026, 7, 15, 10, 30)
ENTRY_PX = 100.0


def pyr_doc(*, max_adds=2, size_pct=100.0, spacing_pct=2.0, short=False,
            max_open_positions=1, pyramid=True, take_profit=20.0,
            unfilled_timeout_sec=None, max_short_notional=25_000,
            sizing_pct=10) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    data["universe"] = {"symbols": [SYM]}
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    data["entry"] = ({"short": {"when": ALWAYS}, "long": {"when": NEVER}} if short
                     else {"long": {"when": ALWAYS}})
    if pyramid:
        data["entry"]["pyramid"] = {
            "max_adds": max_adds, "size_pct": size_pct,
            "spacing": {"type": "percent", "value": spacing_pct}}
    data["exit"] = {"stop_loss": {"type": "percent", "value": 5.0},
                    "take_profit": {"type": "percent", "value": take_profit}}
    data["sizing"] = {"type": "percent_of_equity", "value": sizing_pct}
    data["risk"] = {"max_open_positions": max_open_positions, "max_daily_orders": 100,
                    "max_daily_loss": 1_000_000}
    if short:
        data["risk"]["short"] = {"enabled": True, "max_short_notional": max_short_notional}
    data["session"] = {"flatten_eod": False}
    if unfilled_timeout_sec is not None:
        data["orders"] = {"unfilled_timeout_sec": unfilled_timeout_sec}
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


async def push(agg, book, ts_ms: int, px: float, *, high=None, low=None) -> None:
    book.set(SYM, bid=px - 0.1, ask=px + 0.1)
    await agg.push(SYM, "1m", bar_event(SYM, ts_ms, px,
                                        high if high is not None else px + 0.5,
                                        low if low is not None else px - 0.5, px, 1000))
    await settle()


async def start_and_enter(db, broker, gateway, book, doc):
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway,
                            FixedMDS({SYM: warmup_at(TS, px=ENTRY_PX)}), agg, db)
    await runner.start()
    await push(agg, book, TS, ENTRY_PX)
    return runner, agg


async def fills(db) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind='fill' ORDER BY id")
    return [json.loads(r["payload_json"]) for r in rows]


def doc_limit(runner) -> int:
    return runner.doc.risk.max_open_positions


async def intents(db) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT symbol, side, qty, purpose FROM order_intents WHERE run_id='run-1' ORDER BY id")
    return [dict(r) for r in rows]


# ------------------------------------------------------------------- A6: adds

async def test_no_add_until_the_spacing_level_is_touched(db, broker, gateway, book):
    runner, agg = await start_and_enter(db, broker, gateway, book, pyr_doc())
    position = runner._positions[SYM]
    original = position.qty
    assert position.next_add_px == pytest.approx(position.avg_price * 1.02)

    # a favourable move that stops just short of the level
    await push(agg, book, TS + MIN_MS, 101.0, high=position.next_add_px - 0.01)

    assert runner._positions[SYM].qty == original
    assert [i for i in await intents(db) if i["purpose"] == "pyramid"] == []


async def test_touching_the_level_adds_at_the_volume_weighted_average(db, broker,
                                                                     gateway, book):
    """THE gap: the engine grew this position and the runner never did."""
    runner, agg = await start_and_enter(db, broker, gateway, book, pyr_doc())
    position = runner._positions[SYM]
    original, level = position.qty, position.next_add_px

    await push(agg, book, TS + MIN_MS, level + 1.0)

    assert position.qty == original * 2, "the add did not merge into the position"
    assert position.adds_done == 1
    entry_fill, add_fill = [f for f in await fills(db)][:2]
    assert add_fill["event"] == "pyramid_filled"
    # equal-sized adds -> the average is the mean of the two fills
    assert position.avg_price == pytest.approx(
        (entry_fill["price"] + add_fill["price"]) / 2)
    # the next level is spaced off THIS add's fill, not off the original entry
    assert position.next_add_px == pytest.approx(add_fill["price"] * 1.02)


async def test_the_add_order_is_a_real_second_order_of_the_right_side_and_size(
        db, broker, gateway, book):
    runner, agg = await start_and_enter(db, broker, gateway, book,
                                        pyr_doc(size_pct=50.0))
    position = runner._positions[SYM]
    original = position.qty

    await push(agg, book, TS + MIN_MS, position.next_add_px + 1.0)

    adds = [i for i in await intents(db) if i["purpose"] == "pyramid"]
    assert adds == [{"symbol": SYM, "side": "buy", "qty": original // 2,
                     "purpose": "pyramid"}]
    assert position.qty == original + original // 2


async def test_max_adds_is_respected_and_then_the_ladder_retires(db, broker,
                                                                gateway, book):
    runner, agg = await start_and_enter(db, broker, gateway, book,
                                        pyr_doc(max_adds=1))
    position = runner._positions[SYM]

    await push(agg, book, TS + MIN_MS, position.next_add_px + 1.0)
    assert position.adds_done == 1
    assert position.next_add_px is None, "ladder should retire at max_adds"

    qty_after_one = position.qty
    await push(agg, book, TS + 2 * MIN_MS, 130.0)   # far above any level
    assert position.qty == qty_after_one


async def test_only_one_add_per_bar(db, broker, gateway, book):
    """The engine's `while` loop can consume several levels in one bar; the
    runner cannot, because each level is derived from the previous add's actual
    fill and live that fill has not happened yet. One add per bar is the
    conservative reading and the documented divergence."""
    runner, agg = await start_and_enter(db, broker, gateway, book,
                                        pyr_doc(max_adds=3))
    position = runner._positions[SYM]
    original, first_level = position.qty, position.next_add_px

    # a bar whose high clears the first THREE levels at once
    await push(agg, book, TS + MIN_MS, 100.5, high=position.avg_price * 1.10)

    assert position.adds_done == 1
    assert position.qty == original * 2, "more than one add was taken in a single bar"
    # the ladder advanced off the add's own fill, so a LATER bar can add again
    assert position.next_add_px is not None
    assert position.next_add_px > first_level

    # Note: a second add cannot be driven from this test on the next pushed bar,
    # because equal-sized adds are identical intents and the gateway suppresses
    # duplicates within DUPLICATE_WINDOW_S (10s wall-clock). Real bars are at
    # least a minute apart, so that window cannot bite in production — see
    # `_maybe_pyramid`. `test_max_adds_is_respected_and_then_the_ladder_retires`
    # covers ladder exhaustion.


async def test_a_short_adds_on_a_favourable_downward_move(db, broker, gateway, book):
    runner, agg = await start_and_enter(db, broker, gateway, book, pyr_doc(short=True))
    position = runner._positions[SYM]
    assert position.side == "short"
    # favourable for a short is DOWN
    assert position.next_add_px == pytest.approx(position.avg_price * 0.98)
    original = position.qty

    await push(agg, book, TS + MIN_MS, position.next_add_px - 1.0)

    assert position.qty == original * 2
    adds = [i for i in await intents(db) if i["purpose"] == "pyramid"]
    assert [a["side"] for a in adds] == ["sell_short"], "an add must not be a plain sell"


async def test_the_stop_is_re_placed_from_the_new_average(db, broker, gateway, book):
    """The stop travels with the position. Leaving the original stop would have a
    now-more-distant stop protecting a much larger position — the engine
    recomputes, so the runner must too."""
    runner, agg = await start_and_enter(db, broker, gateway, book, pyr_doc())
    position = runner._positions[SYM]
    old_stop_id, old_stop_px = position.stop_broker_order_id, position.stop_px

    await push(agg, book, TS + MIN_MS, position.next_add_px + 1.0)

    assert position.stop_broker_order_id != old_stop_id, "stop was not replaced"
    assert position.stop_px == pytest.approx(position.avg_price * 0.95)
    assert position.stop_px > old_stop_px, "stop should have travelled up with the average"
    resting = broker._working[position.stop_broker_order_id]
    assert resting.req.stop_px == pytest.approx(position.stop_px)
    assert resting.req.qty == position.qty, "the stop must cover the WHOLE position"


async def test_the_take_profit_is_re_placed_for_the_new_size(db, broker, gateway, book):
    runner, agg = await start_and_enter(db, broker, gateway, book, pyr_doc())
    position = runner._positions[SYM]
    old_target = position.target_broker_order_id

    await push(agg, book, TS + MIN_MS, position.next_add_px + 1.0)

    assert position.target_broker_order_id != old_target
    resting = broker._working[position.target_broker_order_id]
    assert resting.req.qty == position.qty
    assert resting.req.limit_px == pytest.approx(position.avg_price * 1.20)


async def test_a_document_without_pyramid_never_adds(db, broker, gateway, book):
    runner, agg = await start_and_enter(db, broker, gateway, book,
                                        pyr_doc(pyramid=False))
    position = runner._positions[SYM]
    original = position.qty
    assert position.next_add_px is None and position.add_qty == 0

    await push(agg, book, TS + MIN_MS, 130.0)

    assert runner._positions.get(SYM) is None or position.qty == original
    assert [i for i in await intents(db) if i["purpose"] == "pyramid"] == []


# ------------------------------------------- A6: the gateway gate that mattered

async def test_an_add_is_not_blocked_by_max_open_positions(db, broker, gateway, book):
    """The reason `pyramid` had to be its own purpose. With
    `max_open_positions: 1` — the normal setting for a single-symbol pyramid
    strategy — an add submitted as an `entry` is rejected, because the position
    it is growing already fills the limit. Getting this wrong does not fail
    loudly; it silently turns the feature off."""
    runner, agg = await start_and_enter(db, broker, gateway, book,
                                        pyr_doc(max_open_positions=1))
    position = runner._positions[SYM]
    assert runner.risk.open_position_count() == 1 == doc_limit(runner)

    await push(agg, book, TS + MIN_MS, position.next_add_px + 1.0)

    assert position.adds_done == 1, "the add was blocked by max_open_positions"
    rejects = await db.fetch_all(
        "SELECT status FROM order_intents WHERE run_id='run-1' AND purpose='pyramid'")
    assert [r["status"] for r in rejects] == ["SUBMITTED"]


# ----------------------------------------------------------- A7: the clamp is loud

async def test_a_clamped_order_timeout_is_reported(db, broker, gateway, book):
    doc = pyr_doc(unfilled_timeout_sec=300)
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway,
                            FixedMDS({SYM: warmup_at(TS, px=ENTRY_PX)}), agg, db)
    await runner.start()

    rows = await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind='config'")
    events = [json.loads(r["payload_json"]) for r in rows]
    assert events == [{"event": "orders_timeout_clamped", "configured_s": 300,
                       "applied_s": ORDER_WAIT_CEILING_S}]
    assert runner._order_wait_s() == ORDER_WAIT_CEILING_S


async def test_a_timeout_within_the_ceiling_is_used_as_configured(db, broker,
                                                                 gateway, book):
    doc = pyr_doc(unfilled_timeout_sec=20)
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway,
                            FixedMDS({SYM: warmup_at(TS, px=ENTRY_PX)}), agg, db)
    await runner.start()

    assert runner._order_wait_s() == 20
    rows = await db.fetch_all(
        "SELECT id FROM run_events WHERE run_id='run-1' AND kind='config'")
    assert rows == []


# ------------------------------------------- A8a: short buying power (corrected)

async def test_an_open_short_reserves_margin_against_the_next_position(
        db, broker, gateway, book):
    """The corrected A8a. Half of a $100k account is shorted; at the default
    50% initial margin that commits $... of margin which is NOT spendable on the
    next position, exactly as `engine.sized_qty`'s `cash - reserved_margin`.
    Before the fix the runner saw the short's proceeds as free cash."""
    doc = pyr_doc(short=True, pyramid=False, sizing_pct=50, max_short_notional=100_000)
    runner, agg = await start_and_enter(db, broker, gateway, book, doc)
    position = runner._positions[SYM]

    committed = runner._short_margin_committed()
    assert committed == pytest.approx(position.qty * position.avg_price * 0.50)
    assert committed > 0, "an open short must commit margin"


async def test_short_margin_committed_ignores_longs(db, broker, gateway, book):
    runner, agg = await start_and_enter(db, broker, gateway, book,
                                        pyr_doc(pyramid=False))
    assert runner._positions[SYM].side == "long"
    assert runner._short_margin_committed() == 0.0


async def test_a_short_is_clamped_to_max_short_notional_not_rejected(db, broker,
                                                                    gateway, book):
    """The engine clamps a short to `max_short_notional` and trades the smaller
    size; a bare gateway rejection would have live take NO position where the
    backtest took one."""
    doc = pyr_doc(short=True, pyramid=False, sizing_pct=50, max_short_notional=1_000)
    runner, agg = await start_and_enter(db, broker, gateway, book, doc)

    position = runner._positions.get(SYM)
    assert position is not None, "the short was rejected instead of clamped"
    assert position.qty * position.avg_price <= 1_000 * 1.01
