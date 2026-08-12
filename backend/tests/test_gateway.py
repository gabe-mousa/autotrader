"""OrderGateway tests: each of the 8 safety checks in isolation, the
journal-before-broker-call invariant, UNKNOWN-never-retried on broker
exception, kill-switch mass-cancel bypassing the stack, and the rate
limiter's reserved-bucket-for-exits property."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.broker.base import BrokerOrderHandle, BrokerRejection
from app.db import Database
from app.execution.gateway import OrderGateway
from app.execution.rate_limiter import OrderRateLimiter
from app.execution.types import OrderIntentRequest


class FakeBroker:
    def __init__(self, mode="paper", place_result=None, place_raises=None):
        self.mode = mode
        self.placed: list = []
        self.canceled: list[str] = []
        self._place_result = place_result or BrokerOrderHandle("bo-1", "WORKING")
        self._place_raises = place_raises
        self._cancel_raises_for: set[str] = set()

    async def place(self, req):
        self.placed.append(req)
        if self._place_raises:
            raise self._place_raises
        return self._place_result

    async def cancel(self, broker_order_id):
        if broker_order_id in self._cancel_raises_for:
            raise RuntimeError("cancel failed")
        self.canceled.append(broker_order_id)

    async def replace(self, broker_order_id, req):
        raise NotImplementedError

    async def get_positions(self):
        return {}

    async def get_account(self):
        return {}

    def subscribe_events(self, handler):
        pass


class FakeRisk:
    def __init__(self, mode="paper", max_open_positions=1, max_daily_orders=10,
                max_daily_loss=500, open_positions=0, orders_today=0, pnl=0.0,
                open_entry_symbols=None):
        self.run_id = "run-1"
        self.mode = mode
        self.max_open_positions = max_open_positions
        self.max_daily_orders = max_daily_orders
        self.max_daily_loss = max_daily_loss
        self._open_positions = open_positions
        self._orders_today = orders_today
        self._pnl = pnl
        self._open_entry_symbols = open_entry_symbols or set()

    def open_position_count(self): return self._open_positions
    def orders_today_count(self): return self._orders_today
    def daily_pnl(self): return self._pnl
    def has_open_entry_order(self, symbol): return symbol in self._open_entry_symbols


class FakeGlobalRisk:
    def __init__(self, max_total_notional=1_000_000, max_total_daily_loss=10_000,
                blocklist=frozenset(), allowlist=None, notional=0.0, pnl=0.0):
        self.max_total_notional = max_total_notional
        self.max_total_daily_loss = max_total_daily_loss
        self.symbol_blocklist = blocklist
        self.symbol_allowlist = allowlist
        self._notional = notional
        self._pnl = pnl

    def total_notional(self): return self._notional
    def total_daily_pnl(self): return self._pnl


def entry_req(**kw) -> OrderIntentRequest:
    defaults = dict(run_id="run-1", symbol="SPY", side="buy", qty=10, order_type="market",
                    purpose="entry", reference_price=500.0, reference_price_age_s=1.0)
    defaults.update(kw)
    return OrderIntentRequest(**defaults)


def exit_req(**kw) -> OrderIntentRequest:
    defaults = dict(run_id="run-1", symbol="SPY", side="sell", qty=10, order_type="market",
                    purpose="exit", reference_price=500.0, reference_price_age_s=1.0)
    defaults.update(kw)
    return OrderIntentRequest(**defaults)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def gw(db):
    return OrderGateway(db, orders_per_minute=6000)  # generous, so rate limit isn't the bottleneck


# ---- happy path + journal-before-broker invariant --------------------------

async def test_happy_path_accepts_and_journals(db, gw):
    broker = FakeBroker()
    result = await gw.submit(entry_req(), broker, FakeRisk())
    assert result.accepted and result.status == "SUBMITTED"
    assert len(broker.placed) == 1
    assert broker.placed[0].tag == result.intent_id  # correlation tag

    intent = await db.fetch_one("SELECT * FROM order_intents WHERE id=?", (result.intent_id,))
    assert intent["status"] == "SUBMITTED"
    order = await db.fetch_one("SELECT * FROM orders WHERE intent_id=?", (result.intent_id,))
    assert order["schwab_order_id"] == "bo-1"


async def test_broker_exception_marks_unknown_never_retries(db, gw):
    broker = FakeBroker(place_raises=RuntimeError("network blip"))
    result = await gw.submit(entry_req(), broker, FakeRisk())
    assert not result.accepted and result.status == "UNKNOWN"
    assert len(broker.placed) == 1  # exactly one attempt — no blind retry
    intent = await db.fetch_one("SELECT * FROM order_intents WHERE id=?", (result.intent_id,))
    assert intent["status"] == "UNKNOWN"


# ---- check 1: kill switch ---------------------------------------------------

async def test_kill_switch_blocks_everything(db, gw):
    await gw.activate_kill_switch()
    for req in (entry_req(), exit_req()):
        result = await gw.submit(req, FakeBroker(), FakeRisk())
        assert not result.accepted and "kill_switch" in result.reason


async def test_kill_switch_cancel_all_bypasses_the_stack(db, gw):
    await gw.activate_kill_switch()
    broker = FakeBroker()
    broker._cancel_raises_for = {"bad-order"}
    failed = await gw.kill_switch_cancel_all(broker, ["ok-1", "bad-order", "ok-2"])
    assert failed == ["bad-order"]
    assert broker.canceled == ["ok-1", "ok-2"]


# ---- check 2: auth gate -----------------------------------------------------

async def test_auth_gate_blocks_live_only(db, gw):
    live_broker = FakeBroker(mode="live")
    result = await gw.submit(entry_req(), live_broker, FakeRisk(mode="live"), auth_ok=False)
    assert not result.accepted and "auth" in result.reason

    paper_broker = FakeBroker(mode="paper")
    result2 = await gw.submit(entry_req(), paper_broker, FakeRisk(mode="paper"), auth_ok=False)
    assert result2.accepted  # paper doesn't care about Schwab auth


# ---- check 3: mode gate -----------------------------------------------------

async def test_mode_gate_rejects_mismatch(db, gw):
    broker = FakeBroker(mode="paper")
    result = await gw.submit(entry_req(), broker, FakeRisk(mode="live"))
    assert not result.accepted and "mode_gate" in result.reason


# ---- check 4: per-strategy limits — entries only ----------------------------

async def test_max_open_positions_blocks_entry_only(db, gw):
    risk = FakeRisk(max_open_positions=1, open_positions=1)
    entry_result = await gw.submit(entry_req(), FakeBroker(), risk)
    assert not entry_result.accepted and "max_open_positions" in entry_result.reason
    exit_result = await gw.submit(exit_req(), FakeBroker(), risk)
    assert exit_result.accepted  # exits are never blocked by this check


async def test_max_daily_orders(db, gw):
    risk = FakeRisk(max_daily_orders=3, orders_today=3)
    result = await gw.submit(entry_req(), FakeBroker(), risk)
    assert not result.accepted and "max_daily_orders" in result.reason


async def test_duplicate_entry_order_working(db, gw):
    risk = FakeRisk(open_entry_symbols={"SPY"})
    result = await gw.submit(entry_req(symbol="SPY"), FakeBroker(), risk)
    assert not result.accepted and "duplicate_entry" in result.reason
    ok = await gw.submit(entry_req(symbol="QQQ"), FakeBroker(), risk)
    assert ok.accepted


async def test_daily_loss_blocks_entry_but_never_exit(db, gw):
    risk = FakeRisk(max_daily_loss=500, pnl=-600)  # already breached
    entry_result = await gw.submit(entry_req(), FakeBroker(), risk)
    assert not entry_result.accepted and "max_daily_loss" in entry_result.reason
    for purpose in ("exit", "stop", "target"):
        result = await gw.submit(exit_req(purpose=purpose), FakeBroker(), risk)
        assert result.accepted, f"{purpose} must never be blocked by daily-loss breach"


# ---- check 5: global limits --------------------------------------------------

async def test_symbol_blocklist_and_allowlist(db, gw):
    blocked = await gw.submit(entry_req(symbol="GME"), FakeBroker(), FakeRisk(),
                              global_risk=FakeGlobalRisk(blocklist=frozenset({"GME"})))
    assert not blocked.accepted and "blocklist" in blocked.reason

    not_allowed = await gw.submit(entry_req(symbol="GME"), FakeBroker(), FakeRisk(),
                                  global_risk=FakeGlobalRisk(allowlist=frozenset({"SPY"})))
    assert not not_allowed.accepted and "allowlist" in not_allowed.reason


async def test_global_daily_loss_check_5_itself_exempts_protective_exits(db, gw):
    # Check #5's OWN purpose-based exemption, in isolation: submit only the
    # exit first, before anything trips the kill switch in this test, so
    # it's check #5's `is_protective` guard being exercised — not check #1.
    gr = FakeGlobalRisk(max_total_daily_loss=1000, pnl=-1500)
    exit_result = await gw.submit(exit_req(), FakeBroker(), FakeRisk(), global_risk=gr)
    assert exit_result.accepted

    entry_result = await gw.submit(entry_req(), FakeBroker(), FakeRisk(), global_risk=gr)
    assert not entry_result.accepted and "global_daily_loss" in entry_result.reason


async def test_global_daily_loss_breach_trips_kill_switch_blocking_everything_after(db, gw):
    # Unlike the per-strategy daily-loss check (#4), a GLOBAL breach trips
    # the kill switch (plan 06 §35-46) — which per check #1's own comment
    # blocks ALL new placements, protective exits included. Safety after a
    # trip comes from stops already resting at the broker, not from new
    # exit orders still being allowed through.
    gr = FakeGlobalRisk(max_total_daily_loss=1000, pnl=-1500)
    assert not await gw.kill_switch_active()
    entry_result = await gw.submit(entry_req(), FakeBroker(), FakeRisk(), global_risk=gr)
    assert not entry_result.accepted and "global_daily_loss" in entry_result.reason
    assert await gw.kill_switch_active()

    exit_result = await gw.submit(exit_req(), FakeBroker(), FakeRisk(), global_risk=gr)
    assert not exit_result.accepted and "kill_switch" in exit_result.reason


async def test_global_blocklist_and_notional_cap_never_block_protective_orders(db, gw):
    """Regression: symbol_allowlist/blocklist and global_max_notional used to
    apply to EVERY purpose, not just entries (only global_daily_loss had the
    entry-only exemption). A resting protective stop/target for an
    ALREADY-OPEN position must never be blocked by a blocklist change or a
    notional cap — same invariant checks #4/#7 already enforce for exits.
    Untested until now because no real GlobalRiskContext existed to exercise
    the path before RunManager (item 10)."""
    gr = FakeGlobalRisk(blocklist=frozenset({"SPY"}), max_total_notional=1,
                        notional=1_000_000)  # already blown well past the cap
    for purpose in ("exit", "stop", "target"):
        result = await gw.submit(exit_req(purpose=purpose), FakeBroker(), FakeRisk(),
                                 global_risk=gr)
        assert result.accepted, f"{purpose} must never be blocked by a global limit"
    # sanity: the same conditions DO still block a fresh entry
    entry_result = await gw.submit(entry_req(), FakeBroker(), FakeRisk(), global_risk=gr)
    assert not entry_result.accepted and "symbol_blocklist" in entry_result.reason


async def test_max_single_order_notional(db):
    gw = OrderGateway(db, orders_per_minute=6000, max_single_order_notional=1000)
    result = await gw.submit(entry_req(qty=10, reference_price=500), FakeBroker(), FakeRisk())
    assert not result.accepted and "max_single_order_notional" in result.reason


async def test_max_single_order_notional_never_blocks_a_protective_order(db):
    gw = OrderGateway(db, orders_per_minute=6000, max_single_order_notional=1000)
    for purpose in ("exit", "stop", "target"):
        result = await gw.submit(exit_req(purpose=purpose, qty=10, reference_price=500),
                                 FakeBroker(), FakeRisk())
        assert result.accepted, f"{purpose} must never be blocked by max_single_order_notional"


async def test_market_closed_blocks_entry_only(db, gw):
    entry_result = await gw.submit(entry_req(), FakeBroker(), FakeRisk(), market_open=False)
    assert not entry_result.accepted and "market_hours" in entry_result.reason
    exit_result = await gw.submit(exit_req(), FakeBroker(), FakeRisk(), market_open=False)
    assert exit_result.accepted


# ---- check 6: duplicate suppression -----------------------------------------

async def test_duplicate_suppression_within_window(db, gw):
    broker = FakeBroker()
    first = await gw.submit(entry_req(symbol="SPY"), broker, FakeRisk())
    assert first.accepted
    second = await gw.submit(entry_req(symbol="SPY"), broker, FakeRisk(open_positions=0))
    assert not second.accepted and "duplicate_suppression" in second.reason


async def test_no_duplicate_after_window_expires(db, gw):
    broker = FakeBroker()
    first = await gw.submit(entry_req(symbol="SPY"), broker, FakeRisk())
    assert first.accepted
    # backdate the journaled intent past the dedupe window
    await db.execute("UPDATE order_intents SET ts=? WHERE id=?",
                     (int((time.time() - 20) * 1000), first.intent_id))
    second = await gw.submit(entry_req(symbol="SPY"), broker, FakeRisk())
    assert second.accepted


# ---- check 7: price sanity ---------------------------------------------------

async def test_market_order_needs_fresh_quote(db, gw):
    stale = await gw.submit(entry_req(reference_price_age_s=30), FakeBroker(), FakeRisk())
    assert not stale.accepted and "price_sanity" in stale.reason
    missing = await gw.submit(entry_req(reference_price=None, reference_price_age_s=None),
                              FakeBroker(), FakeRisk())
    assert not missing.accepted and "price_sanity" in missing.reason


async def test_protective_stop_exempt_from_price_band(db, gw):
    """A stop 20% below the reference price is normal and must NEVER be
    blocked — the band guard is for fat-fingered discretionary orders only."""
    for purpose in ("stop", "target", "exit"):
        req = exit_req(purpose=purpose, order_type="limit", limit_px=400.0,
                       reference_price=500.0)  # 20% away — would fail the 5% band
        result = await gw.submit(req, FakeBroker(), FakeRisk())
        assert result.accepted, f"{purpose} order wrongly blocked by price-sanity band"


async def test_entry_still_subject_to_price_band(db, gw):
    req = entry_req(order_type="limit", limit_px=400.0, reference_price=500.0)  # 20% away
    result = await gw.submit(req, FakeBroker(), FakeRisk())
    assert not result.accepted and "price_sanity" in result.reason


async def test_limit_price_band(db, gw):
    req = entry_req(order_type="limit", limit_px=600.0, reference_price=500.0)  # 20% away
    result = await gw.submit(req, FakeBroker(), FakeRisk())
    assert not result.accepted and "price_sanity" in result.reason

    ok_req = entry_req(order_type="limit", limit_px=510.0, reference_price=500.0)  # 2% away
    ok = await gw.submit(ok_req, FakeBroker(), FakeRisk())
    assert ok.accepted


# ---- check 8: rate limiter ---------------------------------------------------

async def test_rate_limiter_blocks_when_exhausted(db):
    gw2 = OrderGateway(db, orders_per_minute=2)  # tiny bucket: main~1-2 tokens
    results = []
    for i in range(5):
        r = await gw2.submit(entry_req(symbol=f"S{i}", max_wait_s=0.05), FakeBroker(), FakeRisk())
        results.append(r)
    assert any(not r.accepted and r.reason and "rate_limited" in r.reason for r in results)


# ---- automatic kill-switch trips (Phase 6 slice 1) --------------------------
# Distinct symbols per submit() call throughout — otherwise check #6
# (duplicate suppression, a 10s cooldown on identical run/symbol/side/purpose)
# would reject the second call before it ever reaches the broker, which is
# what these tests are trying to exercise.

async def test_three_consecutive_broker_errors_trip_kill_switch(db, gw):
    broker = FakeBroker(place_raises=RuntimeError("network blip"))
    for i in range(2):
        result = await gw.submit(entry_req(symbol=f"E{i}"), broker, FakeRisk())
        assert result.status == "UNKNOWN"
        assert not await gw.kill_switch_active()  # not yet — only 1 or 2 so far
    third = await gw.submit(entry_req(symbol="E2"), broker, FakeRisk())
    assert third.status == "UNKNOWN"
    assert await gw.kill_switch_active()


async def test_broker_rejection_resets_consecutive_error_streak(db, gw):
    bad_broker = FakeBroker(place_raises=RuntimeError("network blip"))
    for i in range(2):
        await gw.submit(entry_req(symbol=f"E{i}"), bad_broker, FakeRisk())
    rejecting_broker = FakeBroker(place_raises=BrokerRejection("bad symbol"))
    rejected = await gw.submit(entry_req(symbol="R1"), rejecting_broker, FakeRisk())
    assert rejected.status == "REJECTED"  # a real response — resets the streak
    # two more errors after the reset must NOT trip (streak restarted at 0)
    for i in range(2):
        result = await gw.submit(entry_req(symbol=f"F{i}"), bad_broker, FakeRisk())
        assert result.status == "UNKNOWN"
    assert not await gw.kill_switch_active()


async def test_successful_placement_resets_consecutive_error_streak(db, gw):
    bad_broker = FakeBroker(place_raises=RuntimeError("network blip"))
    for i in range(2):
        await gw.submit(entry_req(symbol=f"E{i}"), bad_broker, FakeRisk())
    ok = await gw.submit(entry_req(symbol="QQQ"), FakeBroker(), FakeRisk())
    assert ok.accepted
    for i in range(2):
        result = await gw.submit(entry_req(symbol=f"F{i}"), bad_broker, FakeRisk())
        assert result.status == "UNKNOWN"
    assert not await gw.kill_switch_active()


# ---- resolving UNKNOWN orders (Phase 6 slice 1) ------------------------------

async def test_resolve_unknown_intent_transitions_status_and_audits(db, gw):
    broker = FakeBroker(place_raises=RuntimeError("network blip"))
    result = await gw.submit(entry_req(), broker, FakeRisk())
    assert result.status == "UNKNOWN"

    await gw.resolve_unknown_intent(result.intent_id, actor="user",
                                    resolution="confirmed never placed via broker UI")
    intent = await db.fetch_one("SELECT status FROM order_intents WHERE id=?",
                                (result.intent_id,))
    assert intent["status"] == "RESOLVED_MANUAL"
    audit_rows = await db.fetch_all(
        "SELECT * FROM audit_log WHERE category='unknown_order_resolved'")
    assert len(audit_rows) == 1


async def test_resolve_unknown_intent_raises_if_not_unknown(db, gw):
    result = await gw.submit(entry_req(), FakeBroker(), FakeRisk())
    assert result.accepted  # SUBMITTED, not UNKNOWN
    with pytest.raises(ValueError, match="not UNKNOWN"):
        await gw.resolve_unknown_intent(result.intent_id, actor="user", resolution="n/a")

    with pytest.raises(ValueError, match="no order_intent"):
        await gw.resolve_unknown_intent("does-not-exist", actor="user", resolution="n/a")


async def test_reserved_bucket_protects_exits_under_entry_storm():
    limiter = OrderRateLimiter(orders_per_minute=10, reserved_fraction=0.2, min_reserved=1)
    # drain the main bucket with entries
    drained = 0
    for _ in range(50):
        if await limiter.main.try_acquire():
            drained += 1
        else:
            break
    assert drained >= 1
    # main bucket is empty now; an exit must still get through via the reserved lane
    got_exit = await limiter.acquire("stop", timeout_s=0.2)
    assert got_exit, "reserved bucket must protect protective exits from an entry storm"
