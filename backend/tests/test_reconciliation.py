"""ReconciliationEngine tests (Phase 6 slice 1, docs/plan/06-execution-safety.md
§Reconciliation): "trust Schwab, not our journal" — local live positions/
orders vs actual broker state, unresolved UNKNOWN orders, and that any
mismatch trips the kill switch + pauses every active live run. Real Database
+ OrderGateway (kill switch is real DB-backed state); fakes for
SchwabClient/RunManager since those are the two external systems being
reconciled against."""

from __future__ import annotations

import time
import uuid

import pytest

from app.db import Database
from app.execution.gateway import OrderGateway
from app.execution.reconciliation import ReconciliationEngine

ACCOUNT_A = "hash-a"
ACCOUNT_B = "hash-b"


class FakeSchwab:
    def __init__(self, accounts: dict[str, dict] | None = None):
        # {account_hash: {"positions": [...], "balances": {...}, "orders": [...]}}
        self._accounts = accounts or {}

    async def get_account_numbers(self):
        return [{"hashValue": h, "accountNumber": h} for h in self._accounts]

    async def get_account(self, account_hash: str, positions: bool = False) -> dict:
        acct = self._accounts[account_hash]
        return {"securitiesAccount": {
            "positions": acct.get("positions", []),
            "currentBalances": acct.get("balances", {"equity": 0, "cashBalance": 0,
                                                      "buyingPower": 0})}}

    async def get_orders(self, account_hash: str, from_iso: str, to_iso: str,
                         status: str | None = None, max_results: int = 3000) -> list[dict]:
        self.last_window = (from_iso, to_iso)
        return self._accounts[account_hash].get("orders", [])


def position(symbol: str, qty: float) -> dict:
    if qty >= 0:
        return {"instrument": {"symbol": symbol}, "longQuantity": qty, "shortQuantity": 0}
    return {"instrument": {"symbol": symbol}, "longQuantity": 0, "shortQuantity": -qty}


class FakeRunManager:
    def __init__(self, live_runs: dict[str, list[str]] | None = None,
                statuses: list[dict] | None = None):
        self._live_runs = live_runs or {}
        self._statuses = statuses or []
        self.paused: list[str] = []

    def live_run_symbols(self) -> dict[str, list[str]]:
        return self._live_runs

    def list_active(self) -> list[dict]:
        return self._statuses

    async def pause(self, run_id: str) -> None:
        self.paused.append(run_id)


def live_status(run_id: str, positions: dict[str, float]) -> dict:
    return {"run_id": run_id, "mode": "live",
           "positions": {s: {"qty": q, "avg_price": 1.0, "bars_held": 0}
                        for s, q in positions.items()}}


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def gateway(db):
    return OrderGateway(db, orders_per_minute=6000)


async def insert_live_run(db: Database, run_id: str, account_hash: str | None) -> None:
    """A live `runs` row — account_hash NULL models a run armed before the
    column was persisted (the fail-safe case audit B2 cares about)."""
    await db.execute(
        """INSERT INTO runs (id, strategy_id, strategy_version_hash, mode, state,
           resume_policy, started_at, account_hash) VALUES (?,?,?,?,?,?,?,?)""",
        (run_id, "strat-1", "hash-v1", "live", "RUNNING", "require_confirm",
         int(time.time() * 1000), account_hash))


async def insert_unknown_intent(db: Database, run_id: str = "run-1") -> str:
    intent_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO order_intents (id, run_id, ts, symbol, side, qty, order_type,
           purpose, status) VALUES (?,?,?,?,?,?,?,?,?)""",
        (intent_id, run_id, 0, "SPY", "buy", 1, "market", "entry", "UNKNOWN"))
    await db.execute(
        """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag, status)
           VALUES (?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), intent_id, run_id, "live", "bo-1", intent_id, "UNKNOWN"))
    return intent_id


# ---- clean paths -------------------------------------------------------------

async def test_no_live_runs_is_a_clean_noop(db, gateway):
    schwab = FakeSchwab({ACCOUNT_A: {"positions": [], "orders": []}})
    run_manager = FakeRunManager()
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is True and report["trips"] == []
    assert not await gateway.kill_switch_active()
    assert run_manager.paused == []
    snapshot = await db.fetch_one("SELECT * FROM account_snapshots")
    assert snapshot is not None  # still snapshotted even with nothing to compare


async def test_matching_local_and_broker_positions_is_clean(db, gateway):
    schwab = FakeSchwab({ACCOUNT_A: {"positions": [position("SPY", 10)], "orders": []}})
    run_manager = FakeRunManager(
        live_runs={"run-1": ["SPY"]},
        statuses=[live_status("run-1", {"SPY": 10})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is True
    assert not await gateway.kill_switch_active()
    assert run_manager.paused == []


async def test_broker_position_outside_any_live_universe_is_ignored(db, gateway):
    # Mirrors a live-probe manual test order (e.g. WEN) sitting in the real
    # account in a symbol no live strategy trades — plan 06 says explicitly
    # to ignore these, not flag them.
    schwab = FakeSchwab({ACCOUNT_A: {"positions": [position("WEN", 1)], "orders": []}})
    run_manager = FakeRunManager(live_runs={"run-1": ["SPY"]},
                                 statuses=[live_status("run-1", {})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is True
    assert not await gateway.kill_switch_active()


# ---- mismatches trip the kill switch + pause affected runs ------------------

async def test_position_mismatch_trips_kill_switch_and_pauses_run(db, gateway):
    schwab = FakeSchwab({ACCOUNT_A: {"positions": [position("SPY", 5)], "orders": []}})
    run_manager = FakeRunManager(
        live_runs={"run-1": ["SPY"]},
        statuses=[live_status("run-1", {"SPY": 10})])  # we think 10, broker says 5
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is False
    assert report["trips"][0]["reason"] == "position_mismatch"
    assert report["trips"][0]["symbol"] == "SPY"
    assert await gateway.kill_switch_active()
    assert run_manager.paused == ["run-1"]


async def test_open_order_mismatch_trips_kill_switch(db, gateway):
    await db.execute(
        """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag, status)
           VALUES (?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), "intent-1", "run-1", "live", "bo-local-only", "t", "SUBMITTED"))
    schwab = FakeSchwab({ACCOUNT_A: {"positions": [], "orders": [
        {"orderId": 999, "status": "WORKING"}]}})  # broker has an order we don't know about
    run_manager = FakeRunManager(live_runs={"run-1": []}, statuses=[live_status("run-1", {})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is False
    trip = next(t for t in report["trips"] if t["reason"] == "open_order_mismatch")
    assert "999" in trip["missing_locally"]
    assert "bo-local-only" in trip["missing_at_broker"]
    assert await gateway.kill_switch_active()


async def test_unresolved_unknown_order_trips_even_with_no_live_runs(db, gateway):
    await insert_unknown_intent(db)
    schwab = FakeSchwab({})  # no accounts connected — still must trip
    run_manager = FakeRunManager()  # no live runs active
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is False
    assert report["trips"][0]["reason"] == "unknown_order_unresolved"
    assert await gateway.kill_switch_active()


async def test_resolved_unknown_order_no_longer_trips(db, gateway):
    intent_id = await insert_unknown_intent(db)
    await gateway.resolve_unknown_intent(intent_id, actor="user", resolution="confirmed flat")
    schwab = FakeSchwab({})
    run_manager = FakeRunManager()
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is True
    assert not await gateway.kill_switch_active()


# ---- stale journal rows are REPAIRED, not tripped on -------------------------
# Regression: an order left SUBMITTED/WORKING locally past midnight could never
# appear in the broker's response (the window was hardcoded to today), so it was
# reported missing_at_broker forever — re-tripping the kill switch every 5
# minutes with no way to clear it from the UI.

async def test_order_window_reaches_back_to_oldest_open_local_order(db, gateway):
    three_days_ago = int(time.time() * 1000) - 3 * 86_400_000
    await db.execute(
        """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag,
           status, submitted_at) VALUES (?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), "intent-1", "run-1", "live", "bo-old", "t", "WORKING",
         three_days_ago))
    schwab = FakeSchwab({ACCOUNT_A: {"positions": [], "orders": []}})
    engine = ReconciliationEngine(db, schwab, FakeRunManager(), gateway)

    await engine.run_once()

    expected = time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime(three_days_ago / 1000))
    assert schwab.last_window[0] == expected


async def test_stale_local_order_terminal_at_broker_is_repaired_not_tripped(db, gateway):
    await db.execute(
        """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag,
           status, submitted_at) VALUES (?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), "intent-1", "run-1", "live", "bo-stale", "t", "WORKING",
         int(time.time() * 1000) - 3 * 86_400_000))
    # Schwab CAN account for it — it was canceled. Its answer is the truth.
    schwab = FakeSchwab({ACCOUNT_A: {"positions": [], "orders": [
        {"orderId": "bo-stale", "status": "CANCELED"}]}})
    engine = ReconciliationEngine(db, schwab, FakeRunManager(), gateway)

    report = await engine.run_once()

    assert report["clean"] is True
    assert not await gateway.kill_switch_active()
    assert report["repaired_orders"] == [{"schwab_order_id": "bo-stale", "status": "CANCELED"}]
    row = await db.fetch_one("SELECT status, closed_at FROM orders WHERE schwab_order_id='bo-stale'")
    assert row["status"] == "CANCELED"
    assert row["closed_at"] is not None


async def test_local_order_broker_cannot_account_for_still_trips(db, gateway):
    # The genuine "trust Schwab" violation: we think it's working, and Schwab
    # has never heard of it. That must still trip.
    await db.execute(
        """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag,
           status, submitted_at) VALUES (?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), "intent-1", "run-1", "live", "bo-ghost", "t", "WORKING",
         int(time.time() * 1000)))
    schwab = FakeSchwab({ACCOUNT_A: {"positions": [], "orders": []}})
    engine = ReconciliationEngine(db, schwab, FakeRunManager(), gateway)

    report = await engine.run_once()

    assert report["clean"] is False
    trip = next(t for t in report["trips"] if t["reason"] == "open_order_mismatch")
    assert trip["missing_at_broker"] == ["bo-ghost"]
    assert await gateway.kill_switch_active()


# ---- per-account scoping (audit B2) ------------------------------------------
# This engine used to compare local state against the SUM of positions across
# EVERY connected account, on the (since-expired) premise that there were zero
# live runs so the sums were always empty. With a live run and two connected
# accounts that summing was wrong both ways: an unrelated holding in another
# account tripped the kill switch, and two opposite breaks netted to clean.

async def test_each_run_is_compared_against_its_own_account(db, gateway):
    await insert_live_run(db, "run-a", ACCOUNT_A)
    await insert_live_run(db, "run-b", ACCOUNT_B)
    schwab = FakeSchwab({
        ACCOUNT_A: {"positions": [position("SPY", 10)], "orders": []},
        ACCOUNT_B: {"positions": [position("QQQ", 4)], "orders": []}})
    run_manager = FakeRunManager(
        live_runs={"run-a": ["SPY"], "run-b": ["QQQ"]},
        statuses=[live_status("run-a", {"SPY": 10}), live_status("run-b", {"QQQ": 4})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is True and report["trips"] == []
    assert not await gateway.kill_switch_active()


async def test_position_in_another_account_does_not_trip(db, gateway):
    # The live run trades SPY in account A and matches there exactly. Account B
    # (a different, e.g. CASH, account) independently holds SPY — that is not
    # this run's position and must not pause a real live strategy.
    await insert_live_run(db, "run-a", ACCOUNT_A)
    schwab = FakeSchwab({
        ACCOUNT_A: {"positions": [position("SPY", 10)], "orders": []},
        ACCOUNT_B: {"positions": [position("SPY", 5)], "orders": []}})
    run_manager = FakeRunManager(live_runs={"run-a": ["SPY"]},
                                statuses=[live_status("run-a", {"SPY": 10})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is True and report["trips"] == []
    assert not await gateway.kill_switch_active()
    assert run_manager.paused == []


async def test_opposite_cross_account_discrepancies_no_longer_net_to_clean(db, gateway):
    # run-a is short 5 SPY vs its account; run-b is long 5 SPY vs its account.
    # Summed (the old behaviour) local 20 == broker 20 and the engine reported
    # CLEAN, masking two real breaks. Both must now be reported.
    await insert_live_run(db, "run-a", ACCOUNT_A)
    await insert_live_run(db, "run-b", ACCOUNT_B)
    schwab = FakeSchwab({
        ACCOUNT_A: {"positions": [position("SPY", 5)], "orders": []},
        ACCOUNT_B: {"positions": [position("SPY", 15)], "orders": []}})
    run_manager = FakeRunManager(
        live_runs={"run-a": ["SPY"], "run-b": ["SPY"]},
        statuses=[live_status("run-a", {"SPY": 10}), live_status("run-b", {"SPY": 10})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is False
    trips = {t["account_hash"]: t for t in report["trips"]
             if t["reason"] == "position_mismatch"}
    assert set(trips) == {ACCOUNT_A, ACCOUNT_B}
    assert (trips[ACCOUNT_A]["local_qty"], trips[ACCOUNT_A]["broker_qty"]) == (10, 5)
    assert (trips[ACCOUNT_B]["local_qty"], trips[ACCOUNT_B]["broker_qty"]) == (10, 15)
    assert await gateway.kill_switch_active()
    assert sorted(run_manager.paused) == ["run-a", "run-b"]


async def test_local_order_open_only_in_another_account_trips(db, gateway):
    # Booked to account A, and Schwab reports it open in B instead. Scoping
    # `missing_at_broker` per account is what catches this; the old union
    # check saw "open somewhere" and passed.
    await insert_live_run(db, "run-a", ACCOUNT_A)
    await db.execute(
        """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag,
           status, submitted_at) VALUES (?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), "intent-1", "run-a", "live", "bo-wrong-acct", "t",
         "WORKING", int(time.time() * 1000)))
    schwab = FakeSchwab({
        ACCOUNT_A: {"positions": [], "orders": []},
        ACCOUNT_B: {"positions": [], "orders": [
            {"orderId": "bo-wrong-acct", "status": "WORKING"}]}})
    run_manager = FakeRunManager(live_runs={"run-a": []},
                                statuses=[live_status("run-a", {})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is False
    trip = next(t for t in report["trips"] if t["reason"] == "open_order_mismatch")
    assert trip["missing_at_broker"] == ["bo-wrong-acct"]
    assert await gateway.kill_switch_active()


# ---- unattributed (NULL account_hash) live runs degrade toward safety --------

async def test_null_account_hash_with_one_account_is_unambiguous(db, gateway):
    # Exactly one connected account: the run can only be trading there, so
    # attribute it and reconcile normally rather than trip. Not a guess.
    await insert_live_run(db, "run-a", None)
    schwab = FakeSchwab({ACCOUNT_A: {"positions": [position("SPY", 10)], "orders": []}})
    run_manager = FakeRunManager(live_runs={"run-a": ["SPY"]},
                                statuses=[live_status("run-a", {"SPY": 10})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is True and report["trips"] == []
    assert not await gateway.kill_switch_active()


async def test_null_account_hash_with_one_account_still_catches_a_break(db, gateway):
    await insert_live_run(db, "run-a", None)
    schwab = FakeSchwab({ACCOUNT_A: {"positions": [position("SPY", 3)], "orders": []}})
    run_manager = FakeRunManager(live_runs={"run-a": ["SPY"]},
                                statuses=[live_status("run-a", {"SPY": 10})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is False
    assert report["trips"][0]["reason"] == "position_mismatch"
    assert await gateway.kill_switch_active()


async def test_null_account_hash_with_two_accounts_trips_loudly(db, gateway):
    # Genuinely unknowable attribution. It must NOT silently fall back to
    # sum-everything and must NOT be skipped — it trips.
    await insert_live_run(db, "run-a", None)
    schwab = FakeSchwab({
        ACCOUNT_A: {"positions": [position("SPY", 10)], "orders": []},
        ACCOUNT_B: {"positions": [], "orders": []}})
    run_manager = FakeRunManager(live_runs={"run-a": ["SPY"]},
                                statuses=[live_status("run-a", {"SPY": 10})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is False
    trip = next(t for t in report["trips"] if t["reason"] == "unattributed_live_run")
    assert trip["run_ids"] == ["run-a"] and trip["connected_accounts"] == 2
    assert await gateway.kill_switch_active()
    assert run_manager.paused == ["run-a"]


async def test_missing_run_row_is_treated_as_unattributed(db, gateway):
    # No `runs` row at all for an active live run — same unknowable case.
    schwab = FakeSchwab({
        ACCOUNT_A: {"positions": [], "orders": []},
        ACCOUNT_B: {"positions": [], "orders": []}})
    run_manager = FakeRunManager(live_runs={"run-ghost": ["SPY"]},
                                statuses=[live_status("run-ghost", {})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    assert report["clean"] is False
    assert any(t["reason"] == "unattributed_live_run" for t in report["trips"])
    assert await gateway.kill_switch_active()


async def test_unattributed_run_is_still_reconciled_not_skipped(db, gateway):
    # Beyond the ambiguity trip, the symbol-level detail is still reported so
    # the operator sees WHAT disagrees, not just that attribution is missing.
    await insert_live_run(db, "run-a", None)
    schwab = FakeSchwab({
        ACCOUNT_A: {"positions": [position("SPY", 2)], "orders": []},
        ACCOUNT_B: {"positions": [], "orders": []}})
    run_manager = FakeRunManager(live_runs={"run-a": ["SPY"]},
                                statuses=[live_status("run-a", {"SPY": 10})])
    engine = ReconciliationEngine(db, schwab, run_manager, gateway)

    report = await engine.run_once()

    reasons = [t["reason"] for t in report["trips"]]
    assert "unattributed_live_run" in reasons and "position_mismatch" in reasons
    mismatch = next(t for t in report["trips"] if t["reason"] == "position_mismatch")
    assert mismatch["account_hash"] is None  # compared against the union, loudly
    assert (mismatch["local_qty"], mismatch["broker_qty"]) == (10, 2)
