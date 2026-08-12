"""Smoke tests for the run-control API (app/api/runs.py) wired through the
REAL app lifespan (same ASGI-transport pattern as test_phase0.py's health
test) — proves RunManager/OrderGateway/BarAggregator construction in
main.py's lifespan doesn't blow up, and that the kill-switch/risk-limits/
runs-list endpoints are reachable end-to-end. Deep run-control behavior
(promotion gate, global risk gating, protective orders, etc.) is already
covered against RunManager directly in test_run_manager.py/test_recovery.py
— this file only proves the HTTP wiring."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_phase0 import make_settings


def main_app_state():
    import app.main as main_mod
    return main_mod.app.state


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
            yield c


async def test_runs_list_starts_empty(client):
    r = await client.get("/api/runs")
    assert r.status_code == 200
    assert r.json() == {"runs": []}


async def test_start_run_rejects_unknown_slug(client):
    r = await client.post("/api/runs", json={"slug": "does-not-exist"})
    assert r.status_code == 404


async def test_run_control_on_unknown_run_id_404s(client):
    for action in ("pause", "resume", "flatten", "stop"):
        r = await client.post(f"/api/runs/not-a-real-id/{action}")
        assert r.status_code == 404


async def test_recover_on_unknown_run_id_404s(client):
    r = await client.post("/api/runs/not-a-real-id/recover")
    assert r.status_code == 404


async def test_kill_switch_round_trip(client):
    r = await client.get("/api/kill")
    assert r.status_code == 200 and r.json() == {"active": False}

    r = await client.post("/api/kill", json={})
    assert r.status_code == 200
    assert r.json() == {"active": True, "failed_cancels_by_run": {}}

    r = await client.get("/api/kill")
    assert r.json() == {"active": True}

    r = await client.delete("/api/kill")
    assert r.status_code == 200 and r.json() == {"active": False}


# ---- promotion to live (Phase 6 slice 2) — HTTP wiring only ---------------
# Deep start_live/activate_live behavior (promotion gate, paused-armed
# invariant, etc.) is covered against RunManager directly in
# test_run_manager.py — this only proves the endpoints are wired.

async def test_live_accounts_without_schwab_connection_returns_424(client):
    r = await client.get("/api/runs/live/accounts")
    assert r.status_code == 424


async def test_start_live_rejects_unknown_slug(client):
    r = await client.post("/api/runs/live",
                          json={"slug": "does-not-exist", "account_hash": "H1"})
    assert r.status_code == 404


async def test_start_live_requires_slug_and_account_hash(client):
    r = await client.post("/api/runs/live", json={"account_hash": "H1"})
    assert r.status_code == 400
    r = await client.post("/api/runs/live", json={"slug": "spy-dip"})
    assert r.status_code == 400


async def test_activate_unknown_run_id_400s(client):
    r = await client.post("/api/runs/not-a-real-id/activate")
    assert r.status_code == 400


async def test_eligibility_unknown_slug_404s(client):
    r = await client.get("/api/runs/live/eligibility", params={"slug": "does-not-exist"})
    assert r.status_code == 404


async def test_risk_limits_round_trip(client):
    r = await client.get("/api/risk/limits")
    assert r.status_code == 200
    defaults = r.json()
    assert defaults["max_total_notional"] == 250_000.0

    r = await client.put("/api/risk/limits", json={"max_total_notional": 500.0})
    assert r.status_code == 200
    assert r.json()["max_total_notional"] == 500.0

    r = await client.get("/api/risk/limits")
    assert r.json()["max_total_notional"] == 500.0

    r = await client.put("/api/risk/limits", json={"not_a_real_key": 1})
    assert r.status_code == 400


# ---- lifetime realized P&L (the "Total P&L %" column) ---------------------

async def _seed_run_with_fills(db, run_id: str, starting_equity: float,
                               realized: list[float]) -> None:
    """A finished run with `realized` closing fills, straight into the tables
    — the API reads the same journal recovery does, so no runner is needed."""
    await db.execute(
        "INSERT INTO strategies (id, slug, name, current_version, state, file_path, "
        "updated_at) VALUES (?,?,?,?,?,?,?)",
        (f"s-{run_id}", f"slug-{run_id}", "Seeded", 1, "active", "x.yaml", 0))
    await db.execute(
        "INSERT INTO runs (id, strategy_id, strategy_version_hash, mode, state, "
        "started_at, starting_equity) VALUES (?,?,?,?,?,?,?)",
        (run_id, f"s-{run_id}", "hash", "paper", "STOPPED", 1, starting_equity))
    for i, pnl in enumerate(realized):
        await db.execute(
            "INSERT INTO run_events (run_id, ts, level, kind, payload_json) "
            "VALUES (?,?,?,?,?)",
            (run_id, i, "info", "fill", json.dumps({"symbol": "SPY", "realized_pnl": pnl})))
    # an opening fill carries no realized_pnl at all and must not break the SUM
    await db.execute(
        "INSERT INTO run_events (run_id, ts, level, kind, payload_json) VALUES (?,?,?,?,?)",
        (run_id, 99, "info", "fill", json.dumps({"symbol": "SPY", "qty": 10})))


async def test_run_list_and_detail_expose_total_realized_pnl(client):
    db = main_app_state().db
    await _seed_run_with_fills(db, "run-total", 10_000.0, [150.0, -50.0, 25.0])

    r = await client.get("/api/runs")
    row = next(x for x in r.json()["runs"] if x["id"] == "run-total")
    assert row["realized_pnl_total"] == 125.0
    assert row["realized_pnl_total_pct"] == 1.25

    r = await client.get("/api/runs/run-total")
    assert r.json()["realized_pnl_total"] == 125.0
    assert r.json()["realized_pnl_total_pct"] == 1.25


async def test_total_pnl_pct_is_null_without_starting_equity(client):
    db = main_app_state().db
    await _seed_run_with_fills(db, "run-no-equity", 0.0, [10.0])

    r = await client.get("/api/runs/run-no-equity")
    assert r.json()["realized_pnl_total"] == 10.0
    assert r.json()["realized_pnl_total_pct"] is None


async def test_run_without_fills_totals_zero(client):
    db = main_app_state().db
    await _seed_run_with_fills(db, "run-no-fills", 10_000.0, [])

    r = await client.get("/api/runs/run-no-fills")
    assert r.json()["realized_pnl_total"] == 0.0
    assert r.json()["realized_pnl_total_pct"] == 0.0
