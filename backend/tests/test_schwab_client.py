"""SchwabClient order-endpoint tests (Phase 5.5) — real httpx.MockTransport
so the actual HTTP shape (method, URL, JSON body, Location-header order-id
parsing, error mapping) is verified, not just a hand-rolled fake. SchwabClient
had zero direct tests before this — every other consumer only ever faked the
client's own interface. These are the first new real-money-adjacent methods
added to it, so the wire shape is worth verifying directly against the
captured docs (schwab/trader-api--individual/README.md)."""

from __future__ import annotations

import httpx
import pytest

from app.schwab.client import SchwabAPIError, SchwabClient

TRADER_BASE = "https://api.schwabapi.com/trader/v1"


class FakeAuth:
    async def get_access_token(self) -> str:
        return "fake-token"

    async def refresh(self) -> None:
        pass


def client_with_transport(handler) -> SchwabClient:
    c = SchwabClient(FakeAuth())
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30)
    return c


async def test_place_order_posts_body_and_parses_location():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(201, headers={"Location": f"{TRADER_BASE}/accounts/H1/orders/98765"})

    c = client_with_transport(handler)
    order = {"orderType": "MARKET", "session": "NORMAL", "duration": "DAY",
             "orderStrategyType": "SINGLE", "orderLegCollection": [
                 {"instruction": "BUY", "quantity": 1,
                  "instrument": {"symbol": "SPY", "assetType": "EQUITY"}}]}
    order_id = await c.place_order("H1", order)

    assert order_id == "98765"
    assert seen["method"] == "POST"
    assert seen["url"] == f"{TRADER_BASE}/accounts/H1/orders"
    assert b'"orderType":"MARKET"' in seen["body"] or b'"orderType": "MARKET"' in seen["body"]
    await c.close()


async def test_place_order_missing_location_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201)  # no Location header -- malformed response

    c = client_with_transport(handler)
    with pytest.raises(SchwabAPIError):
        await c.place_order("H1", {})
    await c.close()


async def test_place_order_400_raises_schwab_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"message": "invalid symbol"}')

    c = client_with_transport(handler)
    with pytest.raises(SchwabAPIError) as exc_info:
        await c.place_order("H1", {})
    assert exc_info.value.status == 400
    await c.close()


async def test_get_order_returns_parsed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/trader/v1/accounts/H1/orders/555"
        return httpx.Response(200, json={"orderId": 555, "status": "WORKING"})

    c = client_with_transport(handler)
    order = await c.get_order("H1", "555")
    assert order["status"] == "WORKING"
    await c.close()


async def test_cancel_order_sends_delete():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200)

    c = client_with_transport(handler)
    await c.cancel_order("H1", "555")
    assert seen["method"] == "DELETE"
    assert seen["url"] == f"{TRADER_BASE}/accounts/H1/orders/555"
    await c.close()


async def test_replace_order_puts_and_parses_new_location():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/trader/v1/accounts/H1/orders/555"
        return httpx.Response(201, headers={"Location": f"{TRADER_BASE}/accounts/H1/orders/556"})

    c = client_with_transport(handler)
    new_id = await c.replace_order("H1", "555", {"orderType": "LIMIT"})
    assert new_id == "556"
    await c.close()


async def test_preview_order_returns_validation_result():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/trader/v1/accounts/H1/previewOrder"
        return httpx.Response(200, json={"orderValidationResult": {"validationRules": []},
                                         "commissionAndFee": {"commission": {}}})

    c = client_with_transport(handler)
    result = await c.preview_order("H1", {"orderType": "MARKET"})
    assert "orderValidationResult" in result
    await c.close()


async def test_get_orders_sends_time_window_query_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[{"orderId": 1}])

    c = client_with_transport(handler)
    orders = await c.get_orders("H1", "2026-07-24T00:00:00.000Z", "2026-07-24T23:59:59.000Z",
                                status="WORKING")
    assert len(orders) == 1
    assert seen["params"]["status"] == "WORKING"
    assert seen["params"]["fromEnteredTime"] == "2026-07-24T00:00:00.000Z"
    await c.close()
