"""Crash/restart recovery tests — real Database + OrderGateway + SimBroker
(via RunManager), fakes only for streamer/strategy-files/aggregator/mds
(same fakes as test_run_manager.py). Seeds DB rows by hand to simulate
exactly what an unclean shutdown leaves behind (a `runs` row still
RUNNING/PAUSED, a `positions` row from the last fill, `run_events`
fill-history, and a stale SUBMITTED `order_intents` row), then drives
`recover_at_startup` and checks: cash is reconstructed exactly, today's risk
counters actually gate a post-recovery entry (not just cosmetic), protective
orders are re-placed fresh, stale intents are marked UNKNOWN, resume_policy
branches (auto_resume/require_confirm/flatten_on_restart) behave correctly, and
one run's recovery failure never blocks another's.

LIVE recovery is covered in its own section further down, against a fake
SchwabClient that owns the account's real positions and order book. It is a
different mechanism, not a variation on the paper one: positions come from
Schwab rather than our journal, protective orders still resting at Schwab are
ADOPTED rather than re-placed, orphaned intents are settled from Schwab's own
answer, the run always returns PAUSED, and anything that makes safe
reconciliation impossible (unreachable Schwab, ambiguous account) fails closed
to SUSPENDED_AUTH and stays retryable."""

from __future__ import annotations

import json
import time

import pandas as pd
import pytest

from app.db import Database
from app.execution.gateway import OrderGateway
from app.runtime.manager import RunManager
from app.runtime.recovery import recover_at_startup, recover_run_now
from app.strategy.hashing import content_hash
from tests.test_run_manager import (FakeAggregator, FakeMDSQ, FakeSchwabAccounts,
                                     FakeStrategyFiles, FakeStreamer)
from app.runtime.runner import ET
from tests.test_runner import bar_event, rsi_doc, settle, warmup_df

MIN_MS = 60_000


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def gateway(db):
    return OrderGateway(db, orders_per_minute=6000)


@pytest.fixture
def streamer():
    return FakeStreamer()


@pytest.fixture
def aggregator():
    return FakeAggregator()


async def seed_strategy(db, doc, slug):
    await db.execute(
        "INSERT INTO strategies (id, slug, name, current_version, state, file_path, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc.meta.id, slug, doc.meta.name, 1, "saved", f"{slug}.yaml", int(time.time() * 1000)))


async def seed_dangling_run(db, doc, slug, run_id="run-1", state="RUNNING",
                            resume_policy="auto_resume", starting_equity=100_000.0,
                            qty=10.0, avg_price=100.0, realized_pnl_today=50.0,
                            mode="paper", account_hash=None, broker_order_id=None):
    """Simulates exactly what an unclean shutdown leaves behind: one prior
    closed round trip today (+realized_pnl_today) and one still-open
    position, with its original entry intent never resolved to a terminal
    status (matches reality — the runner never updates order_intents past
    SUBMITTED on fill).

    Every seeded row must land inside the CURRENT ET day, because that is what
    `recovery._reconstruct_today_counters` means by "today" (it filters from ET
    midnight). Stamping the prior entry a flat 30 minutes back put it on the
    PREVIOUS ET day whenever the suite ran in the first 30 minutes after ET
    midnight, so `entries_today` came back empty and every assertion resting on
    "one entry already happened today" inverted. That is LIVING-DOC:64's
    long-unverified "time-of-day-dependent recovery test failure": real, in this
    file, and a fixture bug rather than a product bug — `_today_start_ms()` was
    always correct. Confirmed and fixed 2026-07-30 00:14 ET. `_within_today`
    clamps each offset so the fixture means what its docstring says at every
    hour of the day."""
    now = int(time.time() * 1000)
    et_midnight = int(pd.Timestamp.now(tz=ET).normalize().timestamp() * 1000)

    def _within_today(back_ms: int) -> int:
        """`now - back_ms`, but never earlier than one minute into the ET day."""
        return max(now - back_ms, et_midnight + 60_000)

    await db.execute(
        """INSERT INTO runs (id, strategy_id, strategy_version_hash, mode, state,
           resume_policy, started_at, starting_equity, account_hash)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (run_id, doc.meta.id, content_hash(doc), mode, state, resume_policy,
         _within_today(3600_000), starting_equity, account_hash))
    await db.execute(
        """INSERT INTO positions (run_id, symbol, qty, avg_price, opened_at,
           stop_order_id, target_order_id) VALUES (?,?,?,?,?,?,?)""",
        (run_id, "SPY", qty, avg_price, _within_today(1800_000), "stale-stop", "stale-target"))
    await db.execute(
        "INSERT INTO run_events (run_id, ts, level, kind, payload_json) VALUES (?,?,?,?,?)",
        (run_id, _within_today(1700_000), "info", "fill",
         json.dumps({"symbol": "SPY", "side": "sell", "qty": 5, "price": 110,
                    "reason": "signal", "realized_pnl": realized_pnl_today})))
    await db.execute(
        """INSERT INTO order_intents (id, run_id, ts, symbol, side, qty, order_type,
           purpose, status, gateway_decision_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (f"intent-orphan-{run_id}", run_id, _within_today(1800_000), "SPY", "buy", qty,
         "market", "entry", "SUBMITTED", "[]"))
    if broker_order_id is not None:
        # the `orders` row a live run always has alongside its intent — what
        # live recovery uses to ask Schwab what actually became of the order
        await db.execute(
            """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag, status,
               submitted_at) VALUES (?,?,?,?,?,?,?,?)""",
            (f"order-{run_id}", f"intent-orphan-{run_id}", run_id, mode, broker_order_id,
             f"intent-orphan-{run_id}", "SUBMITTED", _within_today(1800_000)))
    return run_id


def make_manager(db, gateway, streamer, aggregator, mds, doc, slug="spy-dip", schwab=None):
    files = FakeStrategyFiles({slug: doc})
    return RunManager(db, files, mds, streamer, gateway, aggregator,
                      schwab or FakeSchwabAccounts())


ACCT = "acct-hash-1"


class FakeSchwabLive:
    """Enough of SchwabClient for live recovery: the account's real positions
    and its real order book, both settable per test, plus placement so a
    protective order recovery has to re-place is observable.

    `fail` makes every call raise, which is what an expired refresh token or a
    Schwab outage looks like from here."""

    def __init__(self, positions=None, orders=None, hashes=(ACCT,), account_type="MARGIN"):
        self.positions = positions or {}          # symbol -> (qty, avg_price)
        self.orders = orders or {}                # order id -> raw order dict
        self._hashes = hashes
        self._account_type = account_type
        self.placed: list[dict] = []
        self.canceled: list[str] = []
        self.fail = False
        self._next_id = 9000

    def _check(self):
        if self.fail:
            raise RuntimeError("refresh token expired")

    async def get_account_numbers(self):
        self._check()
        return [{"hashValue": h, "accountNumber": h} for h in self._hashes]

    async def get_account(self, account_hash, positions=False):
        self._check()
        return {"securitiesAccount": {
            "type": self._account_type,
            "currentBalances": {"liquidationValue": 100_000.0, "cashBalance": 50_000.0},
            "positions": [
                {"instrument": {"symbol": sym},
                 "longQuantity": max(qty, 0), "shortQuantity": max(-qty, 0),
                 "averagePrice": avg}
                for sym, (qty, avg) in self.positions.items()],
        }}

    async def get_orders(self, account_hash, from_iso, to_iso, status=None, max_results=3000):
        self._check()
        return list(self.orders.values())

    async def get_order(self, account_hash, order_id):
        self._check()
        if str(order_id) not in self.orders:
            raise RuntimeError("404 order not found")
        return self.orders[str(order_id)]

    async def place_order(self, account_hash, order):
        self._check()
        self._next_id += 1
        oid = str(self._next_id)
        self.placed.append(order)
        self.orders[oid] = {"orderId": oid, "status": "WORKING", "quantity":
                            order["orderLegCollection"][0]["quantity"],
                            "filledQuantity": 0,
                            "stopPrice": order.get("stopPrice"), "price": order.get("price")}
        return oid

    async def cancel_order(self, account_hash, order_id):
        self._check()
        self.canceled.append(str(order_id))
        self.orders.pop(str(order_id), None)


def resting_order(oid, *, qty=10.0, stop_px=None, limit_px=None, status="WORKING",
                  filled_qty=0.0):
    return {"orderId": oid, "status": status, "quantity": qty, "filledQuantity": filled_qty,
            "stopPrice": stop_px, "price": limit_px}


# ---- core reconstruction: cash, protective orders, order_intents ----------

async def test_recovers_paper_run_with_exact_cash_and_fresh_protective_orders(
        db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", qty=10.0, avg_price=100.0,
                            realized_pnl_today=50.0, starting_equity=100_000.0)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    summary = await recover_at_startup(db, manager, sim_latency_ms=0)

    assert summary == {"recovered": ["run-1"], "errored": [], "live_suspended": [],
                       "live_recovered": []}
    runner = manager._runners["run-1"]
    broker = manager._brokers["run-1"]
    # 100_000 starting + 50 realized - 10*100 cost basis of the still-open position
    assert broker.cash == pytest.approx(99_050.0)
    assert "SPY" in runner._positions
    pos = runner._positions["SPY"]
    assert pos.stop_broker_order_id is not None and pos.stop_broker_order_id != "stale-stop"
    assert pos.target_broker_order_id is not None and pos.target_broker_order_id != "stale-target"
    stop_order = broker._working[pos.stop_broker_order_id]
    target_order = broker._working[pos.target_broker_order_id]
    assert stop_order.req.stop_px == pytest.approx(100.0 * 0.98)
    assert target_order.req.limit_px == pytest.approx(100.0 * 1.05)

    row = await db.fetch_one("SELECT status FROM order_intents WHERE id='intent-orphan-run-1'")
    assert row["status"] == "UNKNOWN"
    run_row = await db.fetch_one("SELECT state FROM runs WHERE id='run-1'")
    assert run_row["state"] == "RUNNING"

    await manager.stop("run-1")


# ---- today's risk counters actually gate a post-recovery entry ------------

async def test_reconstructed_entries_today_blocks_a_same_day_reentry(
        db, gateway, streamer, aggregator):
    """rsi_doc()'s entry.max_entries_per_day defaults to 1. One entry already
    happened today (the orphaned intent seeded by seed_dangling_run). If
    recovery didn't reconstruct entries_today, the strategy would get a
    fresh daily allowance and re-enter after the recovered position closes —
    a real risk-limit bypass, not a cosmetic gap."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", qty=10.0, avg_price=100.0)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    await recover_at_startup(db, manager, sim_latency_ms=0)
    runner = manager._runners["run-1"]
    mds.cache_quote("SPY", {"bid": 94.5, "ask": 95.0, "last": 95.0})

    # crash the price through the recovered 2% stop (fill @ ~98) to close it
    await streamer.push("LEVELONE_EQUITIES", [{"key": "SPY", "1": 94.5, "2": 95.0, "3": 95.0}])
    await settle()
    assert "SPY" not in runner._positions

    # next bar: entry condition is always-true (rsi<200), but the day's
    # single allowed entry was already reconstructed as consumed
    next_ts = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    mds.cache_quote("SPY", {"bid": 99.9, "ask": 100.1, "last": 100.0})
    await aggregator.push("SPY", "1m", bar_event("SPY", next_ts, 100, 100.5, 99.5, 100, 1000))
    await settle()

    assert "SPY" not in runner._positions
    assert "SPY" not in runner._pending_entries

    await manager.stop("run-1")


# ---- resume_policy branches -------------------------------------------------

async def test_require_confirm_resumes_paused_regardless_of_pre_crash_state(
        db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", state="RUNNING", resume_policy="require_confirm")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    await recover_at_startup(db, manager, sim_latency_ms=0)

    runner = manager._runners["run-1"]
    assert runner.state == "PAUSED"
    row = await db.fetch_one("SELECT state FROM runs WHERE id='run-1'")
    assert row["state"] == "PAUSED"
    await manager.stop("run-1")


async def test_pre_crash_paused_state_is_preserved_under_auto_resume(
        db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", state="PAUSED", resume_policy="auto_resume")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    await recover_at_startup(db, manager, sim_latency_ms=0)

    assert manager._runners["run-1"].state == "PAUSED"
    await manager.stop("run-1")


async def test_flatten_on_restart_flattens_and_stops(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    mds.cache_quote("SPY", {"bid": 99.9, "ask": 100.1, "last": 100.0})
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", resume_policy="flatten_on_restart")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    summary = await recover_at_startup(db, manager, sim_latency_ms=0)
    await settle()

    assert summary["recovered"] == ["run-1"]
    assert "run-1" not in manager._runners  # stop() removes it once flattened
    row = await db.fetch_one("SELECT state, stopped_at FROM runs WHERE id='run-1'")
    assert row["state"] == "STOPPED" and row["stopped_at"] is not None
    pos_row = await db.fetch_one("SELECT * FROM positions WHERE run_id='run-1'")
    assert pos_row is None


async def test_flatten_on_restart_leaves_run_running_if_flatten_cant_complete(
        db, gateway, streamer, aggregator):
    """No quote is cached, so the flatten market sell can never fill (same
    requirement any market order has). Recovery must NOT falsely mark the
    run STOPPED with a possibly-still-open position -- it should time out
    and leave the run RUNNING (protective stop/target orders are still
    resting, so the position stays protected either way)."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", resume_policy="flatten_on_restart")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    summary = await recover_at_startup(db, manager, sim_latency_ms=0, flatten_timeout_s=0.2)

    assert summary["recovered"] == ["run-1"]
    assert "run-1" in manager._runners
    row = await db.fetch_one("SELECT state, stopped_at FROM runs WHERE id='run-1'")
    assert row["state"] == "RUNNING" and row["stopped_at"] is None
    pos_row = await db.fetch_one("SELECT * FROM positions WHERE run_id='run-1'")
    assert pos_row is not None

    await manager.stop("run-1")


# ---- live runs are reconciled against Schwab and re-adopted PAUSED ---------

async def seed_live_run(db, doc, *, positions=None, orders=None, account_hash=ACCT,
                        hashes=(ACCT,), broker_order_id="broker-1"):
    """A dangling LIVE run plus the Schwab-side truth it will be reconciled
    against. `positions` defaults to Schwab confirming exactly the position the
    journal holds (SPY 10 @ 100)."""
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", mode="live", resume_policy="require_confirm",
                            account_hash=account_hash, broker_order_id=broker_order_id)
    return FakeSchwabLive(
        positions={"SPY": (10.0, 100.0)} if positions is None else positions,
        orders=orders or {}, hashes=hashes)


async def test_dangling_live_run_is_recovered_and_adopts_its_resting_stop(
        db, gateway, streamer, aggregator):
    """The whole point: a restart no longer strands a live run. The position
    Schwab confirms is re-adopted, the stop STILL RESTING at Schwab is adopted
    rather than duplicated, and the run comes back PAUSED — controllable
    (pause/resume/flatten) but not trading until a human activates it."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, orders={
        "stale-stop": resting_order("stale-stop", qty=10.0, stop_px=98.0),
        "stale-target": resting_order("stale-target", qty=10.0, limit_px=105.0)})
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    summary = await recover_at_startup(db, manager, sim_latency_ms=0)

    assert summary["live_recovered"] == ["run-1"] and summary["live_suspended"] == []
    runner = manager._runners["run-1"]
    assert runner.state == "PAUSED"
    assert (await db.fetch_one("SELECT state FROM runs WHERE id='run-1'"))["state"] == "PAUSED"
    # adopted, NOT re-placed: a second stop would oversell on the way out
    assert schwab.placed == []
    pos = runner._positions["SPY"]
    assert pos.stop_broker_order_id == "stale-stop"
    assert pos.target_broker_order_id == "stale-target"
    assert pos.stop_px == pytest.approx(98.0)
    # the adopted orders are tracked by the new broker, so their fills are seen
    # and cancel/replace can reach them again
    broker = manager._brokers["run-1"]
    assert set(broker.working_order_ids()) == {"stale-stop", "stale-target"}
    # and the runner can route the adopted stop's fill to its exit path
    assert runner._orders["stale-stop"].purpose == "stop"


async def test_live_recovery_replaces_a_protective_order_that_died_while_down(
        db, gateway, streamer, aggregator):
    """The stop is gone from Schwab's book (canceled/expired overnight) while
    the position is still open. Adopting a dead id would leave the position
    unprotected, so a fresh one must be placed."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, orders={})   # nothing resting
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    await recover_at_startup(db, manager, sim_latency_ms=0)

    pos = manager._runners["run-1"]._positions["SPY"]
    assert pos.stop_broker_order_id not in (None, "stale-stop")
    assert [o["orderType"] for o in schwab.placed] == ["STOP", "LIMIT"]
    assert schwab.placed[0]["stopPrice"] == pytest.approx(98.0)   # 2% below avg_price


async def test_live_recovery_takes_position_quantity_from_schwab_not_the_journal(
        db, gateway, streamer, aggregator):
    """"Trust Schwab, not our journal." A partial exit filled while we were
    down: the broker holds 4 shares, our row still says 10."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, positions={"SPY": (4.0, 100.0)}, orders={
        "stale-stop": resting_order("stale-stop", qty=4.0, stop_px=98.0),
        "stale-target": resting_order("stale-target", qty=4.0, limit_px=105.0)})
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    await recover_at_startup(db, manager, sim_latency_ms=0)

    assert manager._runners["run-1"]._positions["SPY"].qty == pytest.approx(4.0)


async def test_live_recovery_repairs_a_stop_resting_for_the_wrong_quantity(
        db, gateway, streamer, aggregator):
    """A scale-out filled while we were down but its replacement stop never
    got placed: Schwab holds 4 shares and the resting stop still covers 10.
    Adopting that as-is would try to sell 10 shares on a trigger. The stop is
    replaced at the position's real quantity; the target, which cannot lose
    money by being too large a limit, is reported and left."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, positions={"SPY": (4.0, 100.0)}, orders={
        "stale-stop": resting_order("stale-stop", qty=10.0, stop_px=98.0),
        "stale-target": resting_order("stale-target", qty=10.0, limit_px=105.0)})
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    await recover_at_startup(db, manager, sim_latency_ms=0)

    assert "stale-stop" in schwab.canceled
    assert [(o["orderType"], o["orderLegCollection"][0]["quantity"]) for o in schwab.placed] == [
        ("STOP", 4.0)]
    pos = manager._runners["run-1"]._positions["SPY"]
    assert pos.stop_broker_order_id != "stale-stop"
    assert pos.stop_px == pytest.approx(98.0)      # same price, correct size
    events = [json.loads(r["payload_json"]) for r in await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind='recovery'")]
    assert any(e.get("event") == "protective_qty_mismatch" for e in events)


async def test_live_recovery_reports_a_position_outside_the_strategy_universe(
        db, gateway, streamer, aggregator):
    """The strategy's YAML was edited while the run was down and no longer
    trades the symbol it holds. The runner has no bars or subscription for it,
    so it cannot be adopted — but dropping the row would erase a real position
    from the record. Report it and leave it."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, positions={"SPY": (10.0, 100.0), "TSLA": (3.0, 200.0)})
    await db.execute(
        "INSERT INTO positions (run_id, symbol, qty, avg_price, opened_at, side) "
        "VALUES ('run-1','TSLA',3.0,200.0,?, 'long')", (int(time.time() * 1000),))
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    result = await recover_run_now(db, manager, "run-1")

    assert [p["symbol"] for p in result["outside_universe"]] == ["TSLA"]
    assert "TSLA" not in manager._runners["run-1"]._positions
    assert await db.fetch_one("SELECT * FROM positions WHERE symbol='TSLA'") is not None


async def test_live_recovery_drops_a_position_schwab_no_longer_holds(
        db, gateway, streamer, aggregator):
    """Its stop filled while we were down. The run must come back flat, not
    holding a position that does not exist."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, positions={})
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    await recover_at_startup(db, manager, sim_latency_ms=0)

    assert manager._runners["run-1"]._positions == {}
    assert await db.fetch_one("SELECT * FROM positions WHERE run_id='run-1'") is None
    assert schwab.placed == []          # nothing to protect


async def test_live_recovery_adopts_a_position_the_journal_never_recorded(
        db, gateway, streamer, aggregator):
    """A fill landed at Schwab after our last journal write. It is a real
    position in a symbol this strategy trades — it must be adopted and
    protected, not left invisible to the app."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, positions={"SPY": (7.0, 101.0)})
    await db.execute("DELETE FROM positions WHERE run_id='run-1'")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    await recover_at_startup(db, manager, sim_latency_ms=0)

    pos = manager._runners["run-1"]._positions["SPY"]
    assert pos.qty == pytest.approx(7.0) and pos.avg_price == pytest.approx(101.0)
    assert schwab.placed[0]["stopPrice"] == pytest.approx(101.0 * 0.98)


async def test_live_recovery_suspends_when_schwab_is_unreachable_then_retries(
        db, gateway, streamer, aggregator):
    """An expired refresh token is the common case. Recovery must fail closed
    (SUSPENDED_AUTH, still non-terminal) and the UI's Reconnect action must be
    able to complete it once auth is back — that is the retry path."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, orders={
        "stale-stop": resting_order("stale-stop", qty=10.0, stop_px=98.0),
        "stale-target": resting_order("stale-target", qty=10.0, limit_px=105.0)})
    schwab.fail = True
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    summary = await recover_at_startup(db, manager, sim_latency_ms=0)

    assert summary["live_suspended"] == ["run-1"] and summary["live_recovered"] == []
    assert "run-1" not in manager._runners
    row = await db.fetch_one("SELECT state, stopped_at FROM runs WHERE id='run-1'")
    assert row["state"] == "SUSPENDED_AUTH" and row["stopped_at"] is None

    schwab.fail = False                       # user re-authorised, hits Reconnect
    result = await recover_run_now(db, manager, "run-1")

    assert result["mode"] == "live" and result["positions"] == 1
    assert result["adopted_protective_orders"] == 2
    assert manager._runners["run-1"].state == "PAUSED"


async def test_startup_retries_a_live_run_left_suspended_by_an_earlier_restart(
        db, gateway, streamer, aggregator):
    """A run already sitting in SUSPENDED_AUTH from a previous restart must be
    retried by the NEXT startup, not left waiting for someone to notice the
    Reconnect button — the usual cause (expired token) is normally fixed by
    then, and a real live position should not stay stranded on a UI click."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, orders={
        "stale-stop": resting_order("stale-stop", qty=10.0, stop_px=98.0),
        "stale-target": resting_order("stale-target", qty=10.0, limit_px=105.0)})
    await db.execute("UPDATE runs SET state='SUSPENDED_AUTH' WHERE id='run-1'")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    summary = await recover_at_startup(db, manager, sim_latency_ms=0)

    assert summary["live_recovered"] == ["run-1"]
    assert manager._runners["run-1"].state == "PAUSED"


async def test_live_recovery_refuses_when_the_account_is_ambiguous(
        db, gateway, streamer, aggregator):
    """No account_hash on the run and two connected accounts: which one it
    traded in is unknowable, and reconciling against the wrong one would adopt
    positions this run never took. Fail closed."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, account_hash=None, hashes=(ACCT, "acct-hash-2"))
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    summary = await recover_at_startup(db, manager, sim_latency_ms=0)

    assert summary["live_suspended"] == ["run-1"]
    audit = await db.fetch_all(
        "SELECT payload_json FROM audit_log WHERE category='recovery' ORDER BY id DESC LIMIT 1")
    assert "account_hash" in json.loads(audit[0]["payload_json"])["reason"]


async def test_live_recovery_settles_orphaned_intents_from_schwabs_answer(
        db, gateway, streamer, aggregator):
    """Blanket-UNKNOWN (what paper recovery does) would trip the kill switch on
    every restart via reconciliation's unresolved-UNKNOWN check. An order
    Schwab can account for is recorded as what it actually became; only one it
    cannot is UNKNOWN."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, orders={
        "stale-stop": resting_order("stale-stop", qty=10.0, stop_px=98.0),
        "stale-target": resting_order("stale-target", qty=10.0, limit_px=105.0)})
    # the run's orphaned entry intent -> order "broker-1", which Schwab says filled
    schwab.orders["broker-1"] = resting_order("broker-1", qty=10.0, status="FILLED",
                                              filled_qty=10.0)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    await recover_at_startup(db, manager, sim_latency_ms=0)

    intent = await db.fetch_one("SELECT status FROM order_intents WHERE id='intent-orphan-run-1'")
    assert intent["status"] == "FILLED"
    order = await db.fetch_one("SELECT status FROM orders WHERE schwab_order_id='broker-1'")
    assert order["status"] == "FILLED"
    fills = await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind='fill' "
        "ORDER BY id DESC LIMIT 1")
    payload = json.loads(fills[0]["payload_json"])
    assert payload["source"] == "settled_at_recovery"
    # deliberately NO realized_pnl: it cannot be derived for a fill we never saw
    assert "realized_pnl" not in payload


async def test_live_recovery_marks_an_intent_schwab_cannot_account_for_unknown(
        db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, orders={
        "stale-stop": resting_order("stale-stop", qty=10.0, stop_px=98.0),
        "stale-target": resting_order("stale-target", qty=10.0, limit_px=105.0)})
    # "broker-1" is absent from Schwab's book entirely -> genuinely unknown
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    await recover_at_startup(db, manager, sim_latency_ms=0)

    intent = await db.fetch_one("SELECT status FROM order_intents WHERE id='intent-orphan-run-1'")
    assert intent["status"] == "UNKNOWN"


async def test_live_recovery_keeps_todays_risk_counters(db, gateway, streamer, aggregator):
    """Settling an intent to FILLED must not give the run a fresh daily
    allowance — `orders_today`/`entries_today` still count it (rsi_doc allows
    one entry per day, and the seeded orphan consumed it)."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    schwab = await seed_live_run(db, doc, orders={
        "stale-stop": resting_order("stale-stop", qty=10.0, stop_px=98.0),
        "stale-target": resting_order("stale-target", qty=10.0, limit_px=105.0)})
    schwab.orders["broker-1"] = resting_order("broker-1", qty=10.0, status="FILLED",
                                              filled_qty=10.0)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, schwab=schwab)

    await recover_at_startup(db, manager, sim_latency_ms=0)

    runner = manager._runners["run-1"]
    assert runner._entries_today == {"SPY": 1}
    assert runner._orders_today == 1


async def test_suspended_live_run_can_still_be_force_stopped(db, gateway, streamer, aggregator):
    """Regression: before this, a SUSPENDED_AUTH run (no in-memory runner,
    by design -- see test above) could NEVER be stopped again: `stop()`
    unconditionally went through `_get`, which raises KeyError for any
    run_id not in `_runners`. The run would sit non-terminal forever,
    permanently blocking `start_live`'s one-live-run-per-strategy check for
    that strategy. `stop()` must fall back to a DB-only close when there is
    no runner to delegate to, and must tell the caller this happened
    (forced=True) since it does NOT cancel or verify anything on the
    broker's side."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", mode="live")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    await recover_at_startup(db, manager, sim_latency_ms=0)
    assert "run-1" not in manager._runners

    result = await manager.stop("run-1")

    assert result == {"forced": True, "mode": "live"}
    row = await db.fetch_one("SELECT state, stopped_at FROM runs WHERE id='run-1'")
    assert row["state"] == "STOPPED"
    assert row["stopped_at"] is not None

    with pytest.raises(KeyError):
        await manager.stop("run-1")


# ---- fault isolation: one bad run never blocks another's recovery ---------

async def test_one_runs_recovery_failure_does_not_block_another(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    doc_bad = rsi_doc()  # a distinct meta.id/slug from `doc`
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", run_id="run-good")
    # a second dangling run pointing at a slug the (fake) strategy files
    # doesn't have -- load() raises KeyError, simulating an unrecoverable run
    await seed_strategy(db, doc_bad, "spy-missing")
    await seed_dangling_run(db, doc_bad, "spy-missing", run_id="run-bad")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, slug="spy-dip")

    summary = await recover_at_startup(db, manager, sim_latency_ms=0)

    assert sorted(summary["recovered"]) == ["run-good"]
    assert summary["errored"] == ["run-bad"]
    assert "run-good" in manager._runners
    assert "run-bad" not in manager._runners
    row = await db.fetch_one("SELECT state FROM runs WHERE id='run-bad'")
    assert row["state"] == "ERROR"

    await manager.stop("run-good")


# ---- on-demand recovery of a run left ERRORed / dangling ------------------

async def test_recover_run_now_readopts_a_run_startup_recovery_errored(
        db, gateway, streamer, aggregator):
    """The gap `recover_run_now` closes: startup recovery only ever selects
    RUNNING/PAUSED rows, so a run it flipped to ERROR (or one the event loop
    ERRORed) keeps stopped_at NULL and is never picked up again — no runner,
    so pause/resume/flatten all KeyError and force-stop is the only exit.
    The on-demand path must rebuild it exactly as startup recovery would."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    doc_bad = rsi_doc()
    await seed_strategy(db, doc_bad, "spy-missing")
    await seed_dangling_run(db, doc_bad, "spy-missing", run_id="run-1")
    # first pass: slug isn't loadable yet -> run lands in ERROR, non-terminal
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc, slug="spy-missing")
    manager._strategy_files = FakeStrategyFiles({})
    assert (await recover_at_startup(db, manager, sim_latency_ms=0))["errored"] == ["run-1"]
    assert "run-1" not in manager._runners
    with pytest.raises(KeyError):
        await manager.pause("run-1")

    # the underlying problem is fixed; user hits "Reconnect run"
    manager._strategy_files = FakeStrategyFiles({"spy-missing": doc})
    result = await recover_run_now(db, manager, "run-1", sim_latency_ms=0)

    assert result["positions"] == 1
    assert result["reconstructed_cash"] == pytest.approx(99_050.0)
    assert result["started_paused"] is False
    assert manager._brokers["run-1"].cash == pytest.approx(99_050.0)
    assert "SPY" in manager._runners["run-1"]._positions
    row = await db.fetch_one("SELECT state FROM runs WHERE id='run-1'")
    assert row["state"] == "RUNNING"
    await manager.pause("run-1")  # controllable again

    await manager.stop("run-1")


async def test_recover_run_now_rejects_unknown_already_active_and_terminal_runs(
        db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", run_id="run-paper")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    with pytest.raises(KeyError):
        await recover_run_now(db, manager, "nope")

    await recover_at_startup(db, manager, sim_latency_ms=0)
    with pytest.raises(ValueError, match="already active"):
        await recover_run_now(db, manager, "run-paper", sim_latency_ms=0)

    await manager.stop("run-paper")
    with pytest.raises(ValueError, match="already stopped"):
        await recover_run_now(db, manager, "run-paper", sim_latency_ms=0)


async def test_recover_run_now_leaves_run_in_error_when_it_fails_again(
        db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", run_id="run-1", state="ERROR")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    manager._strategy_files = FakeStrategyFiles({})

    with pytest.raises(FileNotFoundError):
        await recover_run_now(db, manager, "run-1", sim_latency_ms=0)

    row = await db.fetch_one("SELECT state, stopped_at FROM runs WHERE id='run-1'")
    assert row["state"] == "ERROR"
    assert row["stopped_at"] is None  # still recoverable / force-stoppable
    assert "run-1" not in manager._runners


# ---- A5c: direction is read from positions.side, not the sign of qty ---------

async def seed_short_position(db, run_id="run-1", *, qty, side, avg_price=100.0):
    """Writes a `positions` row directly so a test can choose the exact
    (qty sign, side) combination — including the legacy one the migration
    produces, which no current writer can create."""
    await db.execute(
        "INSERT INTO positions (run_id, symbol, qty, avg_price, opened_at, side) "
        "VALUES (?,?,?,?,?,?)",
        (run_id, "QQQ", qty, avg_price, int(time.time() * 1000), side))


@pytest.mark.parametrize("qty, side, expect_short", [
    (-10.0, "short", True),    # written by the current runner
    (-10.0, "long", True),     # LEGACY: predates the column, migration defaulted it
    (10.0, "short", True),     # side is authoritative even with an unsigned qty
    (10.0, "long", False),
])
def test_signed_qty_reads_side_with_the_sign_as_fallback(qty, side, expect_short):
    """The one-line rule both recovery readers share. The legacy row is the
    reason the sign cannot simply be dropped: `db.py`'s migration adds
    `side TEXT NOT NULL DEFAULT 'long'`, so every pre-existing short on disk
    claims to be long. Trusting the column alone would rebuild it as a long and
    place its protective stop on the wrong side of the market."""
    from app.runtime.recovery import _signed_qty
    signed = _signed_qty({"qty": qty, "side": side})
    assert (signed < 0) is expect_short
    assert abs(signed) == 10.0


def test_signed_qty_tolerates_a_missing_side_key():
    """`side` absent entirely (a caller that did not select it) must not crash
    and must fall back to the sign."""
    from app.runtime.recovery import _signed_qty
    assert _signed_qty({"qty": -4.0}) == -4.0
    assert _signed_qty({"qty": 4.0, "side": None}) == 4.0


async def test_recovery_selects_the_side_column(db, gateway, streamer, aggregator):
    """The actual A5c defect: the runner already preferred `row["side"]`, but
    recovery's SELECT did not include the column, so the preference could never
    fire and direction always came from the sign."""
    doc = rsi_doc()
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", qty=10.0, avg_price=100.0)
    await seed_short_position(db, qty=-5.0, side="short")

    rows = [dict(r) for r in await db.fetch_all(
        "SELECT symbol, qty, avg_price, opened_at, side FROM positions WHERE run_id='run-1'")]
    assert {r["symbol"]: r["side"] for r in rows} == {"SPY": "long", "QQQ": "short"}

    from app.runtime.recovery import _reconstruct_cash
    cash = await _reconstruct_cash(db, "run-1", 100_000.0, rows)
    # SPY long: -10*100 cost basis. QQQ short: +5*100 proceeds. +50 realized.
    assert cash == pytest.approx(100_000.0 + 50.0 - 1_000.0 + 500.0)


async def test_a_legacy_short_row_still_reconstructs_cash_as_a_short(
        db, gateway, streamer, aggregator):
    """A short written before the column existed: side='long', qty negative.
    Its opening fill ADDED proceeds, so cash must go UP, not down."""
    doc = rsi_doc()
    await seed_strategy(db, doc, "spy-dip")
    await seed_dangling_run(db, doc, "spy-dip", qty=10.0, avg_price=100.0)
    await seed_short_position(db, qty=-5.0, side="long")

    from app.runtime.recovery import _reconstruct_cash
    rows = [dict(r) for r in await db.fetch_all(
        "SELECT symbol, qty, avg_price, opened_at, side FROM positions WHERE run_id='run-1'")]
    cash = await _reconstruct_cash(db, "run-1", 100_000.0, rows)
    assert cash == pytest.approx(100_000.0 + 50.0 - 1_000.0 + 500.0)
