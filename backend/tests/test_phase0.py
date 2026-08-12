"""Phase 0 tests: settings, token store atomicity/rotation logic, authorize URL,
DB schema, health endpoint."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from pathlib import Path

import pytest

from app.auth.manager import AuthError, AuthManager
from app.auth.token_store import TokenSet, TokenStore
from app.config import Settings
from app.db import Database


def make_settings(tmp_path: Path, **kw) -> Settings:
    return Settings(
        _env_file=None,  # isolate from the real .env
        autotrader_data_dir=tmp_path,
        **kw,
    )


# ---- settings ------------------------------------------------------------

def test_settings_defaults_and_paths(tmp_path):
    s = make_settings(tmp_path)
    assert s.autotrader_port == 8484
    assert s.db_path == tmp_path / "autotrader.db"
    assert s.tokens_path == tmp_path / "tokens.json"
    assert not s.schwab_configured
    assert not s.ai_configured


def test_settings_configured_flags(tmp_path):
    s = make_settings(tmp_path, schwab_client_id="x", schwab_client_secret="y",
                      anthropic_api_key="sk-ant-test")
    assert s.schwab_configured and s.ai_configured


# ---- token store ---------------------------------------------------------

def make_tokens(access_age_s=0.0, refresh_age_s=0.0) -> TokenSet:
    now = time.time()
    return TokenSet(
        access_token="acc",
        refresh_token="ref",
        access_token_obtained_at=now - access_age_s,
        refresh_token_obtained_at=now - refresh_age_s,
    )


def test_token_store_roundtrip_and_perms(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    assert store.load() is None
    t = make_tokens()
    store.save(t)
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600
    loaded = store.load()
    assert loaded is not None and loaded.access_token == "acc"


def test_token_store_corrupt_file_preserved(tmp_path):
    p = tmp_path / "tokens.json"
    p.write_text("{not json")
    store = TokenStore(p)
    assert store.load() is None
    assert (tmp_path / "tokens.corrupt").exists()


def test_token_expiry_math():
    fresh = make_tokens()
    assert fresh.access_valid and fresh.refresh_valid
    stale_access = make_tokens(access_age_s=31 * 60)
    assert not stale_access.access_valid and stale_access.refresh_valid
    dead = make_tokens(refresh_age_s=8 * 24 * 3600)
    assert not dead.refresh_valid


# ---- auth manager --------------------------------------------------------

def test_authorize_url(tmp_path):
    s = make_settings(tmp_path, schwab_client_id="CID", schwab_client_secret="SEC")
    url = AuthManager(s).authorize_url()
    parsed = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(parsed.query)
    assert parsed.hostname == "api.schwabapi.com"
    assert q["client_id"] == ["CID"]
    assert q["redirect_uri"] == ["https://127.0.0.1:8443/oauth/callback"]


def test_authorize_url_unconfigured_raises(tmp_path):
    with pytest.raises(AuthError):
        AuthManager(make_settings(tmp_path)).authorize_url()


def test_auth_status_disconnected(tmp_path):
    st = AuthManager(make_settings(tmp_path)).status()
    assert st["connected"] is False
    assert st["access_token_expires_in"] is None


# ---- database ------------------------------------------------------------

async def test_db_schema_and_helpers(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    try:
        await db.audit("test", "unit", {"k": 1})
        rows = await db.fetch_all("SELECT * FROM audit_log")
        assert len(rows) == 1 and json.loads(rows[0]["payload_json"]) == {"k": 1}
        await db.state_set("kill_switch", {"active": False})
        assert await db.state_get("kill_switch") == {"active": False}
        assert await db.state_get("missing", "dflt") == "dflt"
        # a few key tables exist
        for table in ("candles", "strategies", "runs", "order_intents", "promotions"):
            assert await db.fetch_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
    finally:
        await db.close()


async def test_fresh_db_has_nullable_promotions_columns(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    try:
        info = {r["name"]: r["notnull"] for r in await db.fetch_all("PRAGMA table_info(promotions)")}
        assert info["backtest_run_id"] == 0
        assert info["paper_run_id"] == 0
        # both columns accept NULL (override_gate can skip both entirely)
        await db.execute(
            "INSERT INTO promotions (id, strategy_version_hash, gate_snapshot_json, confirmed_at) "
            "VALUES ('p1', 'h1', '{}', 0)")
        row = await db.fetch_one("SELECT * FROM promotions WHERE id='p1'")
        assert row["backtest_run_id"] is None and row["paper_run_id"] is None
    finally:
        await db.close()


async def test_migration_relaxes_a_preexisting_promotions_table(tmp_path):
    """Simulates a real pre-slice-4 database: promotions created with the
    OLD NOT NULL constraint (and one legitimate row in it), then reconnect
    and confirm _migrate_promotions_nullable relaxes it in place without
    losing data — the exact scenario a user's real ~/.autotrader/autotrader.db
    goes through on first startup after this change."""
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    await db.connect()
    await db.conn.executescript("""
        DROP TABLE promotions;
        CREATE TABLE promotions (
            id TEXT PRIMARY KEY, strategy_version_hash TEXT NOT NULL,
            backtest_run_id TEXT NOT NULL, paper_run_id TEXT NOT NULL,
            gate_snapshot_json TEXT NOT NULL, confirmed_at INTEGER NOT NULL
        );
    """)
    await db.conn.commit()
    await db.execute(
        "INSERT INTO promotions (id, strategy_version_hash, backtest_run_id, paper_run_id, "
        "gate_snapshot_json, confirmed_at) VALUES ('old-1', 'h1', 'bt-1', 'run-1', '{}', 123)")
    await db.close()

    db2 = Database(db_path)
    await db2.connect()
    try:
        info = {r["name"]: r["notnull"] for r in await db2.fetch_all("PRAGMA table_info(promotions)")}
        assert info["backtest_run_id"] == 0 and info["paper_run_id"] == 0
        preserved = await db2.fetch_one("SELECT * FROM promotions WHERE id='old-1'")
        assert preserved["backtest_run_id"] == "bt-1" and preserved["paper_run_id"] == "run-1"
        await db2.execute(
            "INSERT INTO promotions (id, strategy_version_hash, gate_snapshot_json, confirmed_at) "
            "VALUES ('new-1', 'h2', '{}', 456)")
    finally:
        await db2.close()


# ---- health endpoint (app wiring) ---------------------------------------

async def test_health_endpoint(tmp_path, monkeypatch):
    import app.config as config_mod
    from httpx import ASGITransport, AsyncClient

    s = make_settings(tmp_path)
    monkeypatch.setattr(config_mod, "get_settings", lambda: s)
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)

    async with main_mod.app.router.lifespan_context(main_mod.app):
        transport = ASGITransport(app=main_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/health")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "ok"
            assert body["auth"]["connected"] is False
            assert body["ai_configured"] is False
            r = await client.get("/api/accounts")
            assert r.status_code == 424  # not connected yet


async def test_coverage_endpoint_reports_every_native_tf(tmp_path, monkeypatch):
    """The Data page renders a column per native tf — coverage must report all
    of them (1m, the Schwab-native intraday tfs, and 1d)."""
    import app.config as config_mod
    from httpx import ASGITransport, AsyncClient

    from app.api.routes import COVERAGE_TFS
    from app.marketdata.store import Candle

    assert set(COVERAGE_TFS) == {"1m", "5m", "15m", "30m", "1d"}

    s = make_settings(tmp_path)
    monkeypatch.setattr(config_mod, "get_settings", lambda: s)
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)

    async with main_mod.app.router.lifespan_context(main_mod.app):
        store = main_mod.app.state.marketdata.store
        for tf in COVERAGE_TFS:
            await store.upsert(
                "SPY", tf,
                [Candle(ts=1_750_000_000_000, open=1, high=2, low=0.5, close=1.5,
                        volume=10)],
                "schwab_rest",
            )
        transport = ASGITransport(app=main_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/api/data/coverage")
            assert r.status_code == 200
            entry = next(e for e in r.json()["coverage"] if e["symbol"] == "SPY")
            for tf in COVERAGE_TFS:
                assert entry[tf] is not None and entry[tf]["bars"] == 1, tf
