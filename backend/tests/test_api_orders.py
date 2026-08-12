"""Smoke tests for the read-only Orders & Transaction History API
(app/api/orders.py) wired through the REAL app lifespan (same ASGI-transport
pattern as test_api_runs.py/test_api_live_probe.py). Seeds order_intents/
orders/run_events rows directly (same INSERT patterns as
test_recovery.py's seed_dangling_run) covering: a paper order with a fill
in run_events, a rejected-at-gateway intent with no orders row, and a
live-probe sentinel order with no matching runs row at all."""

from __future__ import annotations

import json
import time

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
            yield c, main_mod.app.state.db


async def _seed(db):
    now = int(time.time() * 1000)

    # a real strategy + paper run, one filled buy order with a run_events fill
    await db.execute(
        "INSERT INTO strategies (id, slug, name, current_version, state, file_path, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("strat-1", "spy-dip", "SPY Dip Buyer", 1, "active", "spy-dip.yaml", now),
    )
    await db.execute(
        """INSERT INTO runs (id, strategy_id, strategy_version_hash, mode, state,
           resume_policy, started_at, starting_equity) VALUES (?,?,?,?,?,?,?,?)""",
        ("run-1", "strat-1", "hash1", "paper", "RUNNING", "auto_resume", now - 3600_000,
         100_000.0),
    )
    await db.execute(
        """INSERT INTO order_intents (id, run_id, ts, symbol, side, qty, order_type,
           purpose, status, gateway_decision_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("intent-filled", "run-1", now - 1000, "SPY", "buy", 10, "market", "entry",
         "SUBMITTED", json.dumps([{"check": "risk_limits", "passed": True}])),
    )
    await db.execute(
        """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag, status,
           submitted_at, closed_at, raw_last_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("order-filled", "intent-filled", "run-1", "paper", None, "tag1", "SUBMITTED",
         now - 900, None, None),
    )
    await db.execute(
        "INSERT INTO run_events (run_id, ts, level, kind, payload_json) VALUES (?,?,?,?,?)",
        ("run-1", now - 800, "info", "fill",
         json.dumps({"symbol": "SPY", "side": "buy", "qty": 10, "price": 512.3})),
    )

    # rejected-at-gateway intent: no orders row at all
    await db.execute(
        """INSERT INTO order_intents (id, run_id, ts, symbol, side, qty, order_type,
           purpose, status, gateway_decision_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("intent-rejected", "run-1", now - 500, "TSLA", "buy", 5, "market", "entry",
         "REJECTED_LOCAL", json.dumps([{"check": "kill_switch", "passed": False}])),
    )

    # live-probe sentinel: run_id has NO matching runs row
    await db.execute(
        """INSERT INTO order_intents (id, run_id, ts, symbol, side, qty, order_type,
           purpose, status, gateway_decision_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("intent-probe", "live-probe", now - 200, "AAPL", "buy", 1, "market", "probe",
         "SUBMITTED", "[]"),
    )
    await db.execute(
        """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag, status,
           submitted_at, closed_at, raw_last_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("order-probe", "intent-probe", "live-probe", "live", "broker-123", None, "FILLED",
         now - 100, now - 50, None),
    )


async def test_list_orders_returns_all_three_without_crashing(client):
    c, db = client
    await _seed(db)

    r = await c.get("/api/orders")
    assert r.status_code == 200
    orders = r.json()["orders"]
    assert len(orders) == 3

    by_id = {o["intent_id"]: o for o in orders}

    filled = by_id["intent-filled"]
    assert filled["status"] == "SUBMITTED"
    assert filled["name"] == "SPY Dip Buyer"
    assert filled["slug"] == "spy-dip"
    assert filled["mode"] == "paper"
    assert filled["is_live_probe"] is False

    rejected = by_id["intent-rejected"]
    assert rejected["order_id"] is None
    assert rejected["status"] == "REJECTED_LOCAL"

    probe = by_id["intent-probe"]
    assert probe["is_live_probe"] is True
    assert probe["name"] == "(live probe)"
    assert probe["status"] == "FILLED"
    assert probe["mode"] == "live"


async def test_list_orders_filters(client):
    c, db = client
    await _seed(db)

    r = await c.get("/api/orders", params={"symbol": "tsla"})
    assert r.status_code == 200
    orders = r.json()["orders"]
    assert len(orders) == 1
    assert orders[0]["intent_id"] == "intent-rejected"

    r = await c.get("/api/orders", params={"mode": "live"})
    assert len(r.json()["orders"]) == 1

    # regression: a rejected intent with no `orders` row (so no o.mode) must
    # still match its run's mode -- it shouldn't vanish from every mode filter
    r = await c.get("/api/orders", params={"mode": "paper"})
    modes = {o["intent_id"] for o in r.json()["orders"]}
    assert {"intent-filled", "intent-rejected"} <= modes

    r = await c.get("/api/orders", params={"status": "REJECTED_LOCAL"})
    assert len(r.json()["orders"]) == 1


async def test_timeline_correlates_fill_event(client):
    c, db = client
    await _seed(db)

    r = await c.get("/api/orders/intent-filled/timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["intent"]["symbol"] == "SPY"
    assert body["intent"]["gateway_decision"] == [{"check": "risk_limits", "passed": True}]
    assert body["order"]["id"] == "order-filled"
    assert body["is_live_probe"] is False
    assert len(body["matched_events"]) == 1
    assert body["matched_events"][0]["payload"]["price"] == 512.3


async def test_timeline_handles_intent_with_no_orders_row(client):
    c, db = client
    await _seed(db)

    r = await c.get("/api/orders/intent-rejected/timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["order"] is None
    assert body["matched_events"] == []


async def test_timeline_404s_for_unknown_intent(client):
    c, _db = client
    r = await c.get("/api/orders/does-not-exist/timeline")
    assert r.status_code == 404


async def test_export_csv(client):
    c, db = client
    await _seed(db)

    r = await c.get("/api/orders/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0].split(",")[:5] == ["time", "strategy", "mode", "symbol", "side"]
    assert len(lines) == 4  # header + 3 rows
