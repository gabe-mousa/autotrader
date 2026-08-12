"""StrategyRunner integration tests — real Database + OrderGateway + SimBroker
(each already unit-tested standalone), with a FakeAggregator/FakeMDS so tests
drive bar events directly without needing a real streamer. Exercises the full
entry -> fill -> protective-stop-placement -> stop-fill -> position-closed
chain, plus signal exits, cooldown, pause, and the daily-loss-never-blocks-
exits property carried through from the gateway."""

from __future__ import annotations

import asyncio
import copy
import json
import time

import numpy as np
import pandas as pd
import pytest

from app.broker.sim import SimBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.runtime.bar_aggregator import BarEvent
from app.runtime.runner import ET, StrategyRunner
from app.strategy.schema import StrategyDocument
from tests.test_strategy import EXAMPLE

MIN_MS = 60_000


class FakeAggregator:
    def __init__(self):
        self.registered: dict[tuple[str, str], object] = {}

    async def register(self, symbol, tf, callback):
        self.registered[(symbol, tf)] = callback

    async def unregister(self, symbol, tf, callback):
        self.registered.pop((symbol, tf), None)

    async def push(self, symbol, tf, event: BarEvent):
        await self.registered[(symbol, tf)](event)


class FakeMDS:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames

    async def get_candles(self, symbol, tf, start_ms, end_ms):
        df = self.frames.get(symbol, pd.DataFrame())
        if df.empty:
            return df
        lo = pd.Timestamp(start_ms, unit="ms", tz="UTC")
        hi = pd.Timestamp(end_ms, unit="ms", tz="UTC")
        return df[(df.index >= lo) & (df.index <= hi)]


class QuoteBook:
    def __init__(self):
        self.quotes: dict[str, dict] = {}

    def get(self, symbol):
        return self.quotes.get(symbol)

    def set(self, symbol, bid, ask, last=None):
        self.quotes[symbol] = {"bid": bid, "ask": ask, "last": last if last is not None else bid}


def atr_stop_doc() -> StrategyDocument:
    """Same shape as rsi_doc() but with atr_multiple stop_loss/take_profit —
    regression coverage for the atr_value=None-at-fill-time bug."""
    data = copy.deepcopy(EXAMPLE)
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = [
        {"id": "rsi14", "type": "rsi", "params": {"period": 3}},
        {"id": "atr3", "type": "atr", "params": {"period": 3}},
    ]
    data["entry"] = {"long": {"when": {
        "op": "lt", "left": {"ind": "rsi14"}, "right": {"value": 200}}}}
    data["exit"] = {
        "stop_loss": {"type": "atr_multiple", "value": 2.0, "atr_indicator": "atr3"},
        "take_profit": {"type": "atr_multiple", "value": 3.0, "atr_indicator": "atr3"},
    }
    data["sizing"] = {"type": "percent_of_equity", "value": 10}
    data["risk"] = {"max_open_positions": 1, "max_daily_orders": 50, "max_daily_loss": 100000}
    return StrategyDocument.model_validate(data)


# Margin, in bars, for the extra bars a test pushes AFTER the warm-up frame.
# No test pushes anywhere near this many.
_ET_ROLL_MARGIN_BARS = 10


def _safe_base_ms(n: int) -> int:
    """Anchor a warm-up frame near the current wall clock, but never across an
    ET date boundary.

    Bars are anchored to "now" so a runner under test sees plausibly recent
    history. The unguarded version of this straddled ET midnight whenever the
    suite ran within ~n minutes of 00:00 ET: `_maybe_roll_day` then fired
    part-way through a test's pushed bars and cleared `_entries_today` /
    `_orders_today`, silently defeating every assertion about a per-day
    counter. That is LIVING-DOC:64's long-unexplained "time-of-day-dependent
    test failure" — it reproduces exactly at 00:00 ET and nowhere else, which
    is why nobody ever caught it in the act. Confirmed 2026-07-30 00:00 EDT.

    So: keep the now-anchor (every existing test's session-clock expectations
    depend on it) and only slide it forward to ET midnight when the frame plus
    that margin would cross into the next ET day."""
    base_ms = (int(time.time() * 1000) // MIN_MS - n) * MIN_MS
    span_ms = (n + _ET_ROLL_MARGIN_BARS) * MIN_MS
    start = pd.Timestamp(base_ms, unit="ms", tz="UTC").tz_convert(ET)
    end = pd.Timestamp(base_ms + span_ms, unit="ms", tz="UTC").tz_convert(ET)
    if start.date() == end.date():
        return base_ms
    return int(end.normalize().tz_convert("UTC").timestamp() * 1000)


def warmup_df(n=15, start_px=100.0, freq="1min", base_ms=None):
    base_ms = base_ms if base_ms is not None else _safe_base_ms(n)
    idx = pd.to_datetime([base_ms + i * MIN_MS for i in range(n)], unit="ms", utc=True)
    closes = np.full(n, start_px)
    return pd.DataFrame({"open": closes, "high": closes + 0.1, "low": closes - 0.1,
                         "close": closes, "volume": np.full(n, 100)}, index=idx)


def rsi_doc(**overrides) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    data["entry"] = {"long": {"when": {
        "op": "lt", "left": {"ind": "rsi14"}, "right": {"value": 200}}}}  # always true (RSI max is 100;
    # flat warmup data produces RSI==100 exactly, so a "<100" threshold would never fire)
    data["exit"] = {"stop_loss": {"type": "percent", "value": 2.0},
                    "take_profit": {"type": "percent", "value": 5.0}}
    data["sizing"] = {"type": "percent_of_equity", "value": 10}
    data["risk"] = {"max_open_positions": 1, "max_daily_orders": 50, "max_daily_loss": 100000}
    data.update(overrides)
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


async def settle():
    """Let a multi-hop async chain fully resolve: SimBroker's fill task ->
    emit -> runner's queue -> runner's _run_loop iteration. A bare
    asyncio.sleep(0) only yields ONE scheduling step, not enough for a
    multi-hop chain across two separate tasks; tiny REAL sleeps let the
    event loop drain everything ready each iteration."""
    for _ in range(10):
        await asyncio.sleep(0.01)


def bar_event(symbol, ts, o, h, lo, c, v):
    return BarEvent(symbol=symbol, tf="1m", ts=ts, open=o, high=h, low=lo, close=c, volume=v)


# ---- full entry -> fill -> protective stops chain ---------------------------

async def test_entry_fills_and_places_resting_stops(db, broker, gateway, book):
    doc = rsi_doc()
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    await runner.start()

    book.set("SPY", bid=99.9, ask=100.1)
    next_ts = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", next_ts, 100, 100.5, 99.5, 100, 1000))
    await settle()

    assert "SPY" in runner._positions
    pos = runner._positions["SPY"]
    assert pos.stop_broker_order_id is not None
    assert pos.target_broker_order_id is not None
    # stop at 2% below fill, target at 5% above
    working = broker._working
    stop_order = working[pos.stop_broker_order_id]
    target_order = working[pos.target_broker_order_id]
    assert stop_order.req.stop_px == pytest.approx(pos.avg_price * 0.98)
    assert target_order.req.limit_px == pytest.approx(pos.avg_price * 1.05)

    row = await db.fetch_one("SELECT * FROM positions WHERE run_id='run-1' AND symbol='SPY'")
    assert row is not None and row["qty"] == pos.qty
    await runner.stop()


async def test_atr_multiple_stop_places_a_real_price_after_fill(db, broker, gateway, book):
    """Regression: _on_entry_filled used to call compute_stop_price(...,
    atr_value=None, ...) unconditionally, so ANY atr_multiple stop_loss/
    take_profit priced out as None at fill time, which _place_protective
    treats as "skip" — then the "a configured protective order must place"
    invariant raised, crashing the runner to ERROR on the very first fill.
    No existing test used atr_multiple for the runner (only percent), so it
    shipped unnoticed."""
    doc = atr_stop_doc()
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    await runner.start()

    book.set("SPY", bid=99.9, ask=100.1)
    next_ts = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", next_ts, 100, 100.5, 99.5, 100, 1000))
    await settle()

    assert runner.state != "ERROR"
    assert "SPY" in runner._positions
    pos = runner._positions["SPY"]
    assert pos.stop_broker_order_id is not None
    assert pos.target_broker_order_id is not None
    stop_order = broker._working[pos.stop_broker_order_id]
    target_order = broker._working[pos.target_broker_order_id]
    assert stop_order.req.stop_px is not None and stop_order.req.stop_px < pos.avg_price
    assert target_order.req.limit_px is not None and target_order.req.limit_px > pos.avg_price
    await runner.stop()


async def test_stop_fill_closes_position_and_cancels_target(db, broker, gateway, book):
    doc = rsi_doc()
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    await runner.start()

    book.set("SPY", bid=99.9, ask=100.1)
    ts0 = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", ts0, 100, 100.5, 99.5, 100, 1000))
    await settle()
    pos = runner._positions["SPY"]
    target_id = pos.target_broker_order_id

    # price crashes through the stop
    await broker.process_tick("SPY", bid=95, ask=95.5, last=95)
    await settle()

    assert "SPY" not in runner._positions
    assert target_id not in broker._working  # sibling target canceled
    row = await db.fetch_one("SELECT * FROM positions WHERE run_id='run-1' AND symbol='SPY'")
    assert row is None
    events = await db.fetch_all("SELECT kind, payload_json FROM run_events WHERE run_id='run-1'")
    kinds = [e["kind"] for e in events]
    assert "fill" in kinds
    await runner.stop()


async def test_daily_loss_never_blocks_a_protective_exit(db, broker, gateway, book):
    """The gateway's own guarantee (exits never blocked by daily-loss breach)
    must hold end-to-end through the runner too."""
    doc = rsi_doc()
    doc.risk.max_daily_loss = 1.0  # trivially breached immediately
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    await runner.start()

    book.set("SPY", bid=99.9, ask=100.1)
    ts0 = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", ts0, 100, 100.5, 99.5, 100, 1000))
    await settle()
    assert "SPY" in runner._positions  # entry succeeded before any loss accrued

    await broker.process_tick("SPY", bid=95, ask=95.5, last=95)  # triggers the stop
    await settle()
    assert "SPY" not in runner._positions, "protective stop must fire even after daily-loss breach"
    await runner.stop()


# ---- signal exit --------------------------------------------------------------

async def test_signal_exit_cancels_both_resting_orders(db, broker, gateway, book):
    data = rsi_doc()
    data.exit.signal = None
    # rebuild with a real signal exit: rsi > -1 (always true) triggers immediate exit next bar
    from app.strategy.schema import Exit, RuleBlock, StopSpec
    data.exit = Exit(
        signal=RuleBlock(when={"op": "gt", "left": {"ind": "rsi14"}, "right": {"value": -1}}),
        stop_loss=StopSpec(type="percent", value=2.0),
        take_profit=StopSpec(type="percent", value=5.0),
    )
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", data, "paper", broker, gateway, mds, agg, db)
    await runner.start()

    book.set("SPY", bid=99.9, ask=100.1)
    ts0 = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", ts0, 100, 100.5, 99.5, 100, 1000))
    await settle()
    assert "SPY" in runner._positions
    stop_id = runner._positions["SPY"].stop_broker_order_id
    target_id = runner._positions["SPY"].target_broker_order_id

    # next bar close: exit signal fires (rsi>-1 always true) -> market exit
    book.set("SPY", bid=100.9, ask=101.1)
    ts1 = ts0 + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", ts1, 101, 101.5, 100.5, 101, 1000))
    await settle()

    assert "SPY" not in runner._positions
    assert stop_id not in broker._working
    assert target_id not in broker._working
    await runner.stop()


# ---- pause / cooldown ----------------------------------------------------------

async def test_paused_runner_places_no_new_entries(db, broker, gateway, book):
    doc = rsi_doc()
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    await runner.start()
    await runner.pause()
    await settle()
    assert runner.state == "PAUSED"

    book.set("SPY", bid=99.9, ask=100.1)
    ts0 = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", ts0, 100, 100.5, 99.5, 100, 1000))
    await settle()
    assert "SPY" not in runner._positions
    await runner.stop()


async def test_flatten_closes_open_position(db, broker, gateway, book):
    doc = rsi_doc()
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    await runner.start()
    book.set("SPY", bid=99.9, ask=100.1)
    ts0 = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", ts0, 100, 100.5, 99.5, 100, 1000))
    await settle()
    assert "SPY" in runner._positions

    await runner.flatten()
    await settle()
    assert "SPY" not in runner._positions
    await runner.stop()


# ---- crash -> ERROR state, stops resting orders untouched -----------------------

async def test_runner_error_state_never_touches_resting_stops(db, broker, gateway, book, monkeypatch):
    doc = rsi_doc()
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    await runner.start()
    book.set("SPY", bid=99.9, ask=100.1)
    ts0 = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", ts0, 100, 100.5, 99.5, 100, 1000))
    await settle()
    stop_id = runner._positions["SPY"].stop_broker_order_id
    assert stop_id in broker._working

    def boom(*a, **kw):  # _maybe_roll_day is sync — match its real signature
        raise RuntimeError("simulated bug")
    monkeypatch.setattr(runner, "_maybe_roll_day", boom)

    ts1 = ts0 + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", ts1, 101, 101.5, 100.5, 101, 1000))
    await settle()
    assert runner.state == "ERROR"
    assert stop_id in broker._working, "a runtime crash must never cancel resting protective orders"


# ---- scaled/partial exits (exit.scale_outs) ---------------------------------

def scale_out_doc() -> StrategyDocument:
    """Fixed-share sizing so the ladder's share maths is exact: 100 shares,
    50% off at +2%, remainder to the +5% take profit."""
    data = copy.deepcopy(EXAMPLE)
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    data["entry"] = {"long": {"when": {
        "op": "lt", "left": {"ind": "rsi14"}, "right": {"value": 200}}}}
    data["exit"] = {
        "stop_loss": {"type": "percent", "value": 2.0},
        "scale_outs": [{"target": {"type": "percent", "value": 2.0}, "percent": 50}],
        "take_profit": {"type": "percent", "value": 5.0},
    }
    data["sizing"] = {"type": "fixed_shares", "value": 100,
                      "max_position_notional": 1_000_000}
    data["risk"] = {"max_open_positions": 1, "max_daily_orders": 50, "max_daily_loss": 100000}
    return StrategyDocument.model_validate(data)


async def _open_scaled_position(db, broker, gateway, book):
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", scale_out_doc(), "paper", broker, gateway, mds, agg, db)
    await runner.start()
    book.set("SPY", bid=99.9, ask=100.1)
    ts = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", ts, 100, 100.5, 99.5, 100, 1000))
    await settle()
    return runner


async def test_scale_out_levels_rest_alongside_stop_and_target(db, broker, gateway, book):
    runner = await _open_scaled_position(db, broker, gateway, book)
    pos = runner._positions["SPY"]
    assert pos.qty == 100
    assert len(pos.scale_order_qty) == 1

    scale_id, shares = next(iter(pos.scale_order_qty.items()))
    assert shares == 50
    scale_order = broker._working[scale_id]
    assert scale_order.req.order_type == "limit"
    assert scale_order.req.limit_px == pytest.approx(pos.avg_price * 1.02)
    assert scale_order.req.qty == 50

    # the final target claims only the REMAINDER — the resting orders must
    # never sum to more than the position
    target_order = broker._working[pos.target_broker_order_id]
    assert target_order.req.qty == 50
    assert target_order.req.limit_px == pytest.approx(pos.avg_price * 1.05)
    assert broker._working[pos.stop_broker_order_id].req.qty == 100
    await runner.stop()


async def test_scale_out_fill_reduces_position_and_resizes_the_stop(db, broker, gateway, book):
    runner = await _open_scaled_position(db, broker, gateway, book)
    pos = runner._positions["SPY"]
    entry_px, old_stop_id = pos.avg_price, pos.stop_broker_order_id

    # tick through the +2% scale level -> the resting partial limit fills
    await broker.process_tick("SPY", bid=entry_px * 1.03, ask=entry_px * 1.031,
                              last=entry_px * 1.03)
    await settle()

    assert "SPY" in runner._positions, "a partial exit must leave the position OPEN"
    pos = runner._positions["SPY"]
    assert pos.qty == 50
    assert pos.scale_order_qty == {}

    # the stop was sized for 100 shares; leaving it would try to sell more
    # than is now held, so it must have been replaced at the reduced quantity
    assert pos.stop_broker_order_id != old_stop_id
    assert old_stop_id not in broker._working
    new_stop = broker._working[pos.stop_broker_order_id]
    assert new_stop.req.qty == 50
    assert new_stop.req.stop_px == pytest.approx(entry_px * 0.98)

    row = await db.fetch_one("SELECT * FROM positions WHERE run_id='run-1' AND symbol='SPY'")
    assert row["qty"] == 50
    assert runner.state != "ERROR"
    await runner.stop()


async def test_full_exit_cancels_unfilled_scale_levels(db, broker, gateway, book):
    """A stop closing the position leaves the higher scale level resting —
    it would sell shares that are no longer held."""
    runner = await _open_scaled_position(db, broker, gateway, book)
    pos = runner._positions["SPY"]
    scale_id = next(iter(pos.scale_order_qty))
    entry_px = pos.avg_price

    # drop through the stop instead of rallying into the scale level
    await broker.process_tick("SPY", bid=entry_px * 0.97, ask=entry_px * 0.971,
                              last=entry_px * 0.97)
    await settle()

    assert "SPY" not in runner._positions
    assert scale_id not in broker._working, "unfilled scale level left resting after a full exit"
    assert runner.state != "ERROR"
    await runner.stop()
