"""SchwabBroker tests: order translation to Schwab's documented OrderRequest
shape, REJECTED-vs-UNKNOWN exception mapping, poll-based fill/cancel/reject
detection, and positions/account extraction — against a `FakeSchwabClient`
double (same "fake the interface, not the wire" convention every other
Schwab-consumer test in this repo already uses)."""

from __future__ import annotations

import asyncio

import pytest

from app.broker.base import BrokerRejection, OrderRequest
from app.broker.schwab import SchwabBroker
from app.schwab.client import SchwabAPIError

ACCOUNT = "H1"


class EventCollector:
    def __init__(self):
        self.events: list = []

    async def __call__(self, event) -> None:
        self.events.append(event)


class FakeSchwabClient:
    def __init__(self):
        self.placed_orders: list[dict] = []
        self.canceled: list[str] = []
        self.replaced: list[tuple[str, dict]] = []
        self._next_id = 1000
        self._orders: dict[str, dict] = {}
        self._place_raises: Exception | None = None
        self._account: dict = {"securitiesAccount": {
            "type": "CASH", "currentBalances": {"cashBalance": 100_000.0,
                                                "liquidationValue": 100_000.0,
                                                "cashAvailableForTrading": 100_000.0},
            "positions": []}}

    async def place_order(self, account_hash: str, order: dict) -> str:
        assert account_hash == ACCOUNT
        if self._place_raises:
            raise self._place_raises
        self.placed_orders.append(order)
        oid = str(self._next_id)
        self._next_id += 1
        self._orders[oid] = {"orderId": int(oid), "status": "WORKING", "filledQuantity": 0.0,
                             "orderActivityCollection": []}
        return oid

    async def get_order(self, account_hash: str, order_id: str) -> dict:
        return self._orders[order_id]

    async def cancel_order(self, account_hash: str, order_id: str) -> None:
        self.canceled.append(order_id)

    async def replace_order(self, account_hash: str, order_id: str, order: dict) -> str:
        self.replaced.append((order_id, order))
        new_id = str(self._next_id)
        self._next_id += 1
        self._orders[new_id] = {"orderId": int(new_id), "status": "WORKING",
                                "filledQuantity": 0.0, "orderActivityCollection": []}
        return new_id

    async def get_account(self, account_hash: str, positions: bool = False) -> dict:
        return self._account

    async def preview_order(self, account_hash: str, order: dict) -> dict:
        self.previewed = order
        return {"orderValidationResult": {"validationRules": []}, "commissionAndFee": {}}

    async def get_orders(self, account_hash: str, from_iso: str, to_iso: str,
                         status: str | None = None, max_results: int = 3000) -> list[dict]:
        return list(self._orders.values())

    # ---- test helpers to simulate server-side state changes -------------
    def set_status(self, order_id: str, status: str, filled_qty: float = 0.0,
                   fill_price: float | None = None, status_description: str | None = None) -> None:
        activities = []
        if fill_price is not None and filled_qty > 0:
            activities = [{"executionLegs": [{"price": fill_price, "quantity": filled_qty}]}]
        self._orders[order_id] = {
            "orderId": int(order_id), "status": status, "filledQuantity": filled_qty,
            "orderActivityCollection": activities, "statusDescription": status_description,
        }


def buy(symbol="SPY", qty=1, order_type="market", limit_px=None, stop_px=None, intent="i-1"):
    return OrderRequest(intent_id=intent, symbol=symbol, side="buy", qty=qty,
                        order_type=order_type, limit_px=limit_px, stop_px=stop_px)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def fake():
    return FakeSchwabClient()


@pytest.fixture
def broker(fake):
    b = SchwabBroker(fake, ACCOUNT, poll_interval_s=0.01)
    coll = EventCollector()
    b.subscribe_events(coll)
    b._collector = coll  # type: ignore[attr-defined]
    return b


# ---- order translation -----------------------------------------------------

async def test_market_order_translates_to_documented_shape(broker, fake):
    handle = await broker.place(buy(qty=1))
    assert handle.status == "WORKING"
    order = fake.placed_orders[0]
    assert order["orderType"] == "MARKET"
    assert order["session"] == "NORMAL"
    assert order["duration"] == "DAY"
    assert order["orderStrategyType"] == "SINGLE"
    assert "tag" not in order  # Schwab 400s this account's orders if "tag" is present at all
    leg = order["orderLegCollection"][0]
    assert leg == {"instruction": "BUY", "quantity": 1,
                   "instrument": {"symbol": "SPY", "assetType": "EQUITY"}}
    assert "price" not in order and "stopPrice" not in order
    await broker.stop()


async def test_limit_order_includes_price(broker, fake):
    await broker.place(buy(order_type="limit", limit_px=123.45))
    assert fake.placed_orders[0]["price"] == 123.45
    await broker.stop()


async def test_stop_order_includes_stop_price(broker, fake):
    await broker.place(buy(order_type="stop", stop_px=99.5))
    assert fake.placed_orders[0]["stopPrice"] == 99.5
    await broker.stop()


# ---- rejection semantics ----------------------------------------------------

async def test_4xx_raises_broker_rejection(broker, fake):
    fake._place_raises = SchwabAPIError(400, "invalid symbol")
    with pytest.raises(BrokerRejection):
        await broker.place(buy())


async def test_5xx_propagates_unwrapped_for_unknown_handling(broker, fake):
    fake._place_raises = SchwabAPIError(503, "upstream unavailable")
    with pytest.raises(SchwabAPIError):
        await broker.place(buy())


# ---- poll-based fill/cancel/reject detection --------------------------------

async def test_poll_detects_fill_and_emits_event(broker, fake):
    handle = await broker.place(buy(qty=1))
    fake.set_status(handle.broker_order_id, "FILLED", filled_qty=1, fill_price=512.34)
    await asyncio.sleep(0.05)
    await settle()

    fills = [e for e in broker._collector.events if e.kind == "fill"]  # type: ignore[attr-defined]
    assert len(fills) == 1
    assert fills[0].fill.qty == 1
    assert fills[0].fill.price == 512.34
    assert handle.broker_order_id not in broker._working
    await broker.stop()


async def test_poll_detects_cancel(broker, fake):
    handle = await broker.place(buy())
    fake.set_status(handle.broker_order_id, "CANCELED", status_description="user canceled")
    await asyncio.sleep(0.05)
    await settle()

    kinds = [e.kind for e in broker._collector.events]  # type: ignore[attr-defined]
    assert "canceled" in kinds
    assert handle.broker_order_id not in broker._working
    await broker.stop()


async def test_poll_detects_reject(broker, fake):
    handle = await broker.place(buy())
    fake.set_status(handle.broker_order_id, "REJECTED", status_description="no funds")
    await asyncio.sleep(0.05)
    await settle()

    kinds = [e.kind for e in broker._collector.events]  # type: ignore[attr-defined]
    assert "rejected" in kinds
    await broker.stop()


async def test_cancel_calls_client_without_optimistic_emit(broker, fake):
    handle = await broker.place(buy())
    await broker.cancel(handle.broker_order_id)
    assert fake.canceled == [handle.broker_order_id]
    kinds = [e.kind for e in broker._collector.events]  # type: ignore[attr-defined]
    assert "canceled" not in kinds  # only the poller confirms it
    await broker.stop()


async def test_replace_tracks_the_new_order_id(broker, fake):
    handle = await broker.place(buy())
    new_handle = await broker.replace(handle.broker_order_id, buy(order_type="limit", limit_px=100))
    assert new_handle.broker_order_id != handle.broker_order_id
    assert handle.broker_order_id not in broker._working
    assert new_handle.broker_order_id in broker._working
    old_id, replaced_order = fake.replaced[0]
    assert old_id == handle.broker_order_id
    assert replaced_order["orderType"] == "LIMIT"
    await broker.stop()


# ---- positions / account ----------------------------------------------------

async def test_get_positions_nets_long_and_short_quantity(broker, fake):
    fake._account["securitiesAccount"]["positions"] = [
        {"instrument": {"symbol": "SPY"}, "longQuantity": 10, "shortQuantity": 0},
    ]
    positions = await broker.get_positions()
    assert positions == {"SPY": 10}


async def test_get_account_reports_balances_and_positions(broker, fake):
    fake._account["securitiesAccount"]["positions"] = [
        {"instrument": {"symbol": "SPY"}, "longQuantity": 5, "shortQuantity": 0},
    ]
    account = await broker.get_account()
    assert account["cash"] == 100_000.0
    assert account["positions"] == {"SPY": 5}


# ---- preview / working_order_ids --------------------------------------------

async def test_preview_builds_the_same_order_shape_without_placing(broker, fake):
    result = await broker.preview(buy(order_type="limit", limit_px=200.0))
    assert "orderValidationResult" in result
    assert fake.previewed["orderType"] == "LIMIT"
    assert fake.previewed["price"] == 200.0
    assert fake.placed_orders == []  # never actually placed


async def test_working_order_ids_reflects_open_orders(broker, fake):
    handle = await broker.place(buy())
    assert broker.working_order_ids() == [handle.broker_order_id]
    fake.set_status(handle.broker_order_id, "FILLED", filled_qty=1, fill_price=100.0)
    await asyncio.sleep(0.05)
    await settle()
    assert broker.working_order_ids() == []
    await broker.stop()
