"""Smoke tests for the Phase 5.5 hidden dev-screen API (app/api/live_probe.py)
wired through the REAL app lifespan (same ASGI-transport pattern as
test_api_runs.py) — proves the live_probe_enabled config gate actually
blocks every endpoint when off (the default), and that the wiring boots
when on. Deep LiveProbeService behavior (confirm/qty-cap/kill-switch
gating, journaling, broker-event status updates) is already covered
directly against the service in test_live_probe.py — this file only proves
the HTTP layer and the gate."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_phase0 import make_settings


def make_client(tmp_path, monkeypatch, **settings_kw):
    import app.config as config_mod
    import app.main as main_mod

    s = make_settings(tmp_path, **settings_kw)
    monkeypatch.setattr(config_mod, "get_settings", lambda: s)
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)
    return main_mod.app


@pytest.fixture
async def disabled_client(tmp_path, monkeypatch):
    app = make_client(tmp_path, monkeypatch)  # live_probe_enabled defaults False
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def enabled_client(tmp_path, monkeypatch):
    app = make_client(tmp_path, monkeypatch, live_probe_enabled=True, live_probe_max_qty=2)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# ---- disabled by default: every endpoint 403s -----------------------------

async def test_status_reports_disabled_by_default(disabled_client):
    r = await disabled_client.get("/api/live-probe/status")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "max_qty": 5, "max_notional": 25, "accounts": []}


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/live-probe/preview", {}),
    ("post", "/api/live-probe/orders", {}),
    ("post", "/api/live-probe/orders/some-id/cancel", {}),
    ("put", "/api/live-probe/orders/some-id", {}),
    ("get", "/api/live-probe/orders", None),
    ("get", "/api/live-probe/activity-log", None),
])
async def test_disabled_blocks_every_mutating_and_read_endpoint(disabled_client, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    r = await getattr(disabled_client, method)(path, **kwargs)
    assert r.status_code == 403
    assert "live_probe_enabled" in r.json()["detail"]


async def test_disabled_blocks_reconcile(disabled_client):
    r = await disabled_client.get("/api/live-probe/reconcile", params={"account_hash": "H1"})
    assert r.status_code == 403


# ---- enabled: read-only local-DB endpoints work without touching Schwab ----

async def test_enabled_orders_and_activity_log_are_reachable(enabled_client):
    r = await enabled_client.get("/api/live-probe/orders")
    assert r.status_code == 200 and r.json() == {"orders": []}

    r = await enabled_client.get("/api/live-probe/activity-log")
    assert r.status_code == 200 and r.json() == {"messages": []}


async def test_enabled_status_reports_max_qty_and_requires_real_auth_for_accounts(enabled_client):
    # /status is enabled, but listing real accounts needs a real Schwab
    # connection this test env doesn't have -- 424, same convention every
    # other not-connected-yet endpoint in this app already uses.
    r = await enabled_client.get("/api/live-probe/status")
    assert r.status_code == 424


async def test_enabled_place_without_confirm_400s(enabled_client):
    r = await enabled_client.post("/api/live-probe/orders", json={
        "account_hash": "H1", "symbol": "SPY", "side": "buy", "qty": 1,
        "order_type": "market", "confirm": False})
    assert r.status_code == 400
    assert "confirm" in r.json()["detail"]


async def test_enabled_place_over_qty_cap_400s(enabled_client):
    r = await enabled_client.post("/api/live-probe/orders", json={
        "account_hash": "H1", "symbol": "SPY", "side": "buy", "qty": 999,
        "order_type": "market", "confirm": True})
    assert r.status_code == 400
    assert "cap" in r.json()["detail"]
