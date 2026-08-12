"""Strategy optimizer API (docs/plan/14-optimizer.md §10).

Deliberately a separate router from `api/backtests.py`: the sweep endpoints
there are unchanged, and a study is a different object with a different
lifecycle (background, cancellable, holdout-gated).
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, HTTPException, Request

from ..backtest.engine import BacktestConfig
from ..logging import get_logger
from ..optimize.objective import METRICS, Objective
from ..optimize.samplers import SAMPLERS
from ..optimize.space import (
    MAX_DIMS,
    parse_space,
    suggest_space,
    validate_against_document,
    with_labels,
)
from ..optimize.study import StudyRunner
from ..optimize.walkforward import InsufficientDataError, detect_coverage, plan_folds
from ..strategy.files import slugify
from ..strategy.hashing import content_hash
from ..strategy.lint import is_warning, lint
from ..strategy.schema import StrategyDocument

router = APIRouter(prefix="/api/optimize")
log = get_logger("api-optimize")

# A study runs thousands of backtests. This ceiling is about honesty as much
# as runtime: past a few hundred trials over <=8 dimensions the incremental
# trial mostly inflates the maximum rather than finding real structure.
MAX_TRIALS = 2000
DEFAULT_TRIALS = 150


def _load(app, slug: str) -> StrategyDocument:
    if not slug:
        raise HTTPException(status_code=400, detail="slug required")
    try:
        return app.strategy_files.load(slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"strategy {slug!r} not found")


def _base_cfg(body: dict) -> BacktestConfig:
    """The BacktestConfig every trial shares, minus start/end (each fold sets
    its own). Capital/slippage/commission are held FIXED across a study on
    purpose — see docs/plan/14-optimizer.md §1: percent-of-equity sizing is
    near scale-invariant, so sweeping capital burns compute on a flat surface.
    Capital sensitivity is a post-hoc check on the champion instead."""
    raw = dict(body.get("config") or {})
    raw.setdefault("start", "2000-01-01")
    raw.setdefault("end", "2000-01-02")
    try:
        return BacktestConfig.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))


async def _resolve_plan(app, doc: StrategyDocument, body: dict):
    """Measured coverage -> fold plan, or a 4xx naming what was found."""
    try:
        coverage = await detect_coverage(app.db, list(doc.universe.symbols), doc.timeframe.bar)
        folds = body.get("folds")
        return plan_folds(coverage, list(doc.universe.symbols),
                          folds=int(folds) if folds is not None else None,
                          mode=body.get("mode", "rolling"))
    except InsufficientDataError as e:
        raise HTTPException(status_code=422,
                            detail={"error": str(e), "kind": "insufficient_data", **e.detail})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/suggest-space")
async def suggest_space_api(request: Request, body: dict) -> dict:
    """A starting search space derived from the strategy's own parameters, so
    the user edits a table instead of typing dotted JSON paths."""
    doc = _load(request.app.state, body.get("slug", ""))
    return {"space": suggest_space(doc), "max_dims": MAX_DIMS,
            "document": doc.model_dump(mode="json", by_alias=True)}


@router.post("/preview-plan")
async def preview_plan(request: Request, body: dict) -> dict:
    """Resolve the fold plan WITHOUT running anything.

    This endpoint is the main anti-overfitting affordance in the UI: the user
    sees exactly which data will be used, how it is split, and what is being
    held back — before committing to a study — and gets a specific refusal
    when their history is too thin.
    """
    app = request.app.state
    doc = _load(app, body.get("slug", ""))
    plan = await _resolve_plan(app, doc, body)
    n_trials = _n_trials(body)
    return {
        "plan": plan.to_json(),
        "estimated_backtests": n_trials * (2 * len(plan.folds)) + 1,
        "n_trials": n_trials,
    }


def _n_trials(body: dict) -> int:
    try:
        n = int(body.get("n_trials", DEFAULT_TRIALS))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="n_trials must be an integer")
    if not (1 <= n <= MAX_TRIALS):
        raise HTTPException(status_code=422, detail=f"n_trials must be 1..{MAX_TRIALS}")
    return n


@router.post("/studies")
async def create_study(request: Request, body: dict) -> dict:
    app = request.app.state
    slug = body.get("slug", "")
    doc = _load(app, slug)

    # The base strategy must itself be valid — optimizing around a broken
    # document would reject every trial for reasons that have nothing to do
    # with the parameters.
    errors = [p for p in lint(doc) if not is_warning(p)]
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    try:
        dims = parse_space(body.get("space"))
        validate_against_document(dims, doc)
        # resolve labels ONCE, now, against the document this study runs
        # against -- see space.label_for
        dims = with_labels(dims, doc)
        objective = Objective.from_json(body.get("objective"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    sampler = body.get("sampler", "tpe")
    if sampler not in SAMPLERS:
        raise HTTPException(status_code=400,
                            detail=f"sampler must be one of {', '.join(SAMPLERS)}")
    try:
        seed = int(body.get("seed", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="seed must be an integer")

    n_trials = _n_trials(body)
    plan = await _resolve_plan(app, doc, body)
    cfg = _base_cfg(body)

    study_id = str(uuid.uuid4())
    await app.db.execute(
        "INSERT INTO optimization_studies (id, strategy_id, strategy_version_hash, slug, "
        "space_json, objective_json, plan_json, config_json, sampler, seed, n_trials, status, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (study_id, doc.meta.id, content_hash(doc), slug,
         json.dumps([d.to_json() for d in dims]), json.dumps(objective.to_json()),
         json.dumps(plan.to_json()), json.dumps(cfg.model_dump(mode="json")),
         sampler, seed, n_trials, "running", int(time.time() * 1000)))

    runner = StudyRunner(app, study_id=study_id, doc=doc, slug=slug, dims=dims,
                         objective=objective, plan=plan, base_cfg=cfg,
                         sampler_name=sampler, seed=seed, n_trials=n_trials)
    app.optimizer.start(runner)
    log.info("study_started", study_id=study_id, slug=slug, trials=n_trials,
             folds=len(plan.folds), sampler=sampler)
    return {"study_id": study_id, "plan": plan.to_json(), "n_trials": n_trials}


@router.get("/studies")
async def list_studies(request: Request, limit: int = 50) -> dict:
    app = request.app.state
    rows = await app.db.fetch_all(
        "SELECT id, slug, strategy_id, sampler, seed, n_trials, status, created_at, "
        "finished_at, objective_json, plan_json, holdout_json, diagnostics_json, error "
        "FROM optimization_studies ORDER BY created_at DESC LIMIT ?",
        (max(1, min(limit, 200)),))
    out = []
    for r in rows:
        d = dict(r)
        plan = json.loads(d.pop("plan_json"))
        d["objective"] = json.loads(d.pop("objective_json"))
        hold = d.pop("holdout_json", None)
        d["holdout"] = json.loads(hold) if hold else None
        diag = d.pop("diagnostics_json", None)
        d["diagnostics"] = json.loads(diag) if diag else None
        d["bar"] = plan.get("bar")
        d["folds"] = len(plan.get("folds", []))
        runner = app.optimizer.get(d["id"])
        d["progress"] = runner.progress.to_json() if runner else None
        out.append(d)
    return {"studies": out}


async def _study_row(app, study_id: str) -> dict:
    row = await app.db.fetch_one(
        "SELECT * FROM optimization_studies WHERE id=?", (study_id,))
    if not row:
        raise HTTPException(status_code=404, detail="study not found")
    return dict(row)


@router.get("/studies/{study_id}")
async def get_study(request: Request, study_id: str) -> dict:
    app = request.app.state
    r = await _study_row(app, study_id)
    runner = app.optimizer.get(study_id)
    champion = None
    if r.get("champion_trial_id"):
        crow = await app.db.fetch_one(
            "SELECT * FROM optimization_trials WHERE id=?", (r["champion_trial_id"],))
        champion = _trial_json(crow) if crow else None
    counts = await app.db.fetch_one(
        "SELECT COUNT(*) AS n, SUM(status='done') AS done, SUM(status='rejected') AS rejected, "
        "SUM(status='error') AS errored FROM optimization_trials WHERE study_id=?", (study_id,))
    return {
        "study_id": study_id, "slug": r["slug"], "strategy_id": r["strategy_id"],
        "status": r["status"], "sampler": r["sampler"], "seed": r["seed"],
        "n_trials": r["n_trials"], "created_at": r["created_at"],
        "finished_at": r["finished_at"], "error": r["error"],
        "space": json.loads(r["space_json"]),
        "objective": json.loads(r["objective_json"]),
        "plan": json.loads(r["plan_json"]),
        "config": json.loads(r["config_json"]),
        "holdout": json.loads(r["holdout_json"]) if r["holdout_json"] else None,
        "diagnostics": json.loads(r["diagnostics_json"]) if r["diagnostics_json"] else None,
        "champion": champion,
        "progress": runner.progress.to_json() if runner else None,
        "counts": {k: int(counts[k] or 0) for k in ("n", "done", "rejected", "errored")},
    }


def _trial_json(row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json"))
    for key, out in (("folds_json", "folds"), ("metrics_json", "metrics")):
        raw = d.pop(key, None)
        d[out] = json.loads(raw) if raw else None
    return d


@router.get("/studies/{study_id}/trials")
async def list_trials(request: Request, study_id: str, limit: int = 500,
                      status: str | None = None) -> dict:
    """All trials, INCLUDING rejected ones.

    Rejected trials are shown on purpose: the distribution of rejection
    reasons is the fastest way to see that a search space is centered
    somewhere the strategy barely trades.
    """
    app = request.app.state
    await _study_row(app, study_id)
    sql = "SELECT * FROM optimization_trials WHERE study_id=?"
    args: list = [study_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY trial_number LIMIT ?"
    args.append(max(1, min(limit, 5000)))
    rows = await app.db.fetch_all(sql, tuple(args))
    return {"trials": [_trial_json(r) for r in rows]}


@router.post("/studies/{study_id}/cancel")
async def cancel_study(request: Request, study_id: str) -> dict:
    app = request.app.state
    await _study_row(app, study_id)
    if not app.optimizer.cancel(study_id):
        raise HTTPException(status_code=409, detail="study is not running in this process")
    return {"ok": True, "study_id": study_id}


@router.post("/studies/{study_id}/promote")
async def promote_champion(request: Request, study_id: str, body: dict | None = None) -> dict:
    """Materialize the champion: run it as a NORMAL persisted backtest, and
    optionally save it — either as a new VERSION of the same strategy
    (`save_as_version`) or as a separate NEW strategy (`save_as_new_strategy`,
    with an optional `new_name`).

    Both save modes are non-destructive. A new version keeps the optimizer's
    result on the same strategy's timeline, where it can be diffed against
    what you had; a new strategy leaves the original completely untouched,
    which is what you want when the tuned parameters are different enough to
    be a separate idea (or when the original is already running paper/live and
    you don't want its version history moving underneath it).

    Never touches live/paper runs: the promotion gate in
    docs/plan/07-paper-trading.md remains the only path to real money.
    """
    from ..api.backtests import _fetch_candles_with_warmup, _run_and_persist_backtest
    from ..optimize.space import apply_params

    app = request.app.state
    body = body or {}
    r = await _study_row(app, study_id)
    if not r.get("champion_trial_id"):
        raise HTTPException(status_code=409,
                            detail="this study has no champion yet (still running, or no trial "
                                   "passed the constraints)")
    crow = await app.db.fetch_one(
        "SELECT * FROM optimization_trials WHERE id=?", (r["champion_trial_id"],))
    if not crow:
        raise HTTPException(status_code=404, detail="champion trial row is missing")

    doc = _load(app, r["slug"])
    base = doc.model_dump(mode="json", by_alias=True)
    params = json.loads(crow["params_json"])
    try:
        candidate = StrategyDocument.model_validate(apply_params(base, params))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=409,
                            detail=f"champion params no longer fit this strategy (it was edited "
                                   f"after the study ran): {e}")

    plan = json.loads(r["plan_json"])
    cfg = BacktestConfig.model_validate({
        **json.loads(r["config_json"]),
        # Backtest the champion over the study's FULL measured span, holdout
        # included — this run is for the user to inspect, not to select on.
        "start": _d(plan["coverage_start"]), "end": _d(plan["coverage_end"]),
    })
    candles = await _fetch_candles_with_warmup(app, candidate, cfg)
    result = await _run_and_persist_backtest(app, candidate, cfg, candles, r["slug"])

    provenance = (
        f"[optimizer] champion of study {study_id[:8]} "
        f"({', '.join(f'{k}={v}' for k, v in params.items())})")

    saved_version = None
    new_slug = None
    if body.get("save_as_new_strategy"):
        # A separate strategy: new slug, new meta.id, version reset to 1. The
        # meta.id MUST be regenerated -- it's the identity the runtime and the
        # backtests table key off, and two strategies sharing one would make
        # `strategy_id` ambiguous exactly the way the sweep/backtest code
        # already documents.
        name = str(body.get("new_name") or "").strip() or f"{doc.meta.name} (optimized)"
        candidate.meta.name = name[:120]
        candidate.meta.id = str(uuid.uuid4())
        candidate.meta.version = 1
        candidate.meta.description = f"{doc.meta.description}\n\n{provenance}".strip()
        new_slug = slugify(candidate.meta.name)
        if (app.strategy_files.root / new_slug / "strategy.yaml").exists():
            raise HTTPException(
                status_code=409,
                detail=f"a strategy named {name!r} already exists ({new_slug}) — pick a different "
                       "name so the original isn't overwritten")
        app.strategy_files.write(new_slug, candidate, freeze=True)
        await app.strategy_files.reindex()
        await app.db.audit("user", "strategy",
                           {"event": "created", "slug": new_slug, "from_study": study_id})
    elif body.get("save_as_version"):
        candidate.meta.description = f"{doc.meta.description}\n\n{provenance}".strip()
        # freeze=True so this lands as a NEW version snapshot in versions/,
        # leaving the user a diffable trail rather than an in-place edit.
        app.strategy_files.write(r["slug"], candidate, freeze=True)
        saved_version = app.strategy_files.load(r["slug"]).meta.version

    return {"backtest": result, "params": params, "saved_version": saved_version,
            "new_slug": new_slug}


@router.delete("/studies/{study_id}")
async def delete_study(request: Request, study_id: str) -> dict:
    app = request.app.state
    await _study_row(app, study_id)
    app.optimizer.cancel(study_id)
    await app.db.execute("DELETE FROM optimization_trials WHERE study_id=?", (study_id,))
    await app.db.execute("DELETE FROM optimization_studies WHERE id=?", (study_id,))
    return {"ok": True}


@router.get("/meta")
async def meta() -> dict:
    """Static vocabulary for the Optimize page's dropdowns."""
    return {"metrics": list(METRICS), "samplers": list(SAMPLERS),
            "max_dims": MAX_DIMS, "max_trials": MAX_TRIALS,
            "default_trials": DEFAULT_TRIALS}


def _d(ms: int) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.UTC).strftime("%Y-%m-%d")
