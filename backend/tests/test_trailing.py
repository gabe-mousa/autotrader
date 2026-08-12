"""`exit.trailing_stop`, including the optional `activate_at` arming gate.

The properties that matter, and why each is tested rather than assumed:

* A trail RATCHETS. It may only ever tighten (up for a long, down for a
  short); a retrace must never widen it back out, or the "trailing" stop is
  just a stop that moves around.
* A GATED trail contributes NOTHING before it arms. If a dormant trail
  quietly rested a trail-distance from entry it would exit positions the
  strategy explicitly asked to leave alone until they had run.
* Arming happens AFTER the bar's exit checks, so the bar that arms a trail
  can never also be stopped out by it — otherwise the gate could produce an
  instant exit on the very bar it was reached.
* The LIVE runner's resting broker stop is genuinely AMENDED when the ratchet
  moves, at the right price, without consuming `risk.max_daily_orders` (a
  ratcheting stop that burned the daily budget would eventually block a real
  exit), and a failed re-place halts rather than continuing unprotected.
* Crash recovery rebuilds the trail from reloaded candle history rather than
  resetting it to entry (regression for a missing `_recover_trail`, which used
  to AttributeError on every recovery path).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import BacktestConfig, run_backtest
from app.broker.sim import SimBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.runtime.runner import StrategyRunner
from app.strategy.lint import lint
from app.strategy.schema import StrategyDocument
from tests.test_runner import (MIN_MS, FakeAggregator, FakeMDS, QuoteBook, bar_event,
                               rsi_doc, settle, warmup_df)

# =============================================================================
# backtest engine
# =============================================================================


def bars(opens, highs, lows, closes):
    n = len(closes)
    idx = pd.date_range("2024-01-02 14:30", periods=n, freq="30min", tz="UTC")
    return pd.DataFrame({
        "open": np.asarray(opens, float), "high": np.asarray(highs, float),
        "low": np.asarray(lows, float), "close": np.asarray(closes, float),
        "volume": np.full(n, 1000.0)}, index=idx)


def doc(*, side="long", trail=10.0, activate_at=None, stop=50.0, trigger=100.0):
    trailing = {"type": "percent", "value": trail}
    if activate_at is not None:
        trailing["activate_at"] = {"type": "percent", "value": activate_at}
    d = {
        "meta": {"name": "trail"}, "universe": {"symbols": ["X"]},
        "timeframe": {"bar": "30m"}, "indicators": [],
        "entry": {"cooldown_bars": 0, "max_entries_per_day": 50},
        "exit": {"trailing_stop": trailing},
        "sizing": {"type": "percent_of_equity", "value": 10.0,
                   "max_position_notional": 1e9},
        "risk": {"max_open_positions": 1, "max_daily_loss": 1e9, "max_daily_orders": 200},
    }
    if stop is not None:
        d["exit"]["stop_loss"] = {"type": "percent", "value": stop}
    cond = ({"op": "gt", "left": {"price": "close"}, "right": {"value": trigger}}
            if side == "long"
            else {"op": "lt", "left": {"price": "close"}, "right": {"value": trigger}})
    d["entry"]["long" if side == "long" else "short"] = {"when": cond}
    if side == "short":
        d["risk"]["short"] = {"enabled": True, "initial_margin_pct": 50.0,
                              "maintenance_margin_pct": 30.0, "max_short_notional": 1e9}
    return StrategyDocument.model_validate(d)


def run(document, df, capital=100_000.0):
    cfg = BacktestConfig(start="2024-01-02", end="2024-01-31", initial_capital=capital,
                         slippage={"type": "fixed_bps", "value": 0.0})
    return run_backtest(document, cfg, {"X": df})


def test_ungated_trail_ratchets_up_never_loosens_and_exits_at_the_trailed_level():
    """Entry 100, trail 10% => distance 10. High-water 120 puts the trail at
    110; the following bar retraces to a 115 high, which must NOT pull the trail
    back to 105. The exit therefore lands at 110, not at 105 and not at the
    50% stop_loss."""
    df = bars(opens=[101, 100, 110, 115, 112],
              highs=[101, 100, 120, 115, 112],
              lows=[101, 100, 110, 112, 105],
              closes=[101, 100, 120, 113, 105])
    res = run(doc(trail=10.0), df)
    t = res.trades[0]
    assert t["entry_px"] == pytest.approx(100.0)
    assert t["exit_reason"] == "stop"
    assert t["exit_px"] == pytest.approx(110.0)


def test_gated_trail_contributes_no_stop_before_it_arms():
    """activate_at=20% (=> 120) is never reached, so the trail stays dormant.
    Bar 3 dips to 88 — that is well through where an armed trail would sit
    (100-10=90) but nowhere near the 50% stop_loss. A dormant trail must
    contribute NOTHING, so the position must not be stopped out."""
    df = bars(opens=[101, 100, 100, 95, 95],
              highs=[101, 100, 100, 95, 96],
              lows=[101, 100, 100, 88, 94],
              closes=[101, 100, 100, 90, 95])
    res = run(doc(trail=10.0, activate_at=20.0), df)
    assert not any(t["exit_reason"].startswith("stop") for t in res.trades)

    # sanity: the identical bars WITHOUT the gate do stop out on the trail
    res_ungated = run(doc(trail=10.0), df)
    assert res_ungated.trades and res_ungated.trades[0]["exit_reason"] == "stop"
    assert res_ungated.trades[0]["exit_px"] == pytest.approx(90.0)


def test_arming_happens_after_exit_checks_so_the_arming_bar_cannot_stop_out():
    """Bar 2 both reaches the activation price (120) and closes back at 109 —
    below where the freshly armed trail will sit (120-10=110). Because arming
    and ratcheting run AFTER the bar's exit checks, that bar must not exit; the
    NEXT bar's dip to 108 is the one that stops out, at 110."""
    df = bars(opens=[101, 100, 110, 111, 111],
              highs=[101, 100, 120, 111, 111],
              lows=[101, 100, 108, 108, 108],
              closes=[101, 100, 109, 109, 109])
    res = run(doc(trail=10.0, activate_at=20.0), df)
    t = res.trades[0]
    assert t["exit_reason"] == "stop"
    assert t["exit_px"] == pytest.approx(110.0)
    # the arming bar is bar 2; the exit is bar 3, i.e. strictly later
    assert t["bars_held"] == 3


def test_short_trail_ratchets_down():
    """Mirror image: a short entered at 100 with a 10% trail follows the
    LOW-water mark downward. Low-water 80 puts the trail at 90; a bounce whose
    low is 85 must not push it back out to 95. The stop fires on STRENGTH."""
    df = bars(opens=[99, 100, 90, 85, 87],
              highs=[99, 100, 90, 86, 95],
              lows=[99, 100, 80, 85, 87],
              closes=[99, 100, 80, 85, 95])
    res = run(doc(side="short", trail=10.0), df)
    t = res.trades[0]
    assert t["side"] == "short"
    assert t["exit_reason"] == "stop"
    assert t["exit_px"] == pytest.approx(90.0)


# =============================================================================
# lint
# =============================================================================


def test_lint_flags_activate_at_problems():
    d = doc(trail=10.0, activate_at=5.0)
    problems = lint(d)
    assert any("activate_at" in p and "nearer" in p and "(warning)" in p for p in problems)

    # a gated trail with NO stop_loss is unprotected for the whole dormant
    # window -- an ERROR, same class as a short without a stop
    d2 = doc(trail=10.0, activate_at=20.0, stop=None)
    problems2 = lint(d2)
    unprotected = [p for p in problems2 if "activate_at" in p and "stop_loss is REQUIRED" in p]
    assert unprotected and "(warning)" not in unprotected[0]

    # atr_multiple activate_at needs an atr_indicator, like any other StopSpec
    raw = d.model_dump()
    raw["exit"]["trailing_stop"]["activate_at"] = {"type": "atr_multiple", "value": 2.0}
    d3 = StrategyDocument.model_validate(raw)
    assert any("trailing_stop.activate_at" in p and "atr_indicator" in p
               for p in lint(d3))

    # a well-formed gated trail is clean of all three
    ok = lint(doc(trail=5.0, activate_at=10.0))
    assert not any("activate_at" in p for p in ok)


# =============================================================================
# live runner
# =============================================================================


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


def trail_doc(*, trail=5.0, activate_at=None, stop=2.0, max_daily_orders=50):
    """rsi_doc() (entry always fires) plus a trailing stop."""
    d = rsi_doc()
    raw = d.model_dump()
    trailing = {"type": "percent", "value": trail}
    if activate_at is not None:
        trailing["activate_at"] = {"type": "percent", "value": activate_at}
    raw["exit"] = {"stop_loss": {"type": "percent", "value": stop},
                   "trailing_stop": trailing}
    raw["risk"]["max_daily_orders"] = max_daily_orders
    return StrategyDocument.model_validate(raw)


async def open_position(db, broker, gateway, book, document):
    """Drive one bar so the always-true entry fills, and return
    (runner, agg, position, next_ts)."""
    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", document, "paper", broker, gateway, mds, agg, db)
    await runner.start()
    book.set("SPY", bid=99.9, ask=100.1)
    ts = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push("SPY", "1m", bar_event("SPY", ts, 100, 100.0, 100.0, 100, 1000))
    await settle()
    assert "SPY" in runner._positions, "entry did not fill"
    return runner, agg, runner._positions["SPY"], ts


async def test_ratchet_amends_the_resting_broker_stop_to_the_new_price(
        db, broker, gateway, book):
    runner, agg, pos, ts = await open_position(
        db, broker, gateway, book, trail_doc(trail=5.0, stop=2.0))
    entry = pos.avg_price
    first_stop_id = pos.stop_broker_order_id
    assert broker._working[first_stop_id].req.stop_px == pytest.approx(entry * 0.98)

    # the trail distance is 5% OF THE ENTRY (an absolute distance, re-derived
    # once at entry), so a +10% high puts the trail at high - 0.05*entry
    dist = entry * 0.05
    high = entry * 1.10
    await agg.push("SPY", "1m", bar_event("SPY", ts + MIN_MS, entry, high, entry, high, 1000))
    await settle()

    expected = high - dist
    assert pos.trail_px == pytest.approx(expected)
    assert pos.stop_px == pytest.approx(expected)
    # the OLD order is gone and a NEW one rests at the trailed price
    assert pos.stop_broker_order_id != first_stop_id
    assert first_stop_id not in broker._working
    assert broker._working[pos.stop_broker_order_id].req.stop_px == pytest.approx(expected)

    # a retrace must not loosen it: same position, lower high, stop unchanged
    ratcheted_id = pos.stop_broker_order_id
    await agg.push("SPY", "1m",
                   bar_event("SPY", ts + 2 * MIN_MS, high, entry * 1.02, expected + 0.5,
                             entry * 1.02, 1000))
    await settle()
    assert pos.stop_px == pytest.approx(expected)
    assert pos.stop_broker_order_id == ratcheted_id
    await runner.stop()


async def test_trail_amendment_does_not_consume_the_daily_order_budget(
        db, broker, gateway, book):
    """A trail amendment REPLACES existing protection — it adds no exposure. If
    it counted toward `risk.max_daily_orders`, a stop that ratchets every bar
    would silently exhaust the day's budget and then the gateway would refuse a
    genuine exit, which is the exact opposite of what a tightening stop is
    for."""
    runner, agg, pos, ts = await open_position(
        db, broker, gateway, book, trail_doc(trail=5.0, stop=2.0))
    before = runner._orders_today

    px = pos.avg_price
    for n in range(1, 6):                     # five consecutive ratchet steps
        px *= 1.05
        await agg.push("SPY", "1m", bar_event("SPY", ts + n * MIN_MS, px, px, px, px, 1000))
        await settle()

    assert pos.stop_px == pytest.approx(px - pos.avg_price * 0.05)  # really ratcheted 5x
    assert runner._orders_today == before               # ...and cost zero budget
    assert runner.state == "RUNNING"
    await runner.stop()


async def test_a_failed_stop_replacement_halts_the_run_rather_than_continuing(
        db, broker, gateway, book):
    """The re-place is cancel-then-place, so a failure leaves the position with
    NO resting stop. That is a halt-the-run condition (same invariant as a
    protective order failing at entry), never something to continue past."""
    runner, agg, pos, ts = await open_position(
        db, broker, gateway, book, trail_doc(trail=5.0, stop=2.0))

    async def refuse(*a, **kw):
        raise RuntimeError("broker rejected the replacement stop")

    broker.place = refuse
    high = pos.avg_price * 1.10
    await agg.push("SPY", "1m",
                   bar_event("SPY", ts + MIN_MS, pos.avg_price, high, pos.avg_price, high, 1000))
    await settle()

    assert runner.state == "ERROR"
    row = await db.fetch_one("SELECT state FROM runs WHERE id='run-1'")
    assert row["state"] == "ERROR"


async def test_gated_trail_stays_dormant_in_the_runner_until_it_arms(
        db, broker, gateway, book):
    runner, agg, pos, ts = await open_position(
        db, broker, gateway, book, trail_doc(trail=5.0, activate_at=20.0, stop=10.0))
    entry, resting = pos.avg_price, pos.stop_broker_order_id
    assert pos.trail_armed is False and pos.trail_px is None

    # +10% is nowhere near the +20% gate: no trail, no amendment
    await agg.push("SPY", "1m", bar_event("SPY", ts + MIN_MS, entry, entry * 1.10,
                                         entry, entry * 1.10, 1000))
    await settle()
    assert pos.trail_armed is False and pos.trail_px is None
    assert pos.stop_broker_order_id == resting
    assert pos.stop_px == pytest.approx(entry * 0.90)

    # +25% crosses the gate; the trail arms and takes over the resting stop
    high = entry * 1.25
    await agg.push("SPY", "1m",
                   bar_event("SPY", ts + 2 * MIN_MS, entry * 1.10, high, entry * 1.10,
                             high, 1000))
    await settle()
    assert pos.trail_armed is True
    assert pos.stop_px == pytest.approx(high - entry * 0.05)
    await runner.stop()


# =============================================================================
# crash recovery (regression for the missing _recover_trail)
# =============================================================================


async def test_recovery_rebuilds_the_trail_from_candle_history(db, broker, gateway, book):
    """`positions` journals avg_price/opened_at but NOT the high-water mark, so
    `_recover_trail` reconstructs it from the candle history warm-up has
    already reloaded. A position opened at 100 whose bars since then peaked at
    120 must come back with a stop at the TRAILED level (120*0.95), not reset
    to the entry-relative one."""
    document = trail_doc(trail=5.0, stop=2.0)
    df = warmup_df(n=10, start_px=100.0)
    peak_i = 6
    df.iloc[peak_i, df.columns.get_loc("high")] = 120.0
    opened_at = int(df.index[peak_i - 1].timestamp() * 1000)

    mds = FakeMDS({"SPY": df})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", document, "paper", broker, gateway, mds, agg, db)
    book.set("SPY", bid=99.9, ask=100.1)
    broker.seed_position("SPY", 10.0, 100.0)
    await runner.start(recovered_positions=[
        {"symbol": "SPY", "qty": 10.0, "avg_price": 100.0, "opened_at": opened_at}])
    await settle()

    pos = runner._positions["SPY"]
    assert pos.high_water == pytest.approx(120.0)
    assert pos.trail_armed is True
    # the trail distance is 5% of the recovered avg_price = 5.0 (absolute), so
    # a 120 high-water mark trails to 115 -- NOT back to the entry-relative 95
    assert pos.trail_px == pytest.approx(115.0)
    assert pos.stop_px == pytest.approx(115.0)       # the trail beats stop_loss's 98
    assert broker._working[pos.stop_broker_order_id].req.stop_px == pytest.approx(115.0)
    assert runner.state == "RUNNING"
    await runner.stop()


async def test_recovery_falls_back_to_entry_when_history_starts_after_the_position(
        db, broker, gateway, book):
    """No bars at/after `opened_at` means no evidence of any favourable
    excursion. The reconstruction then treats the trail as never having moved
    off entry — protecting LESS than reality, which is the safe direction: it
    can never invent a tighter stop than the data supports."""
    document = trail_doc(trail=5.0, stop=2.0)
    df = warmup_df(n=10, start_px=100.0)
    future = int(df.index[-1].timestamp() * 1000) + 60 * MIN_MS

    mds = FakeMDS({"SPY": df})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", document, "paper", broker, gateway, mds, agg, db)
    book.set("SPY", bid=99.9, ask=100.1)
    broker.seed_position("SPY", 10.0, 100.0)
    await runner.start(recovered_positions=[
        {"symbol": "SPY", "qty": 10.0, "avg_price": 100.0, "opened_at": future}])
    await settle()

    pos = runner._positions["SPY"]
    assert pos.high_water == pytest.approx(100.0)
    assert pos.trail_px == pytest.approx(95.0)       # entry - 5%
    assert pos.stop_px == pytest.approx(98.0)        # stop_loss is TIGHTER, so it wins
    assert runner.state == "RUNNING"
    await runner.stop()


async def test_recovery_leaves_a_gated_trail_dormant_when_the_gate_was_never_reached(
        db, broker, gateway, book):
    """Recovery must never ASSUME armed. With a +20% gate and history that only
    ever reached 105, the trail comes back dormant and only stop_loss rests."""
    document = trail_doc(trail=5.0, activate_at=20.0, stop=2.0)
    df = warmup_df(n=10, start_px=100.0)
    df.iloc[6, df.columns.get_loc("high")] = 105.0
    opened_at = int(df.index[0].timestamp() * 1000)

    mds = FakeMDS({"SPY": df})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", document, "paper", broker, gateway, mds, agg, db)
    book.set("SPY", bid=99.9, ask=100.1)
    broker.seed_position("SPY", 10.0, 100.0)
    await runner.start(recovered_positions=[
        {"symbol": "SPY", "qty": 10.0, "avg_price": 100.0, "opened_at": opened_at}])
    await settle()

    pos = runner._positions["SPY"]
    assert pos.trail_armed is False
    assert pos.trail_px is None
    assert pos.stop_px == pytest.approx(98.0)
    await runner.stop()
