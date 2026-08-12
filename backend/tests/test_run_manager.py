"""RunManager integration tests — real Database + OrderGateway + SimBroker
driven through RunManager (not constructed directly), with fakes for the
strategy files, streamer, market-data quote cache, and bar aggregator so the
whole thing runs without a real Schwab connection. Exercises the promotion-
gate prerequisite, start->pause->resume->stop lifecycle (incl. streamer
subscribe/unsubscribe ref-counting), and live-quote fan-out from the shared
streamer handler into the per-run SimBroker's resting-order evaluation."""

from __future__ import annotations

import time

import pytest

from app.broker.schwab import SchwabBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.runtime.manager import RunManager
from app.strategy.hashing import content_hash
from tests.test_runner import FakeAggregator, FakeMDS, bar_event, rsi_doc, settle, warmup_df

MIN_MS = 60_000
DAY_MS = 86_400_000
ACCOUNT_HASH = "acct-hash-1"


class FakeStreamer:
    def __init__(self):
        self.handlers: dict[str, list] = {}
        self.subscribed: dict[str, set] = {}

    def on_data(self, service, handler):
        self.handlers.setdefault(service, []).append(handler)

    async def subscribe(self, service, symbols):
        self.subscribed.setdefault(service, set()).update(symbols)

    async def unsubscribe(self, service, symbols):
        self.subscribed.setdefault(service, set()).difference_update(symbols)

    async def push(self, service, content):
        for h in self.handlers.get(service, []):
            await h(content)


class FakeMDSQ(FakeMDS):
    """FakeMDS (candles) + the quote-cache surface RunManager relies on."""

    def __init__(self, frames):
        super().__init__(frames)
        self._cache: dict[str, dict] = {}

    def cache_quote(self, symbol, quote):
        self._cache[symbol] = quote

    def latest_cached_quote(self, symbol, max_age_s=10.0):
        return self._cache.get(symbol)


class FakeStrategyFiles:
    def __init__(self, docs: dict):
        self._docs = docs

    def load(self, slug):
        # matches the real StrategyFiles.load's contract (a missing
        # strategy.yaml -> read_text() raises FileNotFoundError naturally)
        if slug not in self._docs:
            raise FileNotFoundError(slug)
        return self._docs[slug]


class FakeSchwabAccounts:
    """Only what RunManager.start_live/SchwabBroker's construction touch —
    no real Schwab connection needed for these tests."""

    def __init__(self, hashes: tuple[str, ...] = (ACCOUNT_HASH,)):
        self._hashes = hashes

    async def get_account_numbers(self):
        return [{"hashValue": h, "accountNumber": h} for h in self._hashes]

    async def get_account(self, account_hash, positions=False):
        return {"securitiesAccount": {"currentBalances": {"liquidationValue": 100_000.0},
                                      "positions": []}}


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


def make_manager(db, gateway, streamer, aggregator, mds, doc, slug="spy-dip", schwab=None,
                 promotion_min_paper_days=14, promotion_min_round_trips=10):
    files = FakeStrategyFiles({slug: doc})
    return RunManager(db, files, mds, streamer, gateway, aggregator, schwab or FakeSchwabAccounts(),
                      promotion_min_paper_days=promotion_min_paper_days,
                      promotion_min_round_trips=promotion_min_round_trips)


async def seed_strategy_and_backtest(db, doc, slug, with_backtest=True):
    await db.execute(
        "INSERT INTO strategies (id, slug, name, current_version, state, file_path, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc.meta.id, slug, doc.meta.name, 1, "saved", f"{slug}.yaml", int(time.time() * 1000)))
    if with_backtest:
        h = content_hash(doc)
        await db.execute(
            "INSERT INTO backtests (id, strategy_version_hash, config_json, status) "
            "VALUES ('bt-1', ?, '{}', 'done')", (h,))


async def seed_paper_evidence(db, doc, days_ago: float, round_trips: int) -> str:
    """A completed paper run for this strategy's version hash, old enough
    and with enough round-trip exits to (maybe) satisfy the promotion gate —
    callers pick days_ago/round_trips relative to whatever minimums the
    manager under test was built with."""
    h = content_hash(doc)
    run_id = "paper-run-1"
    now = int(time.time() * 1000)
    started_at = now - int(days_ago * DAY_MS)
    await db.execute(
        """INSERT INTO runs (id, strategy_id, strategy_version_hash, mode, state,
           resume_policy, started_at, stopped_at) VALUES (?,?,?,?,?,?,?,?)""",
        (run_id, doc.meta.id, h, "paper", "STOPPED", "auto_resume", started_at, now))
    for i in range(round_trips):
        await db.execute(
            "INSERT INTO run_events (run_id, ts, level, kind, payload_json) VALUES (?,?,?,?,?)",
            (run_id, started_at + i, "info", "fill",
             '{"side": "sell", "symbol": "SPY", "qty": 1, "price": 100.0}'))
    return run_id


# ---- promotion-gate prerequisite ------------------------------------------

async def test_start_paper_rejects_without_a_completed_backtest(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip", with_backtest=False)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    with pytest.raises(ValueError, match="no completed backtest"):
        await manager.start_paper("spy-dip")
    assert manager.list_active() == []


async def test_start_paper_requires_the_strategy_be_indexed(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    h = content_hash(doc)
    # backtest exists but the strategies table was never populated (not saved)
    await db.execute(
        "INSERT INTO backtests (id, strategy_version_hash, config_json, status) "
        "VALUES ('bt-1', ?, '{}', 'done')", (h,))
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    with pytest.raises(ValueError, match="not indexed"):
        await manager.start_paper("spy-dip")


# ---- start/subscribe lifecycle --------------------------------------------

async def test_start_paper_creates_run_row_and_subscribes_quotes(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    run_id = await manager.start_paper("spy-dip", sim_latency_ms=0)

    row = await db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
    assert row is not None
    assert row["mode"] == "paper"
    assert row["state"] == "RUNNING"
    assert "SPY" in streamer.subscribed["LEVELONE_EQUITIES"]
    active = manager.list_active()
    assert len(active) == 1 and active[0]["run_id"] == run_id

    await manager.stop(run_id)


# ---- live-quote fan-out drives the per-run SimBroker -----------------------

async def test_quote_fanout_fills_entry_and_resting_stop(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    run_id = await manager.start_paper("spy-dip", sim_latency_ms=0)
    runner = manager._runners[run_id]

    mds.cache_quote("SPY", {"bid": 99.9, "ask": 100.1, "last": 100.0})
    next_ts = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await aggregator.push("SPY", "1m", bar_event("SPY", next_ts, 100, 100.5, 99.5, 100, 1000))
    await settle()

    assert "SPY" in runner._positions
    pos = runner._positions["SPY"]
    assert pos.stop_broker_order_id is not None

    # crash the price via the shared streamer quote handler (not the broker directly)
    await streamer.push("LEVELONE_EQUITIES", [{"key": "SPY", "1": 95.0, "2": 95.5, "3": 95.0}])
    await settle()

    assert "SPY" not in runner._positions
    await manager.stop(run_id)


# ---- pause/resume/stop lifecycle ------------------------------------------

async def test_pause_resume_stop_lifecycle(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    run_id = await manager.start_paper("spy-dip", sim_latency_ms=0)
    runner = manager._runners[run_id]

    await manager.pause(run_id)
    await settle()
    assert runner.state == "PAUSED"

    await manager.resume(run_id)
    await settle()
    assert runner.state == "RUNNING"

    result = await manager.stop(run_id)
    assert result == {"forced": False}
    row = await db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
    assert row["stopped_at"] is not None
    assert "SPY" not in streamer.subscribed.get("LEVELONE_EQUITIES", set())
    assert manager.list_active() == []

    with pytest.raises(KeyError):
        await manager.pause(run_id)
    # a run already cleanly stopped can't be force-stopped again either
    with pytest.raises(KeyError):
        await manager.stop(run_id)


async def test_unknown_run_id_raises_key_error(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    with pytest.raises(KeyError):
        manager.status("does-not-exist")


# ---- RunManager as GlobalRiskContext (gateway check #5) --------------------

async def test_global_max_notional_gates_entries_end_to_end(db, gateway, streamer, aggregator):
    """RunManager is the gateway's GlobalRiskContext — before this wiring
    (Phase 5 item 10), StrategyRunner never passed global_risk to
    gateway.submit() at all, so this check silently never ran for any run."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    await manager.set_global_limits(max_total_notional=1.0)

    run_id = await manager.start_paper("spy-dip", sim_latency_ms=0)
    runner = manager._runners[run_id]
    mds.cache_quote("SPY", {"bid": 99.9, "ask": 100.1, "last": 100.0})
    ts1 = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await aggregator.push("SPY", "1m", bar_event("SPY", ts1, 100, 100.5, 99.5, 100, 1000))
    await settle()
    assert "SPY" not in runner._positions  # blocked by the (tiny) global notional cap

    await manager.set_global_limits(max_total_notional=250_000.0)
    ts2 = ts1 + MIN_MS
    await aggregator.push("SPY", "1m", bar_event("SPY", ts2, 100, 100.5, 99.5, 100, 1000))
    await settle()
    assert "SPY" in runner._positions
    assert manager.total_notional() > 0  # now reflects the open position

    await manager.stop(run_id)


async def test_global_symbol_blocklist_gates_entries_but_not_protective_orders(
        db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    await manager.set_global_limits(symbol_blocklist=["SPY"])

    run_id = await manager.start_paper("spy-dip", sim_latency_ms=0)
    runner = manager._runners[run_id]
    mds.cache_quote("SPY", {"bid": 99.9, "ask": 100.1, "last": 100.0})
    ts = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await aggregator.push("SPY", "1m", bar_event("SPY", ts, 100, 100.5, 99.5, 100, 1000))
    await settle()

    assert "SPY" not in runner._positions
    await manager.stop(run_id)


# ---- promoting to live (Phase 6 slice 2) -----------------------------------

async def test_start_live_rejects_without_a_completed_backtest(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip", with_backtest=False)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    with pytest.raises(ValueError, match="no completed backtest"):
        await manager.start_live("spy-dip", ACCOUNT_HASH)


async def test_start_live_rejects_insufficient_paper_evidence(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=5, round_trips=20)  # too few days
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    with pytest.raises(ValueError, match="doesn't meet the promotion minimums"):
        await manager.start_live("spy-dip", ACCOUNT_HASH)


async def test_start_live_rejects_insufficient_round_trips(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=2)  # too few trades
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    with pytest.raises(ValueError, match="doesn't meet the promotion minimums"):
        await manager.start_live("spy-dip", ACCOUNT_HASH)


async def test_override_gate_skips_never_paper_traded_when_explicitly_set(
        db, gateway, streamer, aggregator):
    # override_gate=True is an explicit user choice that bypasses EVERY
    # promotion-readiness check, including never having paper-traded at all
    # (promotions.backtest_run_id/paper_run_id are nullable specifically for
    # this — db.py's _migrate_promotions_nullable).
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")  # no paper run at all
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH, override_gate=True)

    row = await db.fetch_one("SELECT promotion_id FROM runs WHERE id=?", (run_id,))
    promo = await db.fetch_one("SELECT * FROM promotions WHERE id=?", (row["promotion_id"],))
    assert promo["paper_run_id"] is None
    assert promo["backtest_run_id"] is not None  # this strategy DOES have a backtest
    await manager.stop(run_id)


async def test_override_gate_skips_no_backtest_at_all_when_explicitly_set(
        db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip", with_backtest=False)  # nothing at all
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH, override_gate=True)

    row = await db.fetch_one("SELECT promotion_id FROM runs WHERE id=?", (run_id,))
    promo = await db.fetch_one("SELECT * FROM promotions WHERE id=?", (row["promotion_id"],))
    assert promo["backtest_run_id"] is None
    assert promo["paper_run_id"] is None
    await manager.stop(run_id)


async def test_without_override_still_requires_everything(db, gateway, streamer, aggregator):
    # override_gate defaults to False — the strict gate is unchanged unless
    # a caller explicitly opts into bypassing it.
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip", with_backtest=False)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    with pytest.raises(ValueError, match="no completed backtest"):
        await manager.start_live("spy-dip", ACCOUNT_HASH)


async def test_start_live_override_gate_succeeds_despite_insufficient_evidence(
        db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=1, round_trips=1)  # thin, but exists
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH, override_gate=True)
    assert run_id

    audits = await db.fetch_all(
        "SELECT payload_json FROM audit_log WHERE category='promotion' AND actor='user'")
    assert any('"override": true' in a["payload_json"] for a in audits)
    await manager.stop(run_id)


async def test_start_live_rejects_when_kill_switch_active(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    await gateway.activate_kill_switch()

    with pytest.raises(ValueError, match="kill switch"):
        await manager.start_live("spy-dip", ACCOUNT_HASH)


async def test_start_live_rejects_unknown_account_hash(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    with pytest.raises(ValueError, match="not a connected Schwab account"):
        await manager.start_live("spy-dip", "some-other-hash")


async def test_start_live_rejects_double_promotion(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH)
    with pytest.raises(ValueError, match="already has an active live run"):
        await manager.start_live("spy-dip", ACCOUNT_HASH)
    await manager.stop(run_id)


async def test_start_live_creates_paused_run_with_schwab_broker_and_promotion_row(
        db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    paper_run_id = await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH)

    row = await db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
    assert row["mode"] == "live"
    assert row["state"] == "PAUSED"
    assert row["account_hash"] == ACCOUNT_HASH
    assert row["resume_policy"] == "require_confirm"
    assert row["promotion_id"] is not None
    # Regression: starting_equity was left NULL, which crashed RunDetail.tsx
    # (unconditional .toLocaleString() call) — found live 2026-07-24.
    assert row["starting_equity"] == 100_000.0

    promo = await db.fetch_one("SELECT * FROM promotions WHERE id=?", (row["promotion_id"],))
    assert promo is not None
    assert promo["paper_run_id"] == paper_run_id
    assert promo["backtest_run_id"] == "bt-1"

    broker = manager._brokers[run_id]
    assert isinstance(broker, SchwabBroker)
    assert "SPY" in streamer.subscribed["LEVELONE_EQUITIES"]
    await manager.stop(run_id)


async def test_stop_cancels_a_live_brokers_orphaned_poll_task(db, gateway, streamer, aggregator):
    """Regression (found live 2026-07-24): RunManager.stop() dropped its
    reference to a SchwabBroker without ever calling broker.stop() (which
    exists specifically to cancel its background fill-polling task). The
    poll task kept running orphaned in the background, hitting
    `GET .../orders/{id}` every poll_interval_s indefinitely for an order
    the app could no longer see or cancel through anything (its own
    `_brokers` no longer referenced it either) -- would have run for as
    long as the process stayed up."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH)
    broker = manager._brokers[run_id]

    # simulate a real working order without hitting the network
    from app.broker.base import OrderRequest
    from app.broker.schwab import _WorkingOrder
    broker._working["fake-order-1"] = _WorkingOrder(
        req=OrderRequest(intent_id="i1", symbol="SPY", side="buy", qty=1, order_type="market"),
        broker_order_id="fake-order-1")
    broker._poll_interval_s = 3600  # long enough it never actually fires during the test
    broker._ensure_poller()
    poll_task = broker._poll_task
    assert poll_task is not None and not poll_task.done()

    await manager.stop(run_id)

    assert poll_task.cancelled()


async def test_armed_live_run_never_autotrades_on_a_bar_event(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH)
    runner = manager._runners[run_id]
    assert runner.state == "PAUSED"

    # feed the exact bar shape that reliably triggers an entry for a RUNNING
    # paper run elsewhere in this file (test_quote_fanout_fills_entry...) —
    # while PAUSED (armed, not yet activated) it must be a no-op.
    next_ts = int(runner._candles["SPY"].index[-1].timestamp() * 1000) + MIN_MS
    await aggregator.push("SPY", "1m", bar_event("SPY", next_ts, 100, 100.5, 99.5, 100, 1000))
    await settle()

    assert runner._positions == {}
    await manager.stop(run_id)


async def test_activate_live_transitions_paused_to_running(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH)
    runner = manager._runners[run_id]
    assert runner.state == "PAUSED"

    await manager.activate_live(run_id)
    await settle()
    assert runner.state == "RUNNING"

    audits = await db.fetch_all(
        "SELECT payload_json FROM audit_log WHERE category='promotion'")
    assert any("live_activated" in a["payload_json"] for a in audits)
    await manager.stop(run_id)


async def test_activate_live_rejects_if_not_paused(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH)

    await manager.activate_live(run_id)
    await settle()
    with pytest.raises(ValueError, match="not PAUSED"):
        await manager.activate_live(run_id)
    await manager.stop(run_id)


async def test_activate_live_rejects_a_paper_run(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    run_id = await manager.start_paper("spy-dip", sim_latency_ms=0)

    with pytest.raises(ValueError, match="not a live run"):
        await manager.activate_live(run_id)
    await manager.stop(run_id)


async def test_on_quote_never_calls_process_tick_on_a_live_broker(db, gateway, streamer, aggregator):
    """Regression: SimBroker.process_tick fills orders off synthetic ticks;
    SchwabBroker has no such method (real fills come from its own
    ACCT_ACTIVITY/polling path). RunManager._on_quote must never call it on
    a live-mode broker — this would AttributeError if the mode guard broke."""
    doc = rsi_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH)

    assert not hasattr(manager._brokers[run_id], "process_tick")
    # would raise AttributeError here if the mode guard in _on_quote were removed
    await streamer.push("LEVELONE_EQUITIES", [{"key": "SPY", "1": 99.0, "2": 99.5, "3": 99.2}])
    await settle()

    await manager.stop(run_id)


# ---- promotion_status: read-only eligibility check (Phase 6 slice 3) -------

async def test_promotion_status_unknown_slug_raises_file_not_found(db, gateway, streamer,
                                                                    aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)
    with pytest.raises(FileNotFoundError):
        await manager.promotion_status("does-not-exist")


async def test_promotion_status_no_backtest_no_paper(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip", with_backtest=False)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    status = await manager.promotion_status("spy-dip")
    assert status["eligible"] is False
    assert status["has_backtest"] is False
    assert status["has_paper_run"] is False
    assert status["min_paper_days"] == 14
    assert status["min_round_trips"] == 10


async def test_promotion_status_insufficient_paper_evidence(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=2, round_trips=1)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    status = await manager.promotion_status("spy-dip")
    assert status["eligible"] is False
    assert status["has_backtest"] is True
    assert status["has_paper_run"] is True
    assert status["paper_round_trips"] == 1


async def test_promotion_status_eligible(db, gateway, streamer, aggregator):
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    paper_run_id = await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    status = await manager.promotion_status("spy-dip")
    assert status["eligible"] is True
    assert status["paper_run_id"] == paper_run_id


# ---- audit C1: the promotion gate now inspects the DOCUMENT ------------------

def _gapped_doc():
    """rsi_doc() plus a non-market `orders.exit` — a field honoured by neither
    the engine nor the runner, so it is a BLOCKING live-support gap."""
    import copy as _copy
    data = _copy.deepcopy(rsi_doc().model_dump(mode="json"))
    data["orders"] = {**(data.get("orders") or {}),
                      "exit": {"type": "marketable_limit", "offset_bps": 10}}
    from app.strategy.schema import StrategyDocument
    return StrategyDocument.model_validate(data)


async def test_promotion_is_refused_while_a_blocking_live_gap_is_present(
        db, gateway, streamer, aggregator):
    """Before C1 the gate checked only backtest-exists + paper-days + round-trips
    and never looked at the document, so a strategy relying on a lever the live
    runtime ignores was promoted cleanly. This cannot be satisfied by waiting —
    no amount of paper trading makes an unread field start working — so it is
    worth a distinct message from the evidence-based refusals."""
    doc = _gapped_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)   # fully eligible
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    with pytest.raises(ValueError, match="does not honour as backtested"):
        await manager.start_live("spy-dip", ACCOUNT_HASH)


async def test_a_blocking_live_gap_makes_promotion_status_ineligible(
        db, gateway, streamer, aggregator):
    doc = _gapped_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    status = await manager.promotion_status("spy-dip")

    assert status["has_backtest"] and status["has_paper_run"]
    assert status["eligible"] is False, "eligible despite an unhonoured document field"
    assert [g["field"] for g in status["live_support_gaps"]] == ["orders.exit"]
    assert status["live_support_gaps"][0]["blocking"] is True


async def test_override_gate_still_promotes_past_a_live_gap(
        db, gateway, streamer, aggregator):
    """The gap is advice, not a lock: override_gate bypasses EVERY readiness
    check by design, and every override is journaled."""
    doc = _gapped_doc()
    mds = FakeMDSQ({"SPY": warmup_df(n=10, start_px=100.0)})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    run_id = await manager.start_live("spy-dip", ACCOUNT_HASH, override_gate=True)

    assert run_id
    await manager.stop(run_id)


async def test_an_ordinary_document_is_still_eligible(db, gateway, streamer, aggregator):
    """The regression that would matter most: C1 must not make every strategy
    unpromotable."""
    doc = rsi_doc()
    mds = FakeMDSQ({})
    await seed_strategy_and_backtest(db, doc, "spy-dip")
    await seed_paper_evidence(db, doc, days_ago=30, round_trips=20)
    manager = make_manager(db, gateway, streamer, aggregator, mds, doc)

    status = await manager.promotion_status("spy-dip")

    assert status["eligible"] is True
    assert status["live_support_gaps"] == []
