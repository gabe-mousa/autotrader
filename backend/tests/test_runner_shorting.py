"""Audit A2 — the StrategyRunner's SHORT path (plan/15).

Why this file exists: the backtest engine, OrderGateway and SimBroker all
handled shorting correctly while `StrategyRunner` was structurally long-only —
it never evaluated `entry.short`, hardcoded side="buy"/"sell", and hardcoded the
protective stop BELOW entry and the target ABOVE. A short strategy therefore
backtested one way and paper/live-traded another, silently, which is the single
most expensive class of bug this system can have.

Every property pinned here is one that costs real money when it breaks:
SELL_SHORT is a different order from SELL, a short's stop must rest ABOVE the
entry (a stop below it is an instant guaranteed loss), a cover must never buy
through zero into an unintended long, a winning short must not be reported as a
losing one (that reading feeds the max_daily_loss entry block), and a short must
survive a crash with its mirrored protection intact.

Reuses test_runner.py's fixtures/harness so the short path is exercised through
exactly the same real Database + OrderGateway + SimBroker wiring as the long
path — nothing about shorting is tested against a mock that could agree with a
wrong implementation.
"""

from __future__ import annotations

import copy

import pytest

from app.broker.sim import SimBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.runtime.risk import RunRiskTracker
from app.runtime.runner import StrategyRunner, _LivePosition
from app.strategy.schema import StrategyDocument
# The harness (fake aggregator/MDS/quote book, bar helpers) is shared with the
# long-side runner tests on purpose: the short path must be driven through the
# exact same real Database + OrderGateway + SimBroker wiring, so a divergence
# between the two can only come from the runner itself. Fixtures are re-declared
# below rather than imported, since importing a fixture shadows its definition.
from tests.test_runner import (MIN_MS, FakeAggregator, FakeMDS, QuoteBook, bar_event,
                               settle, warmup_df)
from tests.test_strategy import EXAMPLE


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

ALWAYS = {"op": "lt", "left": {"ind": "rsi14"}, "right": {"value": 200}}   # always true
NEVER = {"op": "gt", "left": {"ind": "rsi14"}, "right": {"value": 200}}    # never true


def short_doc(*, long_fires=False, short_fires=True, signal_short=None,
              signal=None, trailing_stop=None, **overrides) -> StrategyDocument:
    """A shortable document: `entry.short` present AND `risk.short.enabled`.
    Both are required — the opt-in is deliberately separate from the rule's
    presence so a short can't start trading through a config accident."""
    data = copy.deepcopy(EXAMPLE)
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    entry: dict = {"short": {"when": ALWAYS if short_fires else NEVER}}
    if long_fires is not None:
        entry["long"] = {"when": ALWAYS if long_fires else NEVER}
    data["entry"] = entry
    data["exit"] = {"stop_loss": {"type": "percent", "value": 2.0},
                    "take_profit": {"type": "percent", "value": 5.0}}
    if signal is not None:
        data["exit"]["signal"] = {"when": ALWAYS if signal else NEVER}
    if signal_short is not None:
        data["exit"]["signal_short"] = {"when": ALWAYS if signal_short else NEVER}
    if trailing_stop is not None:
        data["exit"]["trailing_stop"] = trailing_stop
    data["sizing"] = {"type": "percent_of_equity", "value": 10}
    data["risk"] = {"max_open_positions": 1, "max_daily_orders": 50,
                    "max_daily_loss": 100000,
                    "short": {"enabled": True, "max_short_notional": 25_000}}
    data.update(overrides)
    return StrategyDocument.model_validate(data)


async def start_runner(db, broker, gateway, doc, book, symbol="SPY", px=100.0):
    mds = FakeMDS({symbol: warmup_df(n=10, start_px=px)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    await runner.start()
    book.set(symbol, bid=px - 0.1, ask=px + 0.1)
    return runner, agg


async def push(runner, agg, symbol="SPY", px=100.0, high=None, low=None):
    ts = int(runner._candles[symbol].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push(symbol, "1m", bar_event(symbol, ts, px,
                                           high if high is not None else px + 0.5,
                                           low if low is not None else px - 0.5,
                                           px, 1000))
    await settle()


# ---------------------------------------------------------------- entry side

async def test_short_entry_emits_sell_short_not_sell(db, broker, gateway, book):
    """THE regression: `entry.short` was never evaluated and the order side was
    hardcoded "buy", so a short strategy silently went LONG in paper/live."""
    runner, agg = await start_runner(db, broker, gateway, short_doc(), book)
    await push(runner, agg)

    assert "SPY" in runner._positions, "entry.short never fired — runner is long-only"
    pos = runner._positions["SPY"]
    assert pos.side == "short"
    assert pos.qty > 0, "plan/15 decision #1: quantities stay POSITIVE"

    row = await db.fetch_one(
        "SELECT side, qty FROM order_intents WHERE run_id='run-1' AND purpose='entry'")
    assert row["side"] == "sell_short"
    # SimBroker is signed internally — a real short, not a long
    assert (await broker.get_positions())["SPY"] == pytest.approx(-pos.qty)
    await runner.stop()


async def test_long_still_emits_a_plain_buy(db, broker, gateway, book):
    """Guard against fixing shorts by breaking longs."""
    doc = short_doc(long_fires=True, short_fires=False)
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)

    assert runner._positions["SPY"].side == "long"
    row = await db.fetch_one(
        "SELECT side FROM order_intents WHERE run_id='run-1' AND purpose='entry'")
    assert row["side"] == "buy"
    await runner.stop()


async def test_a_short_rule_without_the_opt_in_never_trades(db, broker, gateway, book):
    """`risk.short.enabled` defaults False: presence of the rule is not consent."""
    doc = short_doc()
    doc.risk.short.enabled = False
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)
    assert runner._positions == {}
    await runner.stop()


async def test_hard_to_borrow_symbol_is_never_shorted(db, broker, gateway, book):
    doc = short_doc()
    doc.risk.short.hard_to_borrow = ["spy"]      # case-insensitive
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)
    assert runner._positions == {}
    await runner.stop()


# ------------------------------------------------- mirrored protective orders

async def test_short_protective_stop_rests_above_and_target_below(db, broker, gateway, book):
    """A short's stop must sit ABOVE the entry and its target BELOW. Getting
    this backwards (the old hardcoded below=True/False) puts a stop-loss at a
    guaranteed-loss price and a take-profit that can never pay."""
    runner, agg = await start_runner(db, broker, gateway, short_doc(), book)
    await push(runner, agg)
    pos = runner._positions["SPY"]

    stop = broker._working[pos.stop_broker_order_id].req
    target = broker._working[pos.target_broker_order_id].req

    assert stop.stop_px == pytest.approx(pos.avg_price * 1.02)   # 2% ABOVE
    assert stop.stop_px > pos.avg_price
    assert target.limit_px == pytest.approx(pos.avg_price * 0.95)  # 5% BELOW
    assert target.limit_px < pos.avg_price
    # both CLOSE the short, so both are buy_to_cover — not sells
    assert stop.side == "buy_to_cover" and target.side == "buy_to_cover"
    await runner.stop()


async def test_long_protective_orders_are_unchanged(db, broker, gateway, book):
    doc = short_doc(long_fires=True, short_fires=False)
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)
    pos = runner._positions["SPY"]

    stop = broker._working[pos.stop_broker_order_id].req
    target = broker._working[pos.target_broker_order_id].req
    assert stop.stop_px == pytest.approx(pos.avg_price * 0.98)
    assert target.limit_px == pytest.approx(pos.avg_price * 1.05)
    assert stop.side == "sell" and target.side == "sell"
    await runner.stop()


async def test_short_stop_triggers_on_strength_end_to_end(db, broker, gateway, book):
    """The resting stop actually fires the right way round through SimBroker:
    price RISING against the short closes it."""
    runner, agg = await start_runner(db, broker, gateway, short_doc(), book)
    await push(runner, agg)
    pos = runner._positions["SPY"]
    stop_px = broker._working[pos.stop_broker_order_id].req.stop_px

    await broker.process_tick("SPY", bid=stop_px - 1, ask=stop_px - 1, last=stop_px - 1)
    await settle()
    assert "SPY" in runner._positions, "a FALLING price must not stop out a short"

    await broker.process_tick("SPY", bid=stop_px + 1, ask=stop_px + 1, last=stop_px + 1)
    await settle()
    assert "SPY" not in runner._positions
    await runner.stop()


# ------------------------------------------------------------- trailing stop

async def test_short_trailing_stop_ratchets_down_as_price_falls(db, broker, gateway, book):
    """`_position_side()` was the single hook the trail ARITHMETIC needed — once
    it reads the real field, `compute_trail_px`/`trail_is_armed`/`_tighter_stop`
    all take the short branch on their own. The resting order's own SIDE was a
    separate fix (`_place_protective` had "sell" hardcoded), which is why this
    asserts both the level and the instruction."""
    doc = short_doc(trailing_stop={"type": "percent", "value": 1.0})
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)
    pos = runner._positions["SPY"]
    entry = pos.avg_price
    assert pos.trail_px == pytest.approx(entry * 1.01), "a short's trail sits ABOVE entry"

    dist = entry * 0.01          # a fixed DISTANCE, derived once from the entry
    await push(runner, agg, px=95.0, high=95.5, low=94.0)   # favourable excursion
    pos = runner._positions["SPY"]
    assert pos.trail_px == pytest.approx(94.0 + dist)      # ratcheted DOWN off the low
    stop = broker._working[pos.stop_broker_order_id].req
    assert stop.side == "buy_to_cover"
    assert stop.stop_px == pytest.approx(pos.trail_px)
    # the trail is TIGHTER than the fixed stop_loss, so it is what rests
    assert stop.stop_px < entry * 1.02

    await push(runner, agg, px=96.0, high=97.0, low=95.5)   # retrace
    assert runner._positions["SPY"].trail_px == pytest.approx(94.0 + dist), \
        "a short's trail must never loosen back out"
    await runner.stop()


# ------------------------------------------------------------- signal exits

async def test_exit_signal_short_is_used_for_a_short(db, broker, gateway, book):
    """`exit.signal_short` was unreachable: the runner never told the evaluator
    which side it held. Here `signal` never fires and `signal_short` always
    does, so only reading the short-side rule can close the position."""
    doc = short_doc(signal=False, signal_short=True)
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)
    assert "SPY" in runner._positions

    await push(runner, agg)          # second bar: exit rules are evaluated
    assert "SPY" not in runner._positions, "exit.signal_short is still unreachable"

    row = await db.fetch_one(
        "SELECT side FROM order_intents WHERE run_id='run-1' AND purpose='exit'")
    assert row["side"] == "buy_to_cover"
    await runner.stop()


async def test_a_long_uses_plain_signal_and_ignores_signal_short(db, broker, gateway, book):
    doc = short_doc(long_fires=True, short_fires=False, signal=False, signal_short=True)
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)
    await push(runner, agg)
    assert "SPY" in runner._positions, "a long must not exit on exit.signal_short"
    await runner.stop()


async def test_short_signal_falls_back_to_plain_signal(db, broker, gateway, book):
    """No `signal_short` in the document -> the short uses `exit.signal`, so an
    author who writes one exit rule gets it applied to both sides."""
    doc = short_doc(signal=True)
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)
    await push(runner, agg)
    assert "SPY" not in runner._positions
    await runner.stop()


# --------------------------------------------------------------- cover clamp

async def test_flatten_covers_a_short_with_buy_to_cover(db, broker, gateway, book):
    runner, agg = await start_runner(db, broker, gateway, short_doc(), book)
    await push(runner, agg)
    await runner.flatten()
    await settle()

    assert runner._positions == {}
    assert (await broker.get_positions()).get("SPY") is None
    row = await db.fetch_one(
        "SELECT side FROM order_intents WHERE run_id='run-1' AND purpose='exit'")
    assert row["side"] == "buy_to_cover"
    await runner.stop()


async def test_cover_clamp_now_actually_fires(db, broker, gateway, book):
    """The gateway's `cover_exceeds_short` check — which stops a cover buying
    through zero into an unintended LONG — had NEVER once executed in a real
    run: `RunRiskTracker.short_qty` tested `pos.qty < 0` against a
    `_LivePosition` whose qty is always positive, so it returned 0.0 for every
    symbol and the clamp compared against nothing. Now that it reads
    `pos.side`, an oversized cover is rejected locally."""
    runner, agg = await start_runner(db, broker, gateway, short_doc(), book)
    await push(runner, agg)
    pos = runner._positions["SPY"]
    assert runner.risk.short_qty("SPY") == pytest.approx(pos.qty)

    from app.execution.types import OrderIntentRequest
    res = await gateway.submit(
        OrderIntentRequest(run_id="run-1", symbol="SPY", side="buy_to_cover",
                           qty=pos.qty * 2, order_type="market", purpose="exit",
                           reference_price=100.0, reference_price_age_s=0.0),
        broker, runner.risk, None)
    assert not res.accepted
    assert "cover_exceeds_short" in res.reason
    await runner.stop()


def test_short_qty_is_zero_for_a_long_and_for_an_unknown_symbol():
    """The clamp must not accidentally authorise covering a LONG position."""
    class FakeRunner:
        run_id, mode = "r", "paper"
        _pending_entries: set = set()
        _last_price: dict = {}
        _orders_today = 0
        _realized_pnl_today = 0.0
        _positions = {"SPY": _LivePosition("SPY", 10, 100.0, 0, side="long"),
                      "QQQ": _LivePosition("QQQ", 7, 300.0, 0, side="short")}

    risk = RunRiskTracker(FakeRunner())
    assert risk.short_qty("SPY") == 0.0
    assert risk.short_qty("QQQ") == 7.0
    assert risk.short_qty("NVDA") == 0.0


# ------------------------------------------------------------ unrealized P&L

def test_unrealized_pnl_on_a_short_is_positive_when_price_falls():
    """A winning short reported as a loss feeds `daily_pnl()` and therefore the
    gateway's max_daily_loss ENTRY block — it would strangle a profitable short
    run. The old signed-qty formula gave a short a long's sign."""
    class FakeRunner:
        run_id, mode = "r", "paper"
        _pending_entries: set = set()
        _orders_today = 0
        _realized_pnl_today = 0.0
        _positions = {"SPY": _LivePosition("SPY", 10, 100.0, 0, side="short")}
        _last_price = {"SPY": 90.0}

    assert RunRiskTracker(FakeRunner())._unrealized_pnl() == pytest.approx(100.0)


def test_unrealized_pnl_on_a_short_is_negative_when_price_rises():
    class FakeRunner:
        run_id, mode = "r", "paper"
        _pending_entries: set = set()
        _orders_today = 0
        _realized_pnl_today = 0.0
        _positions = {"SPY": _LivePosition("SPY", 10, 100.0, 0, side="short")}
        _last_price = {"SPY": 110.0}

    assert RunRiskTracker(FakeRunner())._unrealized_pnl() == pytest.approx(-100.0)


def test_unrealized_pnl_on_a_long_is_unchanged():
    class FakeRunner:
        run_id, mode = "r", "paper"
        _pending_entries: set = set()
        _orders_today = 0
        _realized_pnl_today = 0.0
        _positions = {"SPY": _LivePosition("SPY", 10, 100.0, 0, side="long")}
        _last_price = {"SPY": 110.0}

    assert RunRiskTracker(FakeRunner())._unrealized_pnl() == pytest.approx(100.0)


async def test_realized_pnl_on_a_profitable_short_is_positive(db, broker, gateway, book):
    """A short covered BELOW its entry made money. This value is journaled and
    is what recovery.py's cash reconstruction sums, so its sign matters twice."""
    runner, agg = await start_runner(db, broker, gateway, short_doc(), book)
    await push(runner, agg)
    entry_px = runner._positions["SPY"].avg_price

    book.set("SPY", bid=90.0, ask=90.0)
    await runner.flatten()
    await settle()

    assert runner._realized_pnl_today > 0
    row = await db.fetch_one(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind='fill' "
        "ORDER BY rowid DESC LIMIT 1")
    import json
    payload = json.loads(row["payload_json"])
    assert payload["side"] == "short"           # the log records the side
    assert payload["order_side"] == "buy_to_cover"
    assert payload["realized_pnl"] > 0
    assert entry_px > 90.0
    await runner.stop()


# -------------------------------------------------- one position per symbol

async def test_long_and_short_cannot_be_held_on_one_symbol(db, broker, gateway, book):
    """Both sides' rules always fire here. A symbol holds at most ONE position
    (Entry's schema docstring: there is no netting), and long is checked first —
    same precedence as the backtest engine, so the two agree."""
    doc = short_doc(long_fires=True, short_fires=True)
    doc.risk.max_open_positions = 5
    doc.entry.max_entries_per_day = 50
    doc.entry.cooldown_bars = 0
    runner, agg = await start_runner(db, broker, gateway, doc, book)

    for _ in range(4):
        await push(runner, agg)

    assert list(runner._positions) == ["SPY"]
    assert runner._positions["SPY"].side == "long", "long is checked first"
    entries = await db.fetch_all(
        "SELECT side FROM order_intents WHERE run_id='run-1' AND purpose='entry' "
        "AND status != 'REJECTED_LOCAL'")
    assert {r["side"] for r in entries} == {"buy"}, "no sell_short while long"
    await runner.stop()


async def test_a_second_entry_is_blocked_while_one_is_pending(db, broker, gateway, book):
    """`_pending_entries` is keyed by SYMBOL, so it blocks regardless of side —
    an in-flight short entry cannot be joined by a long one."""
    doc = short_doc(long_fires=True)
    doc.entry.cooldown_bars = 0
    doc.entry.max_entries_per_day = 50
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    runner._pending_entries.add("SPY")
    await push(runner, agg)
    assert runner._positions == {}
    await runner.stop()


async def test_max_entries_per_day_is_shared_across_sides(db, broker, gateway, book):
    """A symbol that spent its daily entry allowance going short must not get a
    fresh allowance to go long."""
    doc = short_doc(long_fires=True)
    doc.entry.max_entries_per_day = 1
    doc.entry.cooldown_bars = 0
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    runner._entries_today["SPY"] = 1
    await push(runner, agg)
    assert runner._positions == {}
    await runner.stop()


# ------------------------------------------------------------ crash recovery

async def test_short_survives_recovery_with_mirrored_stops(db, broker, gateway, book):
    """A recovered SHORT must come back as a short with its protection mirrored.
    This is the LEGACY row shape — a negative qty and no `side` key, exactly
    what `runtime/recovery.py` still selects — and it must keep working: a
    misread side puts the protective stop on the wrong side of the market,
    which is an instant guaranteed loss."""
    doc = short_doc()
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    book.set("SPY", bid=99.9, ask=100.1)
    # what RunManager.recover_paper does: SimBroker is SIGNED internally, so the
    # row's negative qty seeds a real short without translation
    broker.seed_position("SPY", -50.0, 100.0)

    await runner.start(recovered_positions=[
        {"symbol": "SPY", "qty": -50.0, "avg_price": 100.0,      # negative == short
         "opened_at": int(runner_opened_at())}])

    pos = runner._positions["SPY"]
    assert pos.side == "short"
    assert pos.qty == 50.0, "in-memory qty is positive; only the row is signed"
    stop = broker._working[pos.stop_broker_order_id].req
    target = broker._working[pos.target_broker_order_id].req
    assert stop.stop_px == pytest.approx(102.0) and stop.side == "buy_to_cover"
    assert target.limit_px == pytest.approx(95.0) and target.side == "buy_to_cover"
    await runner.stop()


async def test_a_recovered_position_round_trips_its_sign(db, broker, gateway, book):
    """Write-then-read: `_upsert_position_row` signs the qty on the way out and
    `_recover_position` strips it on the way back in, so a short recovered from
    a row this runner itself wrote is still a short."""
    runner, agg = await start_runner(db, broker, gateway, short_doc(), book)
    await push(runner, agg)
    qty = runner._positions["SPY"].qty

    row = await db.fetch_one("SELECT qty FROM positions WHERE run_id='run-1' AND symbol='SPY'")
    assert row["qty"] == pytest.approx(-qty), "the durable row encodes short as a negative qty"
    await runner.stop()


async def test_a_legacy_unsigned_row_recovers_as_a_long(db, broker, gateway, book):
    """Rows written before shorting existed are positive and must read as longs
    — correct for those rows, and the safe default besides."""
    doc = short_doc(long_fires=True, short_fires=False)
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    book.set("SPY", bid=99.9, ask=100.1)
    broker.seed_position("SPY", 25.0, 100.0)
    await runner.start(recovered_positions=[
        {"symbol": "SPY", "qty": 25.0, "avg_price": 100.0, "opened_at": int(runner_opened_at())}])

    pos = runner._positions["SPY"]
    assert pos.side == "long"
    assert broker._working[pos.stop_broker_order_id].req.stop_px == pytest.approx(98.0)
    await runner.stop()


# --------------------------------------------- the durable `side` column

@pytest.mark.parametrize("long_side", [True, False])
async def test_side_column_round_trips_through_recovery(db, broker, gateway, book, long_side):
    """`positions.side` is now the authoritative record of direction. Written on
    every upsert, read back by `_recover_position` — for BOTH sides, so a
    reader outside `runtime/` never has to infer direction from a sign."""
    doc = short_doc(long_fires=long_side, short_fires=not long_side)
    expected = "long" if long_side else "short"
    runner, agg = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)
    assert runner._positions["SPY"].side == expected
    row = dict(await db.fetch_one(
        "SELECT symbol, qty, avg_price, opened_at, side FROM positions "
        "WHERE run_id='run-1' AND symbol='SPY'"))
    assert row["side"] == expected
    await runner.stop()

    # a fresh runner recovering that row. avg_price is nudged so the re-placed
    # protective orders aren't byte-identical to the ones the first runner just
    # sent — the gateway's duplicate suppression is journal-based and would
    # reject them, which has nothing to do with what this test is pinning.
    row["avg_price"] = row["avg_price"] + 0.5
    broker2 = SimBroker(book.get, starting_cash=100_000, sim_latency_ms=0, sim_slippage_bps=0)
    broker2.seed_position("SPY", row["qty"], row["avg_price"])
    # a fresh OrderGateway too: reusing one would reject the re-placed
    # protective orders as duplicates of the ones the first runner just sent
    fresh = StrategyRunner("run-1", doc, "paper", broker2, OrderGateway(db, orders_per_minute=6000),
                           FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)}),
                           FakeAggregator(), db)
    await fresh.start(recovered_positions=[row])

    assert fresh._positions["SPY"].side == expected
    assert fresh._positions["SPY"].qty > 0
    await fresh.stop()


async def test_side_column_wins_over_a_positive_qty_for_a_short(db, broker, gateway, book):
    """The point of the column: direction no longer DEPENDS on the sign. A row
    saying 'short' with an unsigned qty recovers as a short."""
    doc = short_doc()
    broker.seed_position("SPY", -30.0, 100.0)
    book.set("SPY", bid=99.9, ask=100.1)
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway,
                            FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)}),
                            FakeAggregator(), db)
    await runner.start(recovered_positions=[
        {"symbol": "SPY", "qty": 30.0, "avg_price": 100.0, "side": "short",
         "opened_at": int(runner_opened_at())}])

    pos = runner._positions["SPY"]
    assert pos.side == "short"
    assert broker._working[pos.stop_broker_order_id].req.stop_px == pytest.approx(102.0)
    await runner.stop()


async def test_a_legacy_negative_qty_row_still_recovers_as_a_short(db, broker, gateway, book):
    """The migration back-fills `side` with the DEFAULT 'long' on rows written
    before the column existed, so a legacy short arrives claiming to be long
    with a negative qty. The negative qty must win."""
    doc = short_doc()
    broker.seed_position("SPY", -30.0, 100.0)
    book.set("SPY", bid=99.9, ask=100.1)
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway,
                            FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)}),
                            FakeAggregator(), db)
    await runner.start(recovered_positions=[
        {"symbol": "SPY", "qty": -30.0, "avg_price": 100.0, "side": "long",
         "opened_at": int(runner_opened_at())}])

    pos = runner._positions["SPY"]
    assert pos.side == "short", "a legacy short flipped to long — its stop would be inverted"
    assert broker._working[pos.stop_broker_order_id].req.side == "buy_to_cover"
    await runner.stop()


def runner_opened_at() -> int:
    import time
    return int(time.time() * 1000) - 60_000
