"""Audit A3 — cross-sectional `selection` (top-N) in the LIVE/paper runner.

Why this file exists: `selection` was honoured by the backtest engine and the
string did not appear in `runtime/runner.py` at all. Live, whichever symbol's bar
happened to arrive first and pass `entry_signal` was traded, up to
`max_open_positions`. For the running 81-symbol paper run
(`r4-streak-reversion-core-81-names`) that is close to random selection, and its
paper P&L was therefore not comparable to its own backtest.

`selection` is also the only rule in the schema that CANNOT be decided from a
single symbol's bar, which is the real cost of the item: the runner is
per-symbol event-driven, so when the first bar for a timestamp arrives no other
symbol has reported it yet. The runner answers that with a barrier, and the
properties that matter are therefore as much about the barrier as the ranking:

 * exactly the correct top-N enter and the rest get NOTHING (asserted on the
   submitted intents' symbols, not on an order count);
 * `order` picks the opposite end of the ranking;
 * ties break on SYMBOL NAME, and the outcome does not depend on bar arrival
   order — the property that catches a barrier which decides early;
 * a NaN (warm-up) metric is SKIPPED, never ranked as zero;
 * selection gates ENTRIES only: a position already open in a symbol that has
   dropped out of the top-N is not closed;
 * a timestamp that one symbol never reports still resolves, off the later bar
   that proves the timestamp is over;
 * a document with no `selection` keeps its exact pre-A3 behaviour.

Harness is test_runner.py's / test_runner_session.py's, same as the A4 tests.
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
from tests.test_runner_session import ALWAYS, FixedMDS, et_ms, warmup_at
from tests.test_strategy import EXAMPLE

# Four symbols, deliberately NOT in alphabetical == ranking order, so a test
# that passes by accident of iteration order is unlikely.
UNIVERSE = ["AAA", "BBB", "CCC", "DDD"]

# The ranking metric is the symbol's own close: one number per symbol controls
# the entire cross-section, with no indicator warm-up ambiguity in the way.
CLOSE_METRIC = {"price": "close"}

TS = et_ms(2026, 7, 15, 10, 30)     # mid-morning ET: no window/EOD interference


def sel_doc(*, top_n=2, order="desc", metric=None, with_selection=True,
            max_open_positions=10, indicators=None, exit_signal=None) -> StrategyDocument:
    """A 1m document whose own entry rule ALWAYS fires, so `selection` is the
    only thing that can decide which symbols trade."""
    data = copy.deepcopy(EXAMPLE)
    data["universe"] = {"symbols": list(UNIVERSE)}
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = ([{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
                          + list(indicators or []))
    data["entry"] = {"long": {"when": ALWAYS}}
    data["exit"] = {"stop_loss": {"type": "percent", "value": 2.0},
                    "take_profit": {"type": "percent", "value": 5.0}}
    if exit_signal is not None:
        data["exit"]["signal"] = {"when": exit_signal}
    data["sizing"] = {"type": "percent_of_equity", "value": 5}
    data["risk"] = {"max_open_positions": max_open_positions, "max_daily_orders": 100,
                    "max_daily_loss": 100000}
    data["session"] = {"flatten_eod": False}
    if with_selection:
        data["selection"] = {"metric": metric or CLOSE_METRIC, "top_n": top_n,
                             "order": order}
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


async def start_runner(db, broker, gateway, book, doc, *, warmup=None):
    """`warmup` maps symbol -> bar count, so one symbol can be given enough
    history to evaluate a long-period indicator while the others cannot."""
    warmup = warmup or {}
    frames = {s: warmup_at(TS, n=warmup.get(s, 10), px=100.0) for s in UNIVERSE}
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway,
                            FixedMDS(frames), agg, db)
    await runner.start()
    for symbol in UNIVERSE:
        book.set(symbol, bid=99.9, ask=100.1)
    return runner, agg


async def push(agg, symbol: str, ts_ms: int, px: float) -> None:
    await agg.push(symbol, "1m", bar_event(symbol, ts_ms, px, px + 0.5, px - 0.5, px, 1000))
    await settle()


async def push_cross_section(agg, ts_ms: int, closes: dict[str, float],
                             order: list[str] | None = None) -> None:
    """One bar per symbol at the same timestamp, in `order` (default: the
    dict's own order). The LAST one completes the barrier."""
    for symbol in (order or list(closes)):
        await push(agg, symbol, ts_ms, closes[symbol])


async def entered(db) -> list[str]:
    rows = await db.fetch_all(
        "SELECT symbol, purpose FROM order_intents WHERE run_id='run-1' ORDER BY id")
    return sorted(r["symbol"] for r in rows if r["purpose"] == "entry")


# ------------------------------------------------------------------ the ranking

async def test_only_the_top_n_by_metric_are_entered(db, broker, gateway, book):
    """THE gap: every symbol's rule fires, only the ranked top 2 may trade."""
    runner, agg = await start_runner(db, broker, gateway, book, sel_doc(top_n=2))
    await push_cross_section(agg, TS, {"AAA": 101.0, "BBB": 104.0,
                                       "CCC": 99.0, "DDD": 103.0})

    assert await entered(db) == ["BBB", "DDD"]
    assert sorted(runner._positions) == ["BBB", "DDD"]
    await runner.stop()


async def test_order_asc_selects_the_other_end_of_the_ranking(db, broker, gateway, book):
    runner, agg = await start_runner(db, broker, gateway, book,
                                     sel_doc(top_n=2, order="asc"))
    await push_cross_section(agg, TS, {"AAA": 101.0, "BBB": 104.0,
                                       "CCC": 99.0, "DDD": 103.0})

    assert await entered(db) == ["AAA", "CCC"]
    await runner.stop()


async def test_top_n_of_one_takes_the_single_best(db, broker, gateway, book):
    runner, agg = await start_runner(db, broker, gateway, book, sel_doc(top_n=1))
    await push_cross_section(agg, TS, {"AAA": 101.0, "BBB": 104.0,
                                       "CCC": 99.0, "DDD": 103.0})

    assert await entered(db) == ["BBB"]
    await runner.stop()


# ------------------------------------------- determinism (the barrier's real job)

@pytest.mark.parametrize("arrival", [
    ["AAA", "BBB", "CCC", "DDD"],
    ["DDD", "CCC", "BBB", "AAA"],
    ["CCC", "AAA", "DDD", "BBB"],
])
async def test_outcome_is_independent_of_bar_arrival_order(db, broker, gateway, book,
                                                           arrival):
    """The property that fails if the barrier decides on the first bar it sees:
    ranking then scores one fresh symbol against three stale ones, and the
    winner becomes whichever bar the aggregator happened to deliver first."""
    runner, agg = await start_runner(db, broker, gateway, book, sel_doc(top_n=2))
    await push_cross_section(agg, TS, {"AAA": 101.0, "BBB": 104.0,
                                       "CCC": 99.0, "DDD": 103.0}, order=arrival)

    assert await entered(db) == ["BBB", "DDD"]
    await runner.stop()


@pytest.mark.parametrize("arrival", [["AAA", "BBB", "CCC", "DDD"],
                                     ["DDD", "CCC", "BBB", "AAA"]])
async def test_a_tie_breaks_on_symbol_name_not_arrival_order(db, broker, gateway, book,
                                                             arrival):
    """AAA and BBB tie exactly; top_n=1 forces the tiebreak to decide. The engine
    sorts on `(-value, symbol)`, so AAA wins — from either arrival order."""
    runner, agg = await start_runner(db, broker, gateway, book, sel_doc(top_n=1))
    await push_cross_section(agg, TS, {"AAA": 104.0, "BBB": 104.0,
                                       "CCC": 99.0, "DDD": 100.0}, order=arrival)

    assert await entered(db) == ["AAA"]
    await runner.stop()


async def test_nothing_is_entered_before_the_universe_has_reported(db, broker,
                                                                   gateway, book):
    """Deferral itself: three of four bars in, the cross-section is incomplete,
    so no entry may have been decided yet."""
    runner, agg = await start_runner(db, broker, gateway, book, sel_doc(top_n=2))
    for symbol in ["AAA", "BBB", "CCC"]:
        await push(agg, symbol, TS, 104.0 if symbol == "AAA" else 99.0)

    assert await entered(db) == []
    assert runner._positions == {}

    await push(agg, "DDD", TS, 103.0)
    assert await entered(db) == ["AAA", "DDD"]
    await runner.stop()


# --------------------------------------------------------------- forced flush

async def test_a_timestamp_one_symbol_never_reports_still_resolves(db, broker,
                                                                   gateway, book):
    """A halted or thin name never prints a bar for TS. The barrier must not wait
    forever: the later bar proves TS is over and the ranking runs over the
    symbols that DID report — the same set `engine.selected_at` would score,
    since it skips any symbol with no bar at that timestamp."""
    runner, agg = await start_runner(db, broker, gateway, book, sel_doc(top_n=2))
    await push(agg, "AAA", TS, 101.0)
    await push(agg, "BBB", TS, 104.0)
    await push(agg, "CCC", TS, 99.0)
    assert await entered(db) == [], "decided before TS was known to be over"

    # DDD is silent at TS; the next bar's arrival forces the flush for TS.
    await push(agg, "AAA", TS + MIN_MS, 101.0)

    assert await entered(db) == ["AAA", "BBB"], "wrong ranking over the reporters"
    await runner.stop()


async def test_a_forced_flush_evaluates_the_ranked_bar_not_the_newest(db, broker,
                                                                     gateway, book):
    """The flush is triggered by a LATER bar, but the entry belongs to the bar
    that was ranked. CCC's entry must be priced/evaluated off its TS bar."""
    runner, agg = await start_runner(db, broker, gateway, book, sel_doc(top_n=1))
    await push(agg, "CCC", TS, 108.0)          # clear winner at TS
    await push(agg, "AAA", TS, 99.0)
    await push(agg, "BBB", TS, 99.0)
    await push(agg, "CCC", TS + MIN_MS, 90.0)  # forces the flush for TS

    assert await entered(db) == ["CCC"]
    assert runner._positions["CCC"].qty > 0
    await runner.stop()


# ------------------------------------------------------------------ NaN / warm-up

async def test_a_nan_metric_is_skipped_not_ranked_as_zero(db, broker, gateway, book):
    """Only AAA has enough history for a 50-period SMA; the other three score
    NaN. A NaN treated as zero would rank them (and with `asc` would WIN), so
    the correct outcome is that AAA is the only symbol ranked at all — even
    though top_n=2 has room for another."""
    doc = sel_doc(top_n=2, order="asc", metric={"ind": "sma50"},
                  indicators=[{"id": "sma50", "type": "sma", "params": {"period": 50}}])
    runner, agg = await start_runner(db, broker, gateway, book, doc,
                                     warmup={"AAA": 60})
    await push_cross_section(agg, TS, {"AAA": 101.0, "BBB": 104.0,
                                       "CCC": 99.0, "DDD": 103.0})

    assert await entered(db) == ["AAA"]
    await runner.stop()


async def test_an_all_nan_cross_section_enters_nothing(db, broker, gateway, book):
    """`engine.selected_at` returns an empty SET (not None) when nothing scored,
    which blocks every entry. None would mean "unrestricted" and let all four in
    — the one-character difference this test exists to pin down."""
    doc = sel_doc(top_n=2, metric={"ind": "sma50"},
                  indicators=[{"id": "sma50", "type": "sma", "params": {"period": 50}}])
    runner, agg = await start_runner(db, broker, gateway, book, doc)
    await push_cross_section(agg, TS, {"AAA": 101.0, "BBB": 104.0,
                                       "CCC": 99.0, "DDD": 103.0})

    assert await entered(db) == []
    await runner.stop()


# ------------------------------------------------------- entries only, not exits

async def test_dropping_out_of_the_top_n_does_not_close_a_position(db, broker,
                                                                  gateway, book):
    """`selection` gates ENTRIES only — the engine's `eligible` check sits in its
    entry branch. A position whose symbol has since fallen out of the ranking
    keeps running and exits on its own stop/target/signal."""
    runner, agg = await start_runner(db, broker, gateway, book, sel_doc(top_n=1))
    await push_cross_section(agg, TS, {"AAA": 108.0, "BBB": 99.0,
                                       "CCC": 99.0, "DDD": 99.0})
    assert sorted(runner._positions) == ["AAA"]

    # AAA is now the WORST name in the cross-section.
    await push_cross_section(agg, TS + MIN_MS, {"AAA": 100.0, "BBB": 107.0,
                                               "CCC": 106.0, "DDD": 105.0})

    assert "AAA" in runner._positions, "selection closed a position it only gates entries for"
    exits = await db.fetch_all(
        "SELECT symbol FROM order_intents WHERE run_id='run-1' AND purpose='exit'")
    assert [r["symbol"] for r in exits] == []
    await runner.stop()


# ------------------------------------------------ the no-selection path is intact

async def test_a_document_without_selection_enters_on_its_own_bar_immediately(
        db, broker, gateway, book):
    """The majority case must take exactly its pre-A3 path: one symbol's bar is
    decided on arrival, with no barrier and no waiting for the universe."""
    runner, agg = await start_runner(db, broker, gateway, book,
                                     sel_doc(with_selection=False))
    await push(agg, "CCC", TS, 99.0)

    assert await entered(db) == ["CCC"]
    assert runner._sel_reported == {} and runner._sel_candidates == {}
    await runner.stop()
