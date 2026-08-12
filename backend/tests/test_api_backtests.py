"""API-level tests for the backtest run/list/detail/compare/sweep endpoints
(app/api/backtests.py), wired through the REAL app lifespan (same pattern as
test_api_runs.py). Candles are preloaded directly into the DB so no Schwab
auth/network is needed — MarketDataService.get_candles only backfills on a
coverage miss (see app/marketdata/service.py)."""

from __future__ import annotations

import copy

import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from app.marketdata.store import Candle
from tests.test_phase0 import make_settings
from tests.test_strategy import EXAMPLE

N_DAYS = 140
WARMUP_END = 100  # index into the bdate range where the evaluated window starts


def daily_candles(closes: list[float], start: str = "2023-09-01") -> list[Candle]:
    idx = pd.bdate_range(start, periods=len(closes))
    out = []
    for ts, px in zip(idx, closes):
        ms = int((ts + pd.Timedelta(hours=21)).tz_localize("UTC").value // 10**6)
        out.append(Candle(ts=ms, open=px, high=px * 1.01, low=px * 0.99, close=px,
                           volume=1_000_000))
    return out


def oscillating_closes(n: int) -> list[float]:
    import math
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


async def _seed_strategy(client, slug_name="rsi dip buyer api test", **overrides) -> str:
    from app.strategy.schema import StrategyDocument

    data = copy.deepcopy(EXAMPLE)
    data["meta"]["name"] = slug_name
    data.update(overrides)
    doc = StrategyDocument.model_validate(data)
    app_state = client._app_state
    from app.strategy.files import slugify
    slug = slugify(doc.meta.name)
    app_state.strategy_files.write(slug, doc, freeze=True)
    await app_state.strategy_files.reindex()
    return slug


async def _seed_candles(client, symbol="SPY", closes=None) -> tuple[str, str]:
    closes = closes if closes is not None else oscillating_closes(N_DAYS)
    candles = daily_candles(closes)
    app_state = client._app_state
    await app_state.marketdata.store.upsert(symbol, "1d", candles, source="schwab_rest")
    idx = pd.bdate_range("2023-09-01", periods=N_DAYS)
    start = idx[WARMUP_END].strftime("%Y-%m-%d")
    end = idx[-1].strftime("%Y-%m-%d")
    return start, end


def _config(start: str, end: str) -> dict:
    return {
        "start": start, "end": end, "initial_capital": 100_000,
        "slippage": {"type": "fixed_bps", "value": 0},
        "commission": {"per_order": 0},
        "fill_on_touch": False,
    }


async def test_run_backtest_and_detail_has_symbol_and_bar(client):
    slug = await _seed_strategy(client)
    start, end = await _seed_candles(client)

    r = await client.post("/api/backtests", json={"slug": slug, "config": _config(start, end)})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]

    r = await client.get(f"/api/backtests/{run_id}")
    assert r.status_code == 200
    body = r.json()
    # feature 1: additive fields, everything else unchanged
    assert body["symbol"] == "SPY"
    assert body["bar"] == "1d"
    assert body["config"]["start"] == start
    assert "equity" in body and "trades" in body


async def test_detail_exposes_full_universe_and_strategy_link(client):
    """The price-chart symbol picker needs every universe symbol (not just the
    first), and the header links back to the strategy editor by slug."""
    slug = await _seed_strategy(client, slug_name="multi symbol universe test",
                                universe={"symbols": ["spy", "qqq"]})
    start, end = await _seed_candles(client, symbol="SPY")
    await _seed_candles(client, symbol="QQQ")

    r = await client.post("/api/backtests", json={"slug": slug, "config": _config(start, end)})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]

    body = (await client.get(f"/api/backtests/{run_id}")).json()
    assert body["symbols"] == ["SPY", "QQQ"]
    assert body["symbol"] == "SPY"  # unchanged for existing callers
    assert body["strategy_slug"] == slug
    assert body["strategy_name"] == "multi symbol universe test"


async def test_copied_strategy_is_attributed_to_the_one_that_ran(client):
    """content_hash EXCLUDES meta, so a copy/rename is hash-identical to its
    original. Before backtests.strategy_id, the hash -> name lookup picked an
    arbitrary one and a run of "…v1" could list itself as "…v3"."""
    original = await _seed_strategy(client, slug_name="ema trend original")
    copy_slug = await _seed_strategy(client, slug_name="ema trend renamed copy")
    start, end = await _seed_candles(client)

    app_state = client._app_state
    docs = {s: app_state.strategy_files.load(s) for s in (original, copy_slug)}
    from app.strategy.hashing import content_hash
    assert content_hash(docs[original]) == content_hash(docs[copy_slug]), \
        "precondition: the copy must collide on hash for this test to mean anything"

    r = await client.post("/api/backtests",
                          json={"slug": original, "config": _config(start, end)})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]

    listed = {b["id"]: b for b in (await client.get("/api/backtests")).json()["backtests"]}
    assert listed[run_id]["strategy_name"] == "ema trend original"
    assert listed[run_id]["strategy_slug"] == original

    detail = (await client.get(f"/api/backtests/{run_id}")).json()
    assert detail["strategy_slug"] == original

    # ...and the previously-ignored ?slug= filter now actually filters.
    filtered = (await client.get(f"/api/backtests?slug={original}")).json()["backtests"]
    assert [b["id"] for b in filtered] == [run_id]
    assert (await client.get(f"/api/backtests?slug={copy_slug}")).json()["backtests"] == []


async def test_compare_endpoint_returns_multiple_runs(client):
    slug = await _seed_strategy(client, slug_name="rsi dip buyer compare test",
                                 universe={"symbols": ["aaa"]})
    start, end = await _seed_candles(client, symbol="AAA")
    slug2 = await _seed_strategy(client, slug_name="rsi dip buyer compare test 2",
                                  universe={"symbols": ["aaa"]})

    r1 = await client.post("/api/backtests", json={"slug": slug, "config": _config(start, end)})
    assert r1.status_code == 200, r1.text
    r2 = await client.post("/api/backtests", json={"slug": slug2, "config": _config(start, end)})
    assert r2.status_code == 200, r2.text
    id1, id2 = r1.json()["id"], r2.json()["id"]

    r = await client.get(f"/api/backtests/compare?ids={id1},{id2}")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {b["id"] for b in body["backtests"]}
    assert ids == {id1, id2}
    for b in body["backtests"]:
        assert "metrics" in b and "equity" in b and "config" in b


async def test_compare_caps_at_four_and_404s_unknown(client):
    r = await client.get("/api/backtests/compare?ids=a,b,c,d,e")
    assert r.status_code == 400

    r = await client.get("/api/backtests/compare?ids=does-not-exist")
    assert r.status_code == 404


# ---- what-if (temporarily excluding trades) ---------------------------------

async def _run_for_what_if(client) -> tuple[str, dict]:
    slug = await _seed_strategy(client, slug_name="what if outlier test")
    start, end = await _seed_candles(client)
    r = await client.post("/api/backtests", json={"slug": slug, "config": _config(start, end)})
    assert r.status_code == 200, r.text
    run_id = r.json()["id"]
    detail = (await client.get(f"/api/backtests/{run_id}")).json()
    assert len(detail["trades"]) >= 2, "need multiple trades to exercise exclusion"
    return run_id, detail


async def test_what_if_excluding_nothing_reproduces_the_stored_metrics(client):
    """The load-bearing check: the recompute path must agree with the engine's
    own numbers when nothing is excluded. If this drifts, every adjusted stat
    the UI shows is quietly wrong."""
    run_id, detail = await _run_for_what_if(client)
    r = await client.post(f"/api/backtests/{run_id}/what-if", json={"exclude_trade_ids": []})
    assert r.status_code == 200, r.text
    got, stored = r.json()["metrics"], detail["metrics"]

    # Trade-derived stats are recomputed from the same rows, so they must match
    # exactly — any drift here means the recompute lost or miscounted trades.
    for key in ("trade_count", "win_rate_pct", "profit_factor", "expectancy",
                "avg_win", "avg_loss", "avg_bars_held", "longest_losing_streak",
                "exposure_pct"):
        assert got.get(key) == stored.get(key), key

    # Equity-derived stats must match too: _equity_rows persists equity
    # UNROUNDED precisely so a recompute reads back exactly what the engine
    # measured. Sortino is the canary — it's hypersensitive to sub-microdollar
    # equity differences (see the _equity_rows comment), so if rounding is ever
    # reintroduced there, this assertion is what catches it.
    for key in ("total_return_pct", "final_equity", "max_drawdown_pct", "sharpe",
                "cagr_pct", "sortino", "calmar", "volatility_pct"):
        assert got.get(key) == pytest.approx(stored.get(key), rel=1e-9, abs=1e-9), key


async def test_what_if_excluding_a_trade_adjusts_stats_and_persists_nothing(client):
    run_id, detail = await _run_for_what_if(client)
    trades = detail["trades"]
    best = max(trades, key=lambda t: t["pnl"])

    r = await client.post(f"/api/backtests/{run_id}/what-if",
                          json={"exclude_trade_ids": [best["id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approximate"] is True
    assert body["excluded_trade_ids"] == [best["id"]]
    assert body["excluded_pnl"] == pytest.approx(best["pnl"])
    assert body["metrics"]["trade_count"] == len(trades) - 1
    assert all(t["id"] != best["id"] for t in body["trades"])
    # dropping the best trade must reduce the total return
    assert body["metrics"]["total_return_pct"] < detail["metrics"]["total_return_pct"]
    # equity is adjusted, not just the trade list
    assert body["metrics"]["final_equity"] == pytest.approx(
        detail["metrics"]["final_equity"] - best["pnl"], rel=1e-6)
    assert body["equity"], "adjusted equity curve should be returned for the chart"

    # nothing was written: the stored run is untouched
    after = (await client.get(f"/api/backtests/{run_id}")).json()
    assert after["metrics"] == detail["metrics"]
    assert len(after["trades"]) == len(trades)


async def test_what_if_rejects_unknown_ids_all_trades_and_bad_input(client):
    run_id, detail = await _run_for_what_if(client)
    ids = [t["id"] for t in detail["trades"]]

    r = await client.post(f"/api/backtests/{run_id}/what-if",
                          json={"exclude_trade_ids": [999_999]})
    assert r.status_code == 404

    # excluding everything would leave no strategy to measure
    r = await client.post(f"/api/backtests/{run_id}/what-if", json={"exclude_trade_ids": ids})
    assert r.status_code == 400

    r = await client.post(f"/api/backtests/{run_id}/what-if",
                          json={"exclude_trade_ids": "not-a-list"})
    assert r.status_code == 400

    r = await client.post("/api/backtests/does-not-exist/what-if",
                          json={"exclude_trade_ids": []})
    assert r.status_code == 404
