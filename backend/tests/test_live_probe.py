"""LiveProbeService tests: confirm/qty-cap/kill-switch gating, journaling
into the same order_intents/orders tables OrderGateway uses, broker-event-
driven status updates, and that preview never journals or requires confirm.
Real Database + OrderGateway (for the shared kill switch); FakeSchwabClient
(same double as test_schwab_broker.py) standing in for the wire."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.db import Database
from app.execution.gateway import OrderGateway
from app.live.probe import LiveProbeError, LiveProbeService, PROBE_RUN_ID
from app.streamer.acct_activity import AcctActivityRecorder
from tests.test_schwab_broker import FakeSchwabClient

ACCOUNT = "H1"
SPY_QUOTE_PRICE = 100.0  # cached below; qty=1 * 100 stays well under MAX_NOTIONAL
MAX_NOTIONAL = 10_000  # generous — these tests exercise other checks, not this one


class FakeStreamer:
    def on_data(self, service, handler):
        pass

    async def subscribe(self, service, symbols):
        pass


class FakeMarketData:
    """Duck-types the two methods live.probe.LiveProbeService actually calls
    (app.marketdata.service.MarketDataService.cache_quote/latest_cached_quote)
    — no need for the real class's store/backfiller wiring in a unit test."""

    def __init__(self):
        self._quotes: dict[str, dict] = {}

    def cache_quote(self, symbol: str, quote: dict) -> None:
        quote["cached_at"] = time.time()
        self._quotes[symbol] = quote

    def latest_cached_quote(self, symbol: str, max_age_s: float = 10.0) -> dict | None:
        q = self._quotes.get(symbol)
        if q and time.time() - q["cached_at"] <= max_age_s:
            return q
        return None


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


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
def fake_schwab():
    return FakeSchwabClient()


@pytest.fixture
def marketdata():
    md = FakeMarketData()
    md.cache_quote("SPY", {"last": SPY_QUOTE_PRICE})
    return md


@pytest.fixture
def service(db, gateway, fake_schwab, marketdata):
    activity = AcctActivityRecorder(db, FakeStreamer())
    return LiveProbeService(db, fake_schwab, gateway, max_qty=5, activity=activity,
                            marketdata=marketdata, max_notional=MAX_NOTIONAL,
                            streamer=FakeStreamer(), quote_wait_timeout_s=0.05)


# ---- confirm / qty-cap / kill-switch gating --------------------------------

async def test_place_without_confirm_is_rejected_and_never_journaled(service, db):
    with pytest.raises(LiveProbeError, match="confirm"):
        await service.place(ACCOUNT, "SPY", "buy", 1, "market", None, None, confirm=False)
    rows = await db.fetch_all("SELECT * FROM order_intents WHERE run_id=?", (PROBE_RUN_ID,))
    assert rows == []


async def test_place_over_qty_cap_is_rejected(service):
    with pytest.raises(LiveProbeError, match="exceeds the probe cap"):
        await service.place(ACCOUNT, "SPY", "buy", 999, "market", None, None, confirm=True)


async def test_place_over_notional_cap_is_rejected(db, gateway, fake_schwab, marketdata):
    # qty(1) * SPY_QUOTE_PRICE(100) = $100, over a $10 cap — qty alone (1 <=
    # max_qty 5) wouldn't have caught this; the notional cap is what should.
    activity = AcctActivityRecorder(db, FakeStreamer())
    service = LiveProbeService(db, fake_schwab, gateway, max_qty=5, activity=activity,
                               marketdata=marketdata, max_notional=10,
                               streamer=FakeStreamer(), quote_wait_timeout_s=0.05)
    with pytest.raises(LiveProbeError, match=r"over the probe cap of \$10"):
        await service.place(ACCOUNT, "SPY", "buy", 1, "market", None, None, confirm=True)


async def test_place_market_order_without_a_quote_is_rejected(service):
    # ZZZZ was never cached and the fake streamer never delivers one — fail
    # closed (after the bounded wait) rather than silently skipping the
    # notional check when we genuinely don't know the price.
    with pytest.raises(LiveProbeError, match="no quote available"):
        await service.place(ACCOUNT, "ZZZZ", "buy", 1, "market", None, None, confirm=True)


async def test_place_market_order_self_subscribes_and_waits_for_a_quote(
        db, gateway, fake_schwab):
    # Nothing cached for FRESH up front — unlike `service`'s SPY, which is
    # pre-warmed by the `marketdata` fixture. Placing a market order for it
    # must trigger a subscribe and succeed once the "stream" delivers a
    # quote, without the caller ever calling preview() first.
    class DeliversQuoteStreamer(FakeStreamer):
        def __init__(self, md):
            self._md = md

        async def subscribe(self, service, symbols):
            for sym in symbols:
                self._md.cache_quote(sym, {"last": 5.0})

    md = FakeMarketData()
    activity = AcctActivityRecorder(db, FakeStreamer())
    service = LiveProbeService(db, fake_schwab, gateway, max_qty=5, activity=activity,
                               marketdata=md, max_notional=10,
                               streamer=DeliversQuoteStreamer(md), quote_wait_timeout_s=1.0)
    result = await service.place(ACCOUNT, "FRESH", "buy", 1, "market", None, None, confirm=True)
    assert result["broker_order_id"]


async def test_place_limit_order_notional_uses_limit_px_not_quote(service):
    # No quote needed/consulted for a limit order — its own price is exact.
    with pytest.raises(LiveProbeError, match=r"over the probe cap"):
        await service.place(ACCOUNT, "ZZZZ", "buy", 1, "limit", 999_999.0, None, confirm=True)


async def test_kill_switch_blocks_placement(service, gateway):
    await gateway.activate_kill_switch(actor="test")
    with pytest.raises(LiveProbeError, match="kill switch"):
        await service.place(ACCOUNT, "SPY", "buy", 1, "market", None, None, confirm=True)


async def test_cancel_requires_confirm(service):
    with pytest.raises(LiveProbeError, match="confirm"):
        await service.cancel(ACCOUNT, "some-order-id", confirm=False)


# ---- successful place journals into order_intents/orders -------------------

async def test_successful_place_journals_submitted(service, db, fake_schwab):
    result = await service.place(ACCOUNT, "SPY", "buy", 1, "market", None, None, confirm=True)
    assert result["broker_order_id"]

    intent = await db.fetch_one("SELECT * FROM order_intents WHERE id=?", (result["intent_id"],))
    assert intent["status"] == "SUBMITTED" and intent["run_id"] == PROBE_RUN_ID
    order = await db.fetch_one("SELECT * FROM orders WHERE intent_id=?", (result["intent_id"],))
    assert order["schwab_order_id"] == result["broker_order_id"]
    assert order["status"] == "SUBMITTED"
    assert fake_schwab.placed_orders[0]["orderLegCollection"][0]["quantity"] == 1


async def test_journaled_gateway_decision_is_a_list(service, db):
    # Regression: probe orders skip OrderGateway's 8-check stack entirely,
    # but api/orders.py's order_timeline (and the Orders page's .map() over
    # it) assumes gateway_decision_json always deserializes to a LIST, same
    # shape as OrderGateway.submit's own `checks` — found live: every
    # live-probe order crashed the whole SPA when expanded on /orders.
    import json
    result = await service.place(ACCOUNT, "SPY", "buy", 1, "market", None, None, confirm=True)
    intent = await db.fetch_one("SELECT gateway_decision_json FROM order_intents WHERE id=?",
                                (result["intent_id"],))
    decision = json.loads(intent["gateway_decision_json"])
    assert isinstance(decision, list)


async def test_broker_rejection_journals_rejected(service, db, fake_schwab):
    from app.schwab.client import SchwabAPIError
    fake_schwab._place_raises = SchwabAPIError(400, "invalid symbol")

    with pytest.raises(LiveProbeError):
        await service.place(ACCOUNT, "SPY", "buy", 1, "market", None, None, confirm=True)

    rows = await db.fetch_all("SELECT * FROM order_intents WHERE run_id=?", (PROBE_RUN_ID,))
    assert len(rows) == 1 and rows[0]["status"] == "REJECTED"


# ---- broker events update the journaled order's status ---------------------

async def test_fill_event_updates_local_order_to_filled(service, db, fake_schwab):
    # pre-warm the broker with a fast poll interval BEFORE placing, so the
    # in-flight poll loop's very first sleep already uses it
    broker = service._broker_for(ACCOUNT)
    broker._poll_interval_s = 0.01
    result = await service.place(ACCOUNT, "SPY", "buy", 1, "market", None, None, confirm=True)
    fake_schwab.set_status(result["broker_order_id"], "FILLED", filled_qty=1, fill_price=555.0)
    await asyncio.sleep(0.05)
    await settle()

    order = await db.fetch_one("SELECT * FROM orders WHERE intent_id=?", (result["intent_id"],))
    assert order["status"] == "FILLED"
    assert order["closed_at"] is not None
    await broker.stop()


# ---- preview: read-only, no confirm, no journaling -------------------------

async def test_preview_requires_no_confirm_and_writes_nothing(service, db, fake_schwab):
    result = await service.preview(ACCOUNT, "SPY", "buy", 1, "limit", 500.0, None)
    assert "orderValidationResult" in result
    assert fake_schwab.placed_orders == []
    rows = await db.fetch_all("SELECT * FROM order_intents WHERE run_id=?", (PROBE_RUN_ID,))
    assert rows == []


# ---- reconcile --------------------------------------------------------------

async def test_reconcile_reports_broker_and_local_state(service, fake_schwab):
    await service.place(ACCOUNT, "SPY", "buy", 1, "market", None, None, confirm=True)
    fake_schwab._account["securitiesAccount"]["positions"] = [
        {"instrument": {"symbol": "SPY"}, "longQuantity": 1, "shortQuantity": 0}]

    report = await service.reconcile(ACCOUNT)

    assert report["broker_positions"] == {"SPY": 1}
    assert len(report["local_orders"]) == 1
    await service._broker_for(ACCOUNT).stop()
