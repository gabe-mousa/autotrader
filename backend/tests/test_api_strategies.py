"""API-level tests for the strategy star/favorite endpoint (app/api/strategies.py),
wired through the REAL app lifespan (same pattern as test_api_backtests.py)."""

from __future__ import annotations

import copy
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_phase0 import make_settings
from tests.test_strategy import EXAMPLE


@pytest.fixture
async def client(tmp_path, monkeypatch):
    import app.config as config_mod
    import app.main as main_mod

    s = make_settings(tmp_path, autotrader_strategies_dir=tmp_path / "strategies")
    monkeypatch.setattr(config_mod, "get_settings", lambda: s)
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)

    async with main_mod.app.router.lifespan_context(main_mod.app):
        transport = ASGITransport(app=main_mod.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            c._app_state = main_mod.app.state  # type: ignore[attr-defined]
            yield c


async def _seed(client, name: str) -> str:
    from app.strategy.files import slugify
    from app.strategy.schema import StrategyDocument

    data = copy.deepcopy(EXAMPLE)
    data["meta"]["name"] = name
    doc = StrategyDocument.model_validate(data)
    slug = slugify(name)
    client._app_state.strategy_files.write(slug, doc, freeze=True)
    await client._app_state.strategy_files.reindex()
    return slug


async def test_star_toggles_and_persists(client):
    slug = await _seed(client, "star me")

    listed = (await client.get("/api/strategies")).json()["strategies"]
    assert [s["starred"] for s in listed] == [False]

    r = await client.post(f"/api/strategies/{slug}/star", json={"starred": True})
    assert r.status_code == 200 and r.json() == {"slug": slug, "starred": True}

    listed = (await client.get("/api/strategies")).json()["strategies"]
    assert listed[0]["starred"] is True

    await client.post(f"/api/strategies/{slug}/star", json={"starred": False})
    listed = (await client.get("/api/strategies")).json()["strategies"]
    assert listed[0]["starred"] is False


async def test_starring_never_bumps_the_version(client):
    slug = await _seed(client, "version stable")
    files = client._app_state.strategy_files
    before = files.load(slug).meta.version
    versions_dir = files.root / slug / "versions"
    snapshots = len(list(versions_dir.glob("*")))

    await client.post(f"/api/strategies/{slug}/star", json={"starred": True})

    doc = files.load(slug)
    assert doc.meta.version == before and doc.meta.starred is True
    assert len(list(versions_dir.glob("*"))) == snapshots  # no new frozen snapshot


async def test_starring_does_not_reorder_the_list(client):
    for name in ("alpha strat", "bravo strat", "charlie strat"):
        await _seed(client, name)
    before = [s["slug"] for s in (await client.get("/api/strategies")).json()["strategies"]]

    await client.post("/api/strategies/charlie-strat/star", json={"starred": True})

    after = (await client.get("/api/strategies")).json()["strategies"]
    assert [s["slug"] for s in after] == before
    assert next(s for s in after if s["slug"] == "charlie-strat")["starred"] is True


async def test_star_unknown_slug_404s(client):
    r = await client.post("/api/strategies/nope/star", json={"starred": True})
    assert r.status_code == 404


async def _insert_backtest(client, *, strategy_id, started_at, total_return_pct,
                           max_drawdown_pct=-4.0, sharpe=1.25,
                           start="2023-01-01", end="2023-06-30", sweep_id=None,
                           status="done", run_id=None, hash_="deadbeef"):
    """Write a backtest row straight to the DB — the list endpoint only reads
    it back, so there's no need to spend a real engine run here."""
    run_id = run_id or str(uuid.uuid4())
    await client._app_state.db.execute(
        "INSERT INTO backtests (id, strategy_version_hash, sweep_id, config_json, status, "
        "started_at, finished_at, metrics_json, strategy_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, hash_, sweep_id,
         json.dumps({"start": start, "end": end, "initial_capital": 100_000}),
         status, started_at, started_at + 1000,
         json.dumps({"metrics": {"total_return_pct": total_return_pct,
                                 "max_drawdown_pct": max_drawdown_pct,
                                 "sharpe": sharpe}}), strategy_id))
    return run_id


async def test_list_reports_the_latest_backtest_per_strategy(client):
    slug = await _seed(client, "with history")
    sid = client._app_state.strategy_files.load(slug).meta.id

    await _insert_backtest(client, strategy_id=sid, started_at=1000, total_return_pct=5.0)
    newest = await _insert_backtest(client, strategy_id=sid, started_at=2000,
                                    total_return_pct=-12.5, max_drawdown_pct=-18.25,
                                    sharpe=-0.4, start="2024-01-01", end="2024-12-31")

    row = (await client.get("/api/strategies")).json()["strategies"][0]
    assert row["last_backtest"] == {
        "id": newest, "total_return_pct": -12.5, "max_drawdown_pct": -18.25,
        "sharpe": -0.4, "start": "2024-01-01", "end": "2024-12-31",
        "finished_at": 3000,
    }


async def test_missing_metrics_come_back_as_null_not_an_error(client):
    """sharpe is None for a run with too few return observations
    (metrics.py), so the list must tolerate absent keys."""
    slug = await _seed(client, "thin metrics")
    sid = client._app_state.strategy_files.load(slug).meta.id
    await _insert_backtest(client, strategy_id=sid, started_at=1000,
                           total_return_pct=1.0, sharpe=None)

    row = (await client.get("/api/strategies")).json()["strategies"][0]
    assert row["last_backtest"]["sharpe"] is None
    assert row["last_backtest"]["total_return_pct"] == 1.0


async def test_last_backtest_is_null_when_never_run(client):
    await _seed(client, "never run")
    row = (await client.get("/api/strategies")).json()["strategies"][0]
    assert row["last_backtest"] is None


async def test_sweep_members_and_unfinished_runs_are_ignored(client):
    slug = await _seed(client, "sweep noise")
    sid = client._app_state.strategy_files.load(slug).meta.id

    standalone = await _insert_backtest(client, strategy_id=sid, started_at=1000,
                                        total_return_pct=5.0)
    # both of these are newer, and neither should win
    await _insert_backtest(client, strategy_id=sid, started_at=5000,
                           total_return_pct=99.0, sweep_id="sweep-1")
    await _insert_backtest(client, strategy_id=sid, started_at=6000,
                           total_return_pct=0.0, status="running")

    row = (await client.get("/api/strategies")).json()["strategies"][0]
    assert row["last_backtest"]["id"] == standalone


async def test_backtests_do_not_leak_across_strategies(client):
    a = await _seed(client, "has a run")
    b = await _seed(client, "has no run")
    files = client._app_state.strategy_files
    await _insert_backtest(client, strategy_id=files.load(a).meta.id,
                           started_at=1000, total_return_pct=7.5)

    rows = {s["slug"]: s for s in (await client.get("/api/strategies")).json()["strategies"]}
    assert rows[a]["last_backtest"]["total_return_pct"] == 7.5
    assert rows[b]["last_backtest"] is None
