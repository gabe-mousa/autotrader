"""Smoke test for GET /api/activity (audit-log tail for the dashboard's
recent-activity feed), wired through the REAL app lifespan (same ASGI-
transport pattern as test_api_runs.py)."""

from __future__ import annotations

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

    async with main_mod.app.router.lifespan_context(main_mod.app):
        transport = ASGITransport(app=main_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, main_mod.app


async def test_activity_reflects_startup_row(client):
    # the app lifespan itself writes a "lifecycle: startup" audit row, so the
    # feed is never actually empty on a freshly booted app.
    c, _ = client
    r = await c.get("/api/activity")
    assert r.status_code == 200
    body = r.json()
    assert len(body["activity"]) == 1
    assert body["activity"][0]["category"] == "lifecycle"


async def test_activity_reflects_audit_rows(client):
    c, app = client
    await app.state.db.audit("user", "kill_switch", {"event": "activated", "cancel_all": True})
    await app.state.db.audit("system", "strategy", {"slug": "spy-rsi-dip-buyer"})

    r = await c.get("/api/activity")
    assert r.status_code == 200
    body = r.json()
    assert len(body["activity"]) == 3
    # most recent first
    assert body["activity"][0]["category"] == "strategy"
    assert body["activity"][0]["actor"] == "system"
    assert body["activity"][0]["payload"] == {"slug": "spy-rsi-dip-buyer"}
    assert body["activity"][1]["category"] == "kill_switch"
    assert body["activity"][1]["payload"]["event"] == "activated"


async def test_activity_respects_limit(client):
    c, app = client
    for i in range(5):
        await app.state.db.audit("user", "test", {"i": i})

    r = await c.get("/api/activity", params={"limit": 3})
    assert r.status_code == 200
    assert len(r.json()["activity"]) == 3
