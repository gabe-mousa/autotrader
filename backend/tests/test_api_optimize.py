"""API-level tests for the optimizer (app/api/optimize.py), wired through the
REAL app lifespan — same pattern as test_api_backtests.py. Candles are
preloaded straight into the DB; a study never backfills, so no Schwab
auth/network is involved at any point.

These tests run REAL studies end to end (small trial counts over synthetic
daily candles), so they cover the part unit tests can't: that the fold slicing
actually feeds the engine data, that trials persist, and that the holdout is
scored exactly once at the end.
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from httpx import ASGITransport, AsyncClient

from app.marketdata.store import Candle
from tests.test_phase0 import make_settings
from tests.test_strategy import EXAMPLE

# Enough daily bars to clear MIN_TOTAL_BARS (1250) with room for folds.
N_BARS = 2000


def _candles(n: int) -> list[Candle]:
    """A trending series with a strong oscillation, so an RSI dip-buyer
    actually trades often enough to clear the trade-count constraints."""
    import math

    import pandas as pd
    idx = pd.bdate_range("2010-01-04", periods=n)
    out = []
    for i, ts in enumerate(idx):
        px = 100 + i * 0.03 + 8 * math.sin(i / 5.0)
        ms = int((ts + pd.Timedelta(hours=21)).tz_localize("UTC").value // 10**6)
        out.append(Candle(ts=ms, open=px, high=px * 1.02, low=px * 0.98, close=px,
                          volume=1_000_000))
    return out


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


async def _seed(client, *, bars: int = N_BARS, symbol: str = "SPY") -> str:
    from app.strategy.files import slugify
    from app.strategy.schema import StrategyDocument

    data = copy.deepcopy(EXAMPLE)
    data["meta"]["name"] = "optimizer test strategy"
    data["universe"] = {"symbols": [symbol]}
    data["timeframe"] = {"bar": "1d", "evaluate_on": "bar_close"}
    doc = StrategyDocument.model_validate(data)
    app_state = client._app_state
    slug = slugify(doc.meta.name)
    app_state.strategy_files.write(slug, doc, freeze=True)
    await app_state.strategy_files.reindex()
    if bars:
        await app_state.marketdata.store.upsert(symbol, "1d", _candles(bars),
                                                source="schwab_rest")
    return slug


def _space() -> list[dict]:
    return [{"path": "indicators.0.params.period", "type": "int", "low": 5, "high": 25},
            {"path": "sizing.value", "type": "float", "low": 5.0, "high": 50.0}]


def _loose_objective() -> dict:
    """Constraints relaxed for the test fixture's short synthetic history —
    the DEFAULTS are strict on purpose (that's the point of the feature) and
    are exercised directly in test_optimize.py."""
    return {"metric": "sharpe", "lambda": 0.5,
            "constraints": {"min_trades_per_fold": 1, "min_trades_total": 3,
                            "max_drawdown_pct": 99, "max_losing_streak": 500}}


async def _await_study(client, study_id: str, timeout: float = 120.0) -> dict:
    """Poll until the background study leaves `running`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        r = await client.get(f"/api/optimize/studies/{study_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] != "running":
            return body
        await asyncio.sleep(0.25)
    raise AssertionError("study did not finish in time")


# ---- planning / validation ------------------------------------------------

async def test_preview_plan_reports_measured_coverage_and_the_holdout(client):
    slug = await _seed(client)
    r = await client.post("/api/optimize/preview-plan",
                          json={"slug": slug, "n_trials": 10, "folds": 3})
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    assert plan["bar"] == "1d"
    assert len(plan["folds"]) == 3
    assert plan["holdout_start"] == plan["search_end"]
    assert plan["holdout_bars"] > 0
    # 2 backtests per fold per trial (test + train), plus the holdout run
    assert r.json()["estimated_backtests"] == 10 * 6 + 1


async def test_a_study_refuses_when_there_is_not_enough_history(client):
    """The user's requirement was to use the data we have — which means being
    explicit when we don't have enough, rather than producing a confident
    number from six weeks of noise."""
    slug = await _seed(client, bars=300)
    r = await client.post("/api/optimize/studies",
                          json={"slug": slug, "space": _space(), "n_trials": 5})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["kind"] == "insufficient_data"
    assert detail["total_bars"] == 300
    assert detail["required_bars"] > 300


async def test_missing_candles_names_the_symbol_to_backfill(client):
    slug = await _seed(client, bars=0)
    r = await client.post("/api/optimize/preview-plan", json={"slug": slug})
    assert r.status_code == 422
    assert "SPY" in r.json()["detail"]["error"]


async def test_an_invalid_search_space_is_rejected_at_creation(client):
    slug = await _seed(client)
    r = await client.post("/api/optimize/studies", json={
        "slug": slug, "space": [{"path": "universe.symbols.0", "type": "categorical",
                                 "choices": ["SPY", "QQQ"]}]})
    assert r.status_code == 400
    assert "universe" in str(r.json()["detail"])


async def test_a_path_that_does_not_exist_fails_fast(client):
    slug = await _seed(client)
    r = await client.post("/api/optimize/studies", json={
        "slug": slug,
        "space": [{"path": "exit.no_such_thing.value", "type": "float", "low": 1, "high": 2}]})
    assert r.status_code == 400
    assert "no such field" in str(r.json()["detail"])


async def test_suggest_space_returns_dimensions_for_this_strategy(client):
    slug = await _seed(client)
    r = await client.post("/api/optimize/suggest-space", json={"slug": slug})
    assert r.status_code == 200
    space = r.json()["space"]
    assert space and all("path" in d and "type" in d for d in space)
    assert all("current" in d for d in space), "the UI shows the current value alongside"


# ---- a real study end to end ----------------------------------------------

async def test_full_study_runs_persists_trials_and_scores_a_holdout(client):
    slug = await _seed(client)
    r = await client.post("/api/optimize/studies", json={
        "slug": slug, "space": _space(), "n_trials": 6, "folds": 2,
        "sampler": "random", "seed": 11, "objective": _loose_objective(),
        "config": {"initial_capital": 100_000}})
    assert r.status_code == 200, r.text
    study_id = r.json()["study_id"]

    body = await _await_study(client, study_id)
    assert body["status"] == "done", body.get("error")

    # every trial was persisted, scored or rejected — none silently dropped
    trials = (await client.get(f"/api/optimize/studies/{study_id}/trials")).json()["trials"]
    assert len(trials) == 6
    assert all(t["status"] in ("done", "rejected", "error") for t in trials)
    assert all(t["params"] for t in trials)

    assert body["counts"]["done"] > 0, "this fixture must produce scored trials"

    champ = body["champion"]
    assert champ is not None and champ["score"] is not None
    assert champ["stability"] is not None, "champion must carry a stability score"
    # the holdout was evaluated, exactly once, on the champion
    assert body["holdout"] is not None
    assert body["holdout"]["metric"] == "sharpe"
    assert body["holdout"]["start"] == body["plan"]["holdout_start"]
    assert body["diagnostics"]["trials_scored"] >= 1
    assert "importance" in body["diagnostics"]


async def test_rejected_trials_are_kept_with_their_reason(client):
    """Rejections are diagnostic output, not garbage: the distribution of
    reasons is how a user learns their search space is badly centered."""
    slug = await _seed(client)
    r = await client.post("/api/optimize/studies", json={
        "slug": slug, "space": _space(), "n_trials": 4, "folds": 2,
        "sampler": "random", "seed": 3,
        # impossible trade minimum -> every trial must be rejected
        "objective": {"metric": "sharpe",
                      "constraints": {"min_trades_per_fold": 100_000,
                                      "min_trades_total": 100_000}}})
    study_id = r.json()["study_id"]
    body = await _await_study(client, study_id)

    trials = (await client.get(f"/api/optimize/studies/{study_id}/trials")).json()["trials"]
    assert len(trials) == 4
    assert all(t["status"] == "rejected" for t in trials)
    assert all(t["reject_reason"] for t in trials)
    assert body["champion"] is None
    assert body["counts"]["rejected"] == 4


async def test_study_list_and_delete(client):
    slug = await _seed(client)
    r = await client.post("/api/optimize/studies", json={
        "slug": slug, "space": _space(), "n_trials": 2, "folds": 2,
        "sampler": "random", "seed": 1, "objective": _loose_objective()})
    study_id = r.json()["study_id"]
    await _await_study(client, study_id)

    listed = (await client.get("/api/optimize/studies")).json()["studies"]
    assert any(s["id"] == study_id for s in listed)

    assert (await client.delete(f"/api/optimize/studies/{study_id}")).status_code == 200
    assert (await client.get(f"/api/optimize/studies/{study_id}")).status_code == 404
    after = (await client.get("/api/optimize/studies")).json()["studies"]
    assert not any(s["id"] == study_id for s in after)


async def test_promote_reruns_the_champion_as_a_normal_backtest(client):
    slug = await _seed(client)
    r = await client.post("/api/optimize/studies", json={
        "slug": slug, "space": _space(), "n_trials": 5, "folds": 2,
        "sampler": "random", "seed": 21, "objective": _loose_objective()})
    study_id = r.json()["study_id"]
    body = await _await_study(client, study_id)
    assert body["counts"]["done"] > 0

    before = client._app_state.strategy_files.load(slug).meta.version
    r = await client.post(f"/api/optimize/studies/{study_id}/promote",
                          json={"save_as_version": True})
    assert r.status_code == 200, r.text
    out = r.json()

    # it becomes an ordinary backtest, visible in the normal Backtests UI
    bt_id = out["backtest"]["id"]
    detail = await client.get(f"/api/backtests/{bt_id}")
    assert detail.status_code == 200
    assert "equity" in detail.json()

    # and a NEW strategy version — never an in-place overwrite
    assert out["saved_version"] == before + 1
    assert client._app_state.strategy_files.load(slug).meta.version == before + 1


async def test_promote_before_a_champion_exists_is_a_conflict(client):
    slug = await _seed(client)
    r = await client.post("/api/optimize/studies", json={
        "slug": slug, "space": _space(), "n_trials": 2, "folds": 2,
        "sampler": "random", "seed": 4,
        "objective": {"metric": "sharpe",
                      "constraints": {"min_trades_per_fold": 100_000}}})
    study_id = r.json()["study_id"]
    await _await_study(client, study_id)
    r = await client.post(f"/api/optimize/studies/{study_id}/promote", json={})
    assert r.status_code == 409


async def test_unknown_study_is_404_everywhere(client):
    for path, method in (("", "get"), ("/trials", "get"), ("/cancel", "post")):
        r = await getattr(client, method)(f"/api/optimize/studies/nope{path}")
        assert r.status_code == 404


async def test_meta_exposes_the_vocabulary_the_ui_needs(client):
    body = (await client.get("/api/optimize/meta")).json()
    assert "sharpe" in body["metrics"]
    assert set(body["samplers"]) == {"random", "tpe"}
    assert body["max_dims"] >= 2


async def test_one_late_listing_symbol_is_named_as_the_limiter(client):
    """Regression (found live 2026-07-26): a 30m strategy refused with "only
    ~373 bars" because ONE symbol in a nine-symbol universe had listed six
    weeks earlier — every other symbol had 2288 bars. The intersection was
    correct; reporting it without naming the cause made a one-edit fix look
    like a data-provider limit."""
    from app.strategy.files import slugify
    from app.strategy.schema import StrategyDocument

    data = copy.deepcopy(EXAMPLE)
    data["meta"]["name"] = "late listing universe"
    data["universe"] = {"symbols": ["SPY", "NEWCO"]}
    data["timeframe"] = {"bar": "1d", "evaluate_on": "bar_close"}
    doc = StrategyDocument.model_validate(data)
    app_state = client._app_state
    slug = slugify(doc.meta.name)
    app_state.strategy_files.write(slug, doc, freeze=True)
    await app_state.strategy_files.reindex()

    deep = _candles(N_BARS)
    await app_state.marketdata.store.upsert("SPY", "1d", deep, source="schwab_rest")
    # NEWCO only exists for the last 120 bars — it caps the whole universe
    await app_state.marketdata.store.upsert("NEWCO", "1d", deep[-120:], source="schwab_rest")

    r = await client.post("/api/optimize/preview-plan", json={"slug": slug})
    assert r.status_code == 422
    detail = r.json()["detail"]
    note = detail["limiting_note"]
    assert "NEWCO" in note, "the refusal must name the symbol that caused it"
    assert "2000" in note or str(N_BARS) in note, "and say how much the rest of the universe has"
    assert detail["total_bars"] < 200


async def test_a_runnable_study_still_warns_about_a_short_history_symbol(client):
    """Even when the study CAN run, the capping symbol is worth surfacing —
    the user is optimizing over less data than they think they have."""
    from app.strategy.files import slugify
    from app.strategy.schema import StrategyDocument

    data = copy.deepcopy(EXAMPLE)
    data["meta"]["name"] = "partially capped universe"
    data["universe"] = {"symbols": ["SPY", "MIDCO"]}
    data["timeframe"] = {"bar": "1d", "evaluate_on": "bar_close"}
    doc = StrategyDocument.model_validate(data)
    app_state = client._app_state
    slug = slugify(doc.meta.name)
    app_state.strategy_files.write(slug, doc, freeze=True)
    await app_state.strategy_files.reindex()

    deep = _candles(4000)
    await app_state.marketdata.store.upsert("SPY", "1d", deep, source="schwab_rest")
    await app_state.marketdata.store.upsert("MIDCO", "1d", deep[-1400:], source="schwab_rest")

    r = await client.post("/api/optimize/preview-plan", json={"slug": slug})
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    assert any("MIDCO" in w for w in plan["warnings"])


async def _finished_study(client, slug: str) -> dict:
    r = await client.post("/api/optimize/studies", json={
        "slug": slug, "space": _space(), "n_trials": 5, "folds": 2,
        "sampler": "random", "seed": 21, "objective": _loose_objective()})
    study_id = r.json()["study_id"]
    body = await _await_study(client, study_id)
    assert body["counts"]["done"] > 0
    return body


async def test_promote_can_save_the_champion_as_a_separate_new_strategy(client):
    """Distinct from save_as_version: the ORIGINAL strategy must be left
    completely untouched — no new version, no edited document — which is what
    you want when the original is already running paper/live."""
    slug = await _seed(client)
    study = await _finished_study(client, slug)
    before = client._app_state.strategy_files.load(slug)

    r = await client.post(f"/api/optimize/studies/{study['study_id']}/promote",
                          json={"save_as_new_strategy": True, "new_name": "Tuned Variant"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["new_slug"] == "tuned-variant"
    assert out["saved_version"] is None

    created = client._app_state.strategy_files.load("tuned-variant")
    assert created.meta.name == "Tuned Variant"
    assert created.meta.version == 1
    # a NEW identity — sharing meta.id would make backtests.strategy_id ambiguous
    assert created.meta.id != before.meta.id
    # the champion's params really landed in the new document
    champ_params = study["champion"]["params"]
    from app.optimize.space import get_by_path
    doc_data = created.model_dump(mode="json", by_alias=True)
    for path, value in champ_params.items():
        assert get_by_path(doc_data, path) == pytest.approx(value) if isinstance(
            value, (int, float)) else get_by_path(doc_data, path) == value

    # and the original is byte-for-byte unchanged
    after = client._app_state.strategy_files.load(slug)
    assert after.meta.version == before.meta.version
    assert after.model_dump() == before.model_dump()


async def test_saving_as_a_new_strategy_refuses_to_clobber_an_existing_name(client):
    slug = await _seed(client)
    study = await _finished_study(client, slug)
    r = await client.post(f"/api/optimize/studies/{study['study_id']}/promote",
                          json={"save_as_new_strategy": True,
                                "new_name": "optimizer test strategy"})
    assert r.status_code == 409
    assert "already exists" in str(r.json()["detail"])


async def test_new_strategy_name_defaults_to_optimized_suffix(client):
    slug = await _seed(client)
    study = await _finished_study(client, slug)
    r = await client.post(f"/api/optimize/studies/{study['study_id']}/promote",
                          json={"save_as_new_strategy": True})
    assert r.status_code == 200, r.text
    assert r.json()["new_slug"] == "optimizer-test-strategy-optimized"


async def test_study_space_carries_resolved_labels(client):
    slug = await _seed(client)
    r = await client.post("/api/optimize/studies", json={
        "slug": slug, "space": _space(), "n_trials": 1, "folds": 2,
        "sampler": "random", "seed": 1, "objective": _loose_objective()})
    study_id = r.json()["study_id"]
    await _await_study(client, study_id)
    body = (await client.get(f"/api/optimize/studies/{study_id}")).json()
    labels = [d["label"] for d in body["space"]]
    assert all(labels), "every dimension must carry a label for the UI"
    assert len(set(labels)) == len(labels), "labels must be unique per study"


async def test_the_champion_is_re_run_across_a_capital_ladder(client):
    """docs/plan/14-optimizer.md §1: capital is not a search axis, it is a
    post-hoc check on the champion. This asserts the check actually runs
    through the real engine, at the real capital levels, over the SEARCH span
    only — the holdout must not be touched a second time."""
    slug = await _seed(client)
    r = await client.post("/api/optimize/studies", json={
        "slug": slug, "space": _space(), "n_trials": 4, "folds": 2,
        "sampler": "random", "seed": 7, "objective": _loose_objective(),
        "config": {"initial_capital": 100_000}})
    study_id = r.json()["study_id"]
    body = await _await_study(client, study_id)
    assert body["status"] == "done", body.get("error")
    assert body["champion"] is not None

    cs = body["diagnostics"]["capital_sensitivity"]
    assert [lv["multiplier"] for lv in cs["levels"]] == [0.25, 0.5, 1.0, 2.0, 4.0]
    assert [lv["capital"] for lv in cs["levels"]] == [25_000, 50_000, 100_000, 200_000, 400_000]
    assert cs["verdict"] in (
        "robust", "extremes-only", "fragile", "broken", "inconclusive", "unknown")
    assert cs["headline"]
    # every rung really ran the engine
    assert all(lv.get("metrics") is not None for lv in cs["levels"]), cs
    # ... and the 1x rung reproduces the study's own configured capital
    one_x = next(lv for lv in cs["levels"] if lv["multiplier"] == 1.0)
    assert one_x["metrics"]["trade_count"] is not None

    # the window is the search span, NOT the holdout: the holdout gets exactly
    # one look and it already had it.
    assert cs["window"] == {"start": body["plan"]["search_start"],
                            "end": body["plan"]["search_end"]}


async def test_the_capital_ladder_is_reproducible_under_the_same_seed(client):
    """Two studies, same seed, same data: the same champion and byte-identical
    capital-sensitivity output. The credibility of the whole optimizer rests on
    being exactly reproducible, and this check adds five more backtests that
    could have broken it."""
    import json

    slug = await _seed(client)

    async def _one() -> dict:
        r = await client.post("/api/optimize/studies", json={
            "slug": slug, "space": _space(), "n_trials": 4, "folds": 2,
            "sampler": "random", "seed": 4242, "objective": _loose_objective(),
            "config": {"initial_capital": 100_000}})
        body = await _await_study(client, r.json()["study_id"])
        assert body["status"] == "done", body.get("error")
        return body

    a, b = await _one(), await _one()
    assert a["champion"]["params"] == b["champion"]["params"]
    ca = a["diagnostics"]["capital_sensitivity"]
    cb = b["diagnostics"]["capital_sensitivity"]
    assert ca["verdict"] == cb["verdict"]
    assert json.dumps(ca, sort_keys=True) == json.dumps(cb, sort_keys=True)
