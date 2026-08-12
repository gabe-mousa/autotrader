"""Audit A4 — `market_filter` (and its `exit_when`) in the LIVE/paper runner.

Why this file exists: the regime gate was honoured by the backtest engine and
IGNORED by `runtime/runner.py` — the string `market_filter` did not appear in it
at all. Live, the gate was wide open, which is exactly the "buying dips into a
falling market with no awareness of the tape" failure the feature was built to
prevent. 10 strategies configure a filter and 5 of those are leveraged-ETF
trend strategies whose ENTIRE thesis is the gate.

Every assertion here is a PARITY assertion against `backtest/engine.py`'s
`market_ok` / `market_exit`, not against whatever the runner happens to do:
 * the gate is ANDed into the entry, per side, via `applies_to`;
 * `exit_when` is a separate EXIT path whose reason string is exactly
   `"market_filter"` and which takes precedence over the document's own signal;
 * a configured filter with NO reference data FAILS CLOSED;
 * the gate reads the reference bar at or BEFORE the traded bar — never a later
   one (`searchsorted(..., "right") - 1`).

The assertions are on the actual submitted intent (symbol/side/purpose rows in
`order_intents`), not merely on a count of orders. Harness is
test_runner.py's/test_runner_session.py's (real Database + OrderGateway +
SimBroker, fake aggregator/MDS), so the gate runs through the same wiring as
every other runner behaviour.
"""

from __future__ import annotations

import copy

import pytest

from app.broker.sim import SimBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.runtime.runner import StrategyRunner
from app.strategy.schema import StrategyDocument
from tests.test_runner import MIN_MS, FakeAggregator, QuoteBook, bar_event, settle
from tests.test_runner_session import ALWAYS, NEVER, FixedMDS, et_ms, warmup_at
from tests.test_strategy import EXAMPLE

REF = "QQQ"          # reference (gate) symbol — deliberately NOT the traded one
TRADED = "SPY"

# The gate is driven by the reference symbol's raw close against a fixed level,
# so a test controls it with one number and no indicator warm-up ambiguity.
GATE_OPEN_ABOVE = {"op": "gt", "left": {"price": "close"}, "right": {"value": 100}}
GATE_EXIT_BELOW = {"op": "lt", "left": {"price": "close"}, "right": {"value": 100}}
REF_STRONG = 101.0   # gate OPEN
REF_WEAK = 99.0      # gate CLOSED (and, with exit_when set, gate EXIT)


def mf_doc(*, applies_to="both", exit_when=None, short=False, ref=REF,
           with_filter=True, exit_signal=None) -> StrategyDocument:
    """A 1m document whose own entry rule ALWAYS fires, so the gate is the only
    thing that can decide whether a trade happens."""
    data = copy.deepcopy(EXAMPLE)
    data["universe"] = {"symbols": [TRADED]}
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    data["entry"] = ({"short": {"when": ALWAYS}, "long": {"when": NEVER}} if short
                     else {"long": {"when": ALWAYS}})
    data["exit"] = {"stop_loss": {"type": "percent", "value": 2.0},
                    "take_profit": {"type": "percent", "value": 5.0}}
    if exit_signal is not None:
        data["exit"]["signal"] = {"when": exit_signal}
        if short:
            data["exit"]["signal_short"] = {"when": exit_signal}
    data["sizing"] = {"type": "percent_of_equity", "value": 10}
    data["risk"] = {"max_open_positions": 1, "max_daily_orders": 50,
                    "max_daily_loss": 100000}
    if short:
        data["risk"]["short"] = {"enabled": True, "max_short_notional": 25_000}
    data["session"] = {"flatten_eod": False}
    if with_filter:
        mf = {"symbol": ref, "indicators": [], "when": GATE_OPEN_ABOVE,
              "applies_to": applies_to}
        if exit_when is not None:
            mf["exit_when"] = exit_when
        data["market_filter"] = mf
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


# A mid-morning ET instant: inside no particular window, not near the close, so
# nothing but the gate is in play.
TS = et_ms(2026, 7, 15, 10, 30)


async def start_runner(db, broker, gateway, book, doc, *, ref_px=REF_STRONG,
                       ref_data=True, traded_px=100.0, ref_symbol=REF):
    """Runner whose traded AND reference histories both stop immediately before
    TS. `ref_data=False` gives the reference symbol NO candles at all — the
    fail-closed case."""
    frames = {TRADED: warmup_at(TS, px=traded_px)}
    if ref_data:
        frames[ref_symbol] = warmup_at(TS, px=ref_px)
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway,
                            FixedMDS(frames), agg, db)
    await runner.start()
    book.set(TRADED, bid=traded_px - 0.1, ask=traded_px + 0.1)
    return runner, agg


async def push_ref(agg, ts_ms: int, px: float, symbol: str = REF):
    await agg.push(symbol, "1m", bar_event(symbol, ts_ms, px, px + 0.5, px - 0.5, px, 1000))
    await settle()


async def push_traded(agg, ts_ms: int, px: float = 100.0):
    await agg.push(TRADED, "1m", bar_event(TRADED, ts_ms, px, px + 0.5, px - 0.5, px, 1000))
    await settle()


async def intents(db, purpose: str | None = None) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT symbol, side, purpose FROM order_intents WHERE run_id='run-1' ORDER BY id")
    return [dict(r) for r in rows if purpose is None or r["purpose"] == purpose]


async def exit_reasons(db) -> list[str]:
    import json
    rows = await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind='order' ORDER BY id")
    return [json.loads(r["payload_json"])["exit_reason"] for r in rows
            if "exit_reason" in json.loads(r["payload_json"])]


# ------------------------------------------------------------------ entry gate

async def test_entry_that_would_fire_is_blocked_when_the_gate_is_false(db, broker,
                                                                      gateway, book):
    """THE gap. The document's own rule fires; QQQ is weak; nothing may be sent."""
    runner, agg = await start_runner(db, broker, gateway, book, mf_doc(), ref_px=REF_WEAK)
    await push_traded(agg, TS)

    assert runner._positions == {}, "entered with the regime gate false"
    assert runner._pending_entries == set()
    assert await intents(db) == [], "an order was submitted despite the gate"
    await runner.stop()


async def test_the_same_entry_fires_when_the_gate_is_true(db, broker, gateway, book):
    """Identical document and identical traded bars — only the REFERENCE symbol
    differs, which is what proves the block above came from the gate."""
    runner, agg = await start_runner(db, broker, gateway, book, mf_doc(), ref_px=REF_STRONG)
    await push_traded(agg, TS)

    assert TRADED in runner._positions
    assert await intents(db, "entry") == [{"symbol": TRADED, "side": "buy",
                                          "purpose": "entry"}]
    await runner.stop()


async def test_gate_uses_bars_pushed_live_not_only_warmup(db, broker, gateway, book):
    """The reference symbol is SUBSCRIBED, so a regime flip that happens after
    start is seen: weak warm-up blocks, then a strong reference bar opens it."""
    runner, agg = await start_runner(db, broker, gateway, book, mf_doc(), ref_px=REF_WEAK)
    await push_traded(agg, TS)
    assert runner._positions == {}, "precondition: blocked while weak"

    await push_ref(agg, TS, REF_STRONG)
    await push_traded(agg, TS + MIN_MS)

    assert TRADED in runner._positions
    await runner.stop()


@pytest.mark.parametrize("applies_to,short,expect_entry", [
    ("long", False, False),   # gate false, applies to longs -> long blocked
    ("long", True, True),     # gate false, applies to longs -> short allowed
    ("short", True, False),   # gate false, applies to shorts -> short blocked
    ("short", False, True),   # gate false, applies to shorts -> long allowed
])
async def test_applies_to_gates_only_its_own_side(db, broker, gateway, book,
                                                  applies_to, short, expect_entry):
    doc = mf_doc(applies_to=applies_to, short=short)
    runner, agg = await start_runner(db, broker, gateway, book, doc, ref_px=REF_WEAK)
    await push_traded(agg, TS)

    assert (TRADED in runner._positions) is expect_entry
    if expect_entry:
        assert runner._positions[TRADED].side == ("short" if short else "long")
        assert (await intents(db, "entry"))[0]["side"] == (
            "sell_short" if short else "buy")
    else:
        assert await intents(db, "entry") == []
    await runner.stop()


# ------------------------------------------------------------------- exit_when

async def test_exit_when_closes_an_open_position_with_reason_market_filter(
        db, broker, gateway, book):
    doc = mf_doc(exit_when=GATE_EXIT_BELOW)
    runner, agg = await start_runner(db, broker, gateway, book, doc, ref_px=REF_STRONG)
    await push_traded(agg, TS)
    assert TRADED in runner._positions, "precondition: a position to close"

    await push_ref(agg, TS + MIN_MS, REF_WEAK)
    await push_traded(agg, TS + MIN_MS)

    assert runner._positions == {}, "exit_when did not close the position"
    assert "market_filter" in await exit_reasons(db)
    assert await db.fetch_one("SELECT 1 FROM positions WHERE run_id='run-1'") is None
    await runner.stop()


async def test_market_filter_exit_takes_precedence_over_the_documents_own_signal(
        db, broker, gateway, book):
    """Both exits fire on the same bar. The engine checks the gate FIRST so the
    reason is attributable to the regime, not to the rule — same here."""
    doc = mf_doc(exit_when=GATE_EXIT_BELOW, exit_signal=ALWAYS)
    runner, agg = await start_runner(db, broker, gateway, book, doc, ref_px=REF_STRONG)
    await push_traded(agg, TS)
    assert TRADED in runner._positions

    await push_ref(agg, TS + MIN_MS, REF_WEAK)
    await push_traded(agg, TS + MIN_MS)

    assert runner._positions == {}
    reasons = await exit_reasons(db)
    assert reasons == ["market_filter"], f"expected the gate to own the exit, got {reasons}"
    await runner.stop()


async def test_no_exit_when_means_a_closed_gate_never_closes_a_position(
        db, broker, gateway, book):
    """`when` going false must NOT close anything — `exit_when` is a separate
    condition precisely so a filter can carry hysteresis."""
    runner, agg = await start_runner(db, broker, gateway, book, mf_doc(), ref_px=REF_STRONG)
    await push_traded(agg, TS)
    assert TRADED in runner._positions

    await push_ref(agg, TS + MIN_MS, REF_WEAK)
    await push_traded(agg, TS + MIN_MS)

    assert TRADED in runner._positions, "an entry gate must not double as an exit"
    assert "market_filter" not in await exit_reasons(db)
    await runner.stop()


# ----------------------------------------------------------------- fail closed

async def test_missing_reference_data_blocks_every_entry(db, broker, gateway, book):
    """A configured filter with no reference bars FAILS CLOSED. Trading
    unfiltered because the reference feed is missing defeats the whole gate."""
    runner, agg = await start_runner(db, broker, gateway, book, mf_doc(), ref_data=False)
    await push_traded(agg, TS)
    await push_traded(agg, TS + MIN_MS)

    assert runner._positions == {}
    assert await intents(db) == []
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM run_events "
        "WHERE run_id='run-1' AND kind='market_filter_no_data'")
    assert row["n"] == 1, "the no-data warning must be logged once, not once per bar"
    await runner.stop()


async def test_no_market_filter_leaves_the_entry_path_untouched(db, broker, gateway, book):
    """Control: the same document without a filter trades, so the blocks above
    are the gate and not a broken entry path."""
    runner, agg = await start_runner(db, broker, gateway, book, mf_doc(with_filter=False),
                                     ref_data=False)
    await push_traded(agg, TS)

    assert TRADED in runner._positions
    await runner.stop()


# ---------------------------------------------------------------- no lookahead

async def test_a_reference_bar_after_the_traded_bar_is_not_used(db, broker, gateway, book):
    """The engine takes the reference bar at or BEFORE the traded timestamp. A
    LATER reference bar (which live can genuinely deliver first, e.g. a symbol
    whose feed runs ahead) must not be consulted: that would be look-ahead."""
    runner, agg = await start_runner(db, broker, gateway, book, mf_doc(), ref_px=REF_WEAK)
    # a strong reference bar stamped one minute AFTER the traded bar we push next
    await push_ref(agg, TS + MIN_MS, REF_STRONG)
    await push_traded(agg, TS)

    assert runner._positions == {}, "the gate read a reference bar from the future"
    # and the same strong bar DOES open the gate once the traded bar catches up
    await push_traded(agg, TS + MIN_MS)
    assert TRADED in runner._positions
    await runner.stop()


# ------------------------------------------------ reference symbol is not traded

async def test_reference_symbol_is_subscribed_and_unsubscribed(db, broker, gateway, book):
    runner, agg = await start_runner(db, broker, gateway, book, mf_doc())
    assert (REF, "1m") in agg.registered, "the gate's reference symbol was never subscribed"

    await runner.stop()
    assert (REF, "1m") not in agg.registered


async def test_reference_symbol_in_the_universe_is_registered_once(db, broker, gateway, book):
    """A reference symbol that is ALSO traded must not be double-registered —
    `stop()` unregisters it once, so a second registration would leak a live
    subscription."""
    doc = mf_doc(ref=TRADED)
    runner, agg = await start_runner(db, broker, gateway, book, doc, ref_px=REF_STRONG,
                                     ref_symbol=TRADED, traded_px=REF_STRONG)
    assert sorted(agg.registered) == [(TRADED, "1m")]

    # and it still works as BOTH: the bar both feeds the gate and trades
    await push_traded(agg, TS, px=REF_STRONG)
    assert TRADED in runner._positions

    await runner.stop()
    assert agg.registered == {}


async def test_a_reference_only_bar_never_trades_and_never_rolls_the_day(
        db, broker, gateway, book):
    """A reference bar is a DATA update, not a clock tick: no position, no
    intent, and no per-day counter reset (the engine's timeline is built from
    `universe.symbols` only, so its reference frame cannot roll the day either)."""
    runner, agg = await start_runner(db, broker, gateway, book, mf_doc())
    await push_traded(agg, TS)
    assert runner._entries_today.get(TRADED) == 1
    day_before = runner._day

    # a reference bar on the NEXT ET day must not reset the entry counters
    await push_ref(agg, et_ms(2026, 7, 16, 10, 30), REF_STRONG)

    assert REF not in runner._positions
    assert runner._entries_today.get(TRADED) == 1, "a reference bar rolled the day"
    assert runner._day == day_before
    assert [i["symbol"] for i in await intents(db)] == [TRADED] * len(await intents(db))
    assert runner._mf_candles is not None and len(runner._mf_candles) == 11, \
        "the reference bar must still have landed in the gate's frame"
    await runner.stop()
