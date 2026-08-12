"""Smoke tests for the execution-safety API (app/api/execution.py) wired
through the REAL app lifespan (same ASGI-transport pattern as
test_api_live_probe.py) — the HTTP layer over OrderGateway.resolve_unknown_intent,
already covered directly against the gateway in test_gateway.py."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_phase0 import make_settings


@pytest.fixture
async def client(tmp_path, monkeypatch):
    import app.config as config_mod
    import app.main as main_mod

    s = make_settings(tmp_path)
    monkeypatch.setattr(config_mod, "get_settings", lambda: s)
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)
    app = main_mod.app
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, app


async def insert_unknown_intent(app) -> str:
    intent_id = str(uuid.uuid4())
    db = app.state.db
    await db.execute(
        """INSERT INTO order_intents (id, run_id, ts, symbol, side, qty, order_type,
           purpose, status) VALUES (?,?,?,?,?,?,?,?,?)""",
        (intent_id, "run-1", 0, "SPY", "buy", 1, "market", "entry", "UNKNOWN"))
    return intent_id


async def test_list_unknown_orders_empty_by_default(client):
    c, _ = client
    r = await c.get("/api/execution/unknown-orders")
    assert r.status_code == 200
    assert r.json() == {"intents": []}


async def test_list_and_resolve_unknown_order(client):
    c, app = client
    intent_id = await insert_unknown_intent(app)

    listed = await c.get("/api/execution/unknown-orders")
    assert listed.status_code == 200
    assert [i["id"] for i in listed.json()["intents"]] == [intent_id]

    resolved = await c.post(f"/api/execution/unknown-orders/{intent_id}/resolve",
                            json={"resolution": "confirmed never placed"})
    assert resolved.status_code == 200
    assert resolved.json() == {"intent_id": intent_id, "status": "RESOLVED_MANUAL"}

    listed_after = await c.get("/api/execution/unknown-orders")
    assert listed_after.json() == {"intents": []}


async def test_resolve_requires_a_resolution_body(client):
    c, app = client
    intent_id = await insert_unknown_intent(app)
    r = await c.post(f"/api/execution/unknown-orders/{intent_id}/resolve", json={})
    assert r.status_code == 400


async def test_resolve_nonexistent_intent_404s_as_400(client):
    c, _ = client
    r = await c.post("/api/execution/unknown-orders/does-not-exist/resolve",
                     json={"resolution": "n/a"})
    assert r.status_code == 400


async def test_resolve_already_resolved_intent_rejects(client):
    c, app = client
    intent_id = await insert_unknown_intent(app)
    first = await c.post(f"/api/execution/unknown-orders/{intent_id}/resolve",
                         json={"resolution": "first"})
    assert first.status_code == 200
    second = await c.post(f"/api/execution/unknown-orders/{intent_id}/resolve",
                          json={"resolution": "second"})
    assert second.status_code == 400


# ---- open orders (live, direct from Schwab -- independent of any run) ------
#
# Regression context (found live 2026-07-24): a run's SchwabBroker instance
# is the ONLY thing that tracks a live order in-process; once a run stops
# (cleanly or force-closed after a restart), that instance is gone and the
# order becomes invisible/uncancelable through the rest of this app. These
# routes intentionally go straight to Schwab instead of through any run.

class FakeSchwabOrders:
    def __init__(self, orders_by_account: dict[str, list[dict]]):
        self._orders_by_account = orders_by_account
        self.canceled: list[tuple[str, str]] = []

    async def get_account_numbers(self):
        return [{"hashValue": h, "accountNumber": h} for h in self._orders_by_account]

    async def get_orders(self, account_hash, from_iso, to_iso, status=None, max_results=3000):
        return self._orders_by_account.get(account_hash, [])

    async def cancel_order(self, account_hash, order_id):
        self.canceled.append((account_hash, order_id))


async def test_open_orders_without_schwab_connection_returns_424(client):
    c, _ = client
    r = await c.get("/api/execution/open-orders")
    assert r.status_code == 424


async def test_open_orders_filters_out_closed_statuses(client):
    c, app = client
    app.state.schwab = FakeSchwabOrders({
        "acct-1": [
            {"orderId": 1, "status": "PENDING_ACTIVATION", "enteredTime": "2026-07-24T20:12:03.000Z"},
            {"orderId": 2, "status": "FILLED", "enteredTime": "2026-07-24T19:00:00.000Z"},
            {"orderId": 3, "status": "WORKING", "enteredTime": "2026-07-24T20:00:00.000Z"},
            {"orderId": 4, "status": "CANCELED", "enteredTime": "2026-07-24T18:00:00.000Z"},
        ],
    })
    r = await c.get("/api/execution/open-orders")
    assert r.status_code == 200
    orders = r.json()["orders"]
    assert [o["orderId"] for o in orders] == [1, 3]  # newest enteredTime first
    assert all(o["account_hash"] == "acct-1" for o in orders)


async def test_open_orders_spans_every_connected_account(client):
    c, app = client
    app.state.schwab = FakeSchwabOrders({
        "acct-1": [{"orderId": 1, "status": "WORKING", "enteredTime": "2026-07-24T20:00:00.000Z"}],
        "acct-2": [{"orderId": 2, "status": "QUEUED", "enteredTime": "2026-07-24T19:00:00.000Z"}],
    })
    r = await c.get("/api/execution/open-orders")
    assert sorted(o["orderId"] for o in r.json()["orders"]) == [1, 2]


async def test_cancel_open_order_requires_account_hash(client):
    c, app = client
    app.state.schwab = FakeSchwabOrders({})
    r = await c.post("/api/execution/open-orders/123/cancel", json={})
    assert r.status_code == 400


async def test_cancel_open_order_calls_schwab_and_audits(client):
    c, app = client
    fake = FakeSchwabOrders({})
    app.state.schwab = fake
    r = await c.post("/api/execution/open-orders/123/cancel", json={"account_hash": "acct-1"})
    assert r.status_code == 200
    assert r.json()["order_id"] == "123"
    assert fake.canceled == [("acct-1", "123")]
    audit_rows = await app.state.db.fetch_all(
        "SELECT category, payload_json FROM audit_log WHERE category='run_control' ORDER BY id DESC LIMIT 1")
    assert len(audit_rows) == 1
    assert "order_canceled_manual" in audit_rows[0]["payload_json"]
