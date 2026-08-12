"""Parameter sweep API tests (POST/GET /api/backtests/sweep) — real 1-lever
sweep over 3 RSI periods through the real app lifespan + preloaded candles
(same fixture pattern as test_api_backtests.py), confirming 3 backtests land
under one sweep_id with differing metrics, and that an intentionally-invalid
combo (RSI period outside the schema's allowed range) is skipped and
reported rather than crashing the whole sweep."""

from __future__ import annotations

import copy
import math

import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from app.marketdata.store import Candle
from tests.test_phase0 import make_settings
from tests.test_strategy import EXAMPLE

N_DAYS = 140
WARMUP_END = 100


def daily_candles(closes: list[float], start: str = "2023-09-01") -> list[Candle]:
    idx = pd.bdate_range(start, periods=len(closes))
    out = []
    for ts, px in zip(idx, closes):
        ms = int((ts + pd.Timedelta(hours=21)).tz_localize("UTC").value // 10**6)
        out.append(Candle(ts=ms, open=px, high=px * 1.01, low=px * 0.99, close=px,
                           volume=1_000_000))
    return out


def oscillating_closes(n: int) -> list[float]:
    return [100 + 10 * math.sin(i / 6) for i in range(n)]


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


async def _seed(client) -> tuple[str, str, str]:
    from app.strategy.files import slugify
    from app.strategy.schema import StrategyDocument

    data = copy.deepcopy(EXAMPLE)
    data["meta"]["name"] = "rsi sweep test"
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 14}}]
    doc = StrategyDocument.model_validate(data)
    slug = slugify(doc.meta.name)
    app_state = client._app_state
    app_state.strategy_files.write(slug, doc, freeze=True)
    await app_state.strategy_files.reindex()

    candles = daily_candles(oscillating_closes(N_DAYS))
    await app_state.marketdata.store.upsert("SPY", "1d", candles, source="schwab_rest")
    idx = pd.bdate_range("2023-09-01", periods=N_DAYS)
    start = idx[WARMUP_END].strftime("%Y-%m-%d")
    end = idx[-1].strftime("%Y-%m-%d")
    return slug, start, end


def _config(start: str, end: str) -> dict:
    return {
        "start": start, "end": end, "initial_capital": 100_000,
        "slippage": {"type": "fixed_bps", "value": 0},
        "commission": {"per_order": 0},
        "fill_on_touch": False,
    }


async def test_sweep_runs_valid_combos_and_skips_invalid(client):
    slug, start, end = await _seed(client)
    grid = {
        "rsi_period": {
            "path": "indicators.0.params.period",
            # 500 is outside RSI's registered param range -> fails lint,
            # must be skipped rather than crashing the sweep.
            "values": [7, 14, 21, 500],
        }
    }
    r = await client.post("/api/backtests/sweep",
                           json={"slug": slug, "config": _config(start, end), "grid": grid})
    assert r.status_code == 200, r.text
    body = r.json()
    sweep_id = body["sweep_id"]
    assert len(body["results"]) == 3
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["combo"] == {"rsi_period": 500}
    assert "reason" in body["skipped"][0]

    periods = {r["combo"]["rsi_period"] for r in body["results"]}
    assert periods == {7, 14, 21}
    backtest_ids = {r["backtest_id"] for r in body["results"]}
    assert len(backtest_ids) == 3

    # metrics differ sensibly across periods (not all identical runs)
    metrics_summaries = [tuple(sorted(r["metrics_summary"].items())) for r in body["results"]]
    assert len(set(metrics_summaries)) > 1

    # GET /backtests/sweep/{sweep_id} reflects the same 3 runs
    r2 = await client.get(f"/api/backtests/sweep/{sweep_id}")
    assert r2.status_code == 200
    body2 = r2.json()
    assert {r["backtest_id"] for r in body2["results"]} == backtest_ids
    assert body2["grid"] == grid

    # each backtest is independently reachable via the normal detail endpoint.
    # NOTE: sweep combos are never frozen as their own strategy_versions
    # snapshot (only the base strategy is), so symbol/bar (feature 1, looked
    # up via strategy_version_hash -> strategy_versions.file_path) come back
    # None here — a known, documented limitation of the sweep feature.
    for bt_id in backtest_ids:
        r3 = await client.get(f"/api/backtests/{bt_id}")
        assert r3.status_code == 200
        assert r3.json()["metrics"]


async def test_sweep_detail_identifies_its_strategy(client):
    """The sweep detail page links to the strategy's editor and renders its
    document, so the endpoint has to say which strategy the sweep varied."""
    slug, start, end = await _seed(client)
    grid = {"rsi_period": {"path": "indicators.0.params.period", "values": [7, 14]}}
    r = await client.post("/api/backtests/sweep",
                          json={"slug": slug, "config": _config(start, end), "grid": grid})
    sweep_id = r.json()["sweep_id"]

    body = (await client.get(f"/api/backtests/sweep/{sweep_id}")).json()
    assert body["strategy_slug"] == slug
    assert body["strategy_name"] == "rsi sweep test"
    assert body["grid"] == grid


async def test_list_exposes_sweep_grouping(client):
    """The runs table collapses a sweep to one expandable row, which needs
    each run's sweep_id and its combo."""
    slug, start, end = await _seed(client)
    grid = {"rsi_period": {"path": "indicators.0.params.period", "values": [7, 14]}}
    r = await client.post("/api/backtests/sweep",
                          json={"slug": slug, "config": _config(start, end), "grid": grid})
    sweep_id = r.json()["sweep_id"]
    # plus a standalone run, which must stay ungrouped
    await client.post("/api/backtests", json={"slug": slug, "config": _config(start, end)})

    listed = (await client.get("/api/backtests")).json()["backtests"]
    members = [b for b in listed if b["sweep_id"] == sweep_id]
    assert len(members) == 2
    assert sorted(m["sweep_combo"]["rsi_period"] for m in members) == [7, 14]
    standalone = [b for b in listed if b["sweep_id"] is None]
    assert len(standalone) == 1
    assert standalone[0]["sweep_combo"] is None


async def test_sweep_rejects_too_many_levers(client):
    slug, start, end = await _seed(client)
    grid = {
        "a": {"path": "indicators.0.params.period", "values": [7, 14]},
        "b": {"path": "exit.stop_loss.value", "values": [1, 2]},
        "c": {"path": "exit.take_profit.value", "values": [3, 4]},
    }
    r = await client.post("/api/backtests/sweep",
                           json={"slug": slug, "config": _config(start, end), "grid": grid})
    assert r.status_code == 400


async def test_sweep_rejects_universe_levers(client):
    """Regression: every combo in a sweep must share one universe -- a combo
    asking for a symbol the fetch didn't cover would silently produce 0
    trades rather than an error (see sweep.py's _FORBIDDEN_PATH_PREFIXES).
    timeframe.bar IS sweepable; only non-`bar` timeframe paths are not."""
    slug, start, end = await _seed(client)
    for bad_path in ("universe.symbols.0", "timeframe.evaluate_on"):
        grid = {"a": {"path": bad_path, "values": [1, 2]}}
        r = await client.post("/api/backtests/sweep",
                              json={"slug": slug, "config": _config(start, end), "grid": grid})
        assert r.status_code == 400
        assert bad_path in r.json()["detail"]


async def test_sweep_over_timeframe_bar(client):
    """Bar size is a lever: each distinct bar gets its own candle fetch (with
    its own warm-up), and a bar with no data is skipped rather than failing
    the whole sweep."""
    slug, start, end = await _seed(client)  # seeds 1d candles only
    app_state = client._app_state
    # 5m is derived from stored 1m bars (see marketdata/store.py's AGG_RULES),
    # so seed 1m history over the same window — one hour per day is plenty of
    # bars to warm up and evaluate on, and gaps between days are fine.
    # a few days past the window's end too, so the store's coverage (which is
    # just first/last, see marketdata/service.py) reaches past `end` and no
    # backfill — which would need Schwab auth — is attempted.
    minutes = []
    for c in daily_candles(oscillating_closes(N_DAYS + 3)):
        for k in range(60):
            minutes.append(Candle(ts=c.ts + k * 60_000, open=c.open, high=c.high,
                                  low=c.low, close=c.close, volume=c.volume))
    await app_state.marketdata.store.upsert("SPY", "1m", minutes, source="schwab_rest")

    grid = {"bar": {"path": "timeframe.bar", "values": ["1d", "5m"]}}
    r = await client.post("/api/backtests/sweep",
                          json={"slug": slug, "config": _config(start, end), "grid": grid})
    assert r.status_code == 200, r.text
    body = r.json()
    ran = {res["combo"]["bar"] for res in body["results"]}
    assert ran == {"1d", "5m"}, body["skipped"]
    # results keep combo order regardless of the per-bar fetch grouping
    assert [res["combo"]["bar"] for res in body["results"]] == ["1d", "5m"]
    # each ran against its own bar size
    for res in body["results"]:
        detail = (await client.get(f"/api/backtests/{res['backtest_id']}")).json()
        assert detail["metrics"]


async def test_sweep_skips_bar_with_no_candles(client):
    """A bar size with no data must not take the rest of the sweep down."""
    slug, start, end = await _seed(client)  # 1d candles only; no 5m history
    grid = {"bar": {"path": "timeframe.bar", "values": ["1d", "5m"]}}
    r = await client.post("/api/backtests/sweep",
                          json={"slug": slug, "config": _config(start, end), "grid": grid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [res["combo"]["bar"] for res in body["results"]] == ["1d"]
    assert [s["combo"]["bar"] for s in body["skipped"]] == ["5m"]


async def test_sweep_unknown_slug_404s(client):
    r = await client.post("/api/backtests/sweep", json={
        "slug": "does-not-exist",
        "config": _config("2024-01-01", "2024-02-01"),
        "grid": {"a": {"path": "indicators.0.params.period", "values": [7, 14]}},
    })
    assert r.status_code == 404


async def test_get_unknown_sweep_404s(client):
    r = await client.get("/api/backtests/sweep/not-a-real-sweep")
    assert r.status_code == 404
