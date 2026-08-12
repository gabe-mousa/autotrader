"""Run-control API (docs/plan/07-paper-trading.md, 08-runtime-concurrency.md)
— starts/pauses/resumes/stops/flattens paper runs through `RunManager`, and
exposes the kill switch + global risk-limit overrides. No app-level auth
(server binds 127.0.0.1 only, same as every other route)."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from ..auth.manager import NotAuthenticatedError
from ..logging import get_logger
from ..runtime.recovery import recover_run_now
from ..schwab.client import SchwabAPIError
from ..strategy.live_support import gap_payloads

router = APIRouter(prefix="/api")
log = get_logger("api-runs")


@router.post("/runs")
async def start_run(request: Request, body: dict) -> dict:
    app = request.app.state
    slug = body.get("slug")
    if not slug:
        raise HTTPException(status_code=400, detail="slug required")
    starting_cash = float(body.get("starting_cash", 100_000.0))
    try:
        run_id = await app.run_manager.start_paper(slug, starting_cash=starting_cash)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"strategy {slug!r} not found")
    except ValueError as e:
        # promotion-gate rejection (no backtest yet) or not-indexed — both
        # are client-fixable ("go backtest it first"/"go save it first")
        raise HTTPException(status_code=400, detail=str(e))
    return {"run_id": run_id}


# ---- promotion to live (Phase 6 slice 2, docs/plan/07-paper-trading.md) ----

@router.get("/runs/live/accounts")
async def live_accounts(request: Request) -> dict:
    """Connected Schwab accounts, for picking an account_hash to promote a
    strategy against. Same shape as live_probe.py's /status account list,
    but independent of the unrelated live_probe_enabled flag."""
    app = request.app.state
    try:
        hashes = await app.schwab.get_account_numbers()
        accounts = await app.schwab.get_accounts()
    except NotAuthenticatedError:
        raise HTTPException(status_code=424, detail="Not connected to Schwab")
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    by_number = {str(a.get("securitiesAccount", {}).get("accountNumber")): a for a in accounts}
    out = []
    for h in hashes:
        number = h.get("accountNumber", "")
        acct = by_number.get(number, {}).get("securitiesAccount", {})
        out.append({"hash": h.get("hashValue"),
                   "account_number_masked": f"••••{number[-4:]}" if number else "unknown",
                   "type": acct.get("type", "?")})
    return {"accounts": out}


@router.get("/runs/live/eligibility")
async def live_eligibility(request: Request, slug: str) -> dict:
    """Read-only "would this pass the promotion gate?" check — the wizard's
    first step calls this before the user commits to anything. Distinct from
    start_live's own gate check (which raises); this never creates a run."""
    app = request.app.state
    try:
        status = await app.run_manager.promotion_status(slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"strategy {slug!r} not found")
    if status["backtest_id"] is not None:
        row = await app.db.fetch_one(
            "SELECT metrics_json FROM backtests WHERE id=?", (status["backtest_id"],))
        payload = json.loads(row["metrics_json"]) if row and row["metrics_json"] else {}
        status["backtest_metrics"] = payload.get("metrics", payload)
    else:
        status["backtest_metrics"] = None
    return status


@router.post("/runs/live")
async def start_live_run(request: Request, body: dict) -> dict:
    app = request.app.state
    slug = body.get("slug")
    account_hash = body.get("account_hash")
    if not slug:
        raise HTTPException(status_code=400, detail="slug required")
    if not account_hash:
        raise HTTPException(status_code=400, detail="account_hash required")
    try:
        run_id = await app.run_manager.start_live(
            slug, account_hash, override_gate=bool(body.get("override_gate", False)))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"strategy {slug!r} not found")
    except ValueError as e:
        # promotion-gate rejection, kill switch active, unknown account_hash,
        # or an already-active live run for this strategy — all client-fixable
        raise HTTPException(status_code=400, detail=str(e))
    return {"run_id": run_id}


@router.post("/runs/{run_id}/activate")
async def activate_live_run(request: Request, run_id: str) -> dict:
    try:
        await request.app.state.run_manager.activate_live(run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"run_id": run_id, "event": "activated"}


async def _control(request: Request, run_id: str, method_name: str) -> dict:
    app = request.app.state
    method = getattr(app.run_manager, method_name)
    try:
        result = await method(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no active run {run_id!r}")
    out = {"run_id": run_id, "event": method_name}
    if isinstance(result, dict):
        out.update(result)
    return out


@router.post("/runs/{run_id}/pause")
async def pause_run(request: Request, run_id: str) -> dict:
    return await _control(request, run_id, "pause")


@router.post("/runs/{run_id}/resume")
async def resume_run(request: Request, run_id: str) -> dict:
    return await _control(request, run_id, "resume")


@router.post("/runs/{run_id}/flatten")
async def flatten_run(request: Request, run_id: str) -> dict:
    return await _control(request, run_id, "flatten")


@router.post("/runs/{run_id}/stop")
async def stop_run(request: Request, run_id: str) -> dict:
    return await _control(request, run_id, "stop")


@router.post("/runs/{run_id}/recover")
async def recover_run(request: Request, run_id: str) -> dict:
    """Re-adopt a run that has no in-memory runner (backend restarted mid-run,
    or startup recovery could not complete) so it can be paused/resumed/
    flattened again instead of only force-stopped.

    Paper rebuilds broker state from the durable positions/events tables; LIVE
    reconciles against Schwab — real positions, resting protective orders
    adopted rather than re-placed — and always returns PAUSED, so it restores
    control of the run without resuming real-money order entry (that still
    needs the separate activate confirmation). See runtime/recovery.py."""
    app = request.app.state
    try:
        result = await recover_run_now(app.db, app.run_manager, run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — surface the real reason recovery failed
        log.error("run_recover_endpoint_failed", run_id=run_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"recovery failed: {e}")
    return {"run_id": run_id, "event": "recovered", **result}


async def _realized_pnl_totals(db, run_ids: list[str]) -> dict[str, float]:
    """Lifetime realized P&L per run, summed from the `fill` journal events.

    Same source `recovery._reconstruct_today_counters` uses for the daily
    figure, just without the ts>=today filter, so the two columns can never
    disagree about what a fill was worth. Runs with no closing fill yet are
    absent from the result (the caller treats that as 0.0). Realized only —
    an open position's mark-to-market is deliberately not included, because
    the daily column next to it is realized-only too."""
    if not run_ids:
        return {}
    placeholders = ",".join("?" * len(run_ids))
    rows = await db.fetch_all(
        f"""SELECT run_id, SUM(json_extract(payload_json, '$.realized_pnl')) AS total
            FROM run_events
            WHERE kind='fill' AND run_id IN ({placeholders})
              AND json_extract(payload_json, '$.realized_pnl') IS NOT NULL
            GROUP BY run_id""", tuple(run_ids))
    return {r["run_id"]: float(r["total"] or 0.0) for r in rows}


def _apply_total_pnl(d: dict, total: float) -> None:
    """Attach `realized_pnl_total` and its percentage of starting equity."""
    d["realized_pnl_total"] = round(total, 2)
    equity = d.get("starting_equity")
    d["realized_pnl_total_pct"] = (
        round(total / equity * 100.0, 2) if equity else None)


@router.get("/runs")
async def list_runs(request: Request) -> dict:
    app = request.app.state
    rows = await app.db.fetch_all(
        """SELECT r.id, r.mode, r.state, r.started_at, r.stopped_at, r.starting_equity,
           s.slug, s.name
           FROM runs r JOIN strategies s ON s.id = r.strategy_id
           ORDER BY r.started_at DESC LIMIT 200""")
    active = {status["run_id"]: status for status in app.run_manager.list_active()}
    totals = await _realized_pnl_totals(app.db, [row["id"] for row in rows])
    out = []
    for row in rows:
        d = dict(row)
        _apply_total_pnl(d, totals.get(row["id"], 0.0))
        live = active.get(row["id"])
        if live:
            d["live"] = live
        # AUDIT C1 — the Running list is where a user watches runs, so it is
        # where "this run is not honouring part of its document" has to be
        # visible. Loaded from the strategy FILE rather than journaled on the
        # run, so an edited document shows its current gaps; the run's own
        # start event records what was true when it started.
        try:
            _doc = app.strategy_files.load(row["slug"])
            d["live_support_gaps"] = gap_payloads(_doc)
            # The Running list must make instrument obvious at a glance: an
            # options run and a shares run have very different risk shapes and
            # a user should never have to open one to find out which it is.
            _expr = getattr(_doc, "expression", None)
            d["instrument"] = ("options" if _expr is not None
                               and _expr.instrument == "options" else "shares")
        except Exception:      # noqa: BLE001 — a missing/invalid file must not 500 the list
            d["live_support_gaps"] = []
            d["instrument"] = "shares"
        out.append(d)
    return {"runs": out}


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict:
    app = request.app.state
    row = await app.db.fetch_one(
        """SELECT r.*, s.slug, s.name FROM runs r JOIN strategies s ON s.id = r.strategy_id
           WHERE r.id=?""", (run_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    d = dict(row)
    _apply_total_pnl(d, (await _realized_pnl_totals(app.db, [run_id])).get(run_id, 0.0))
    try:
        d["live"] = app.run_manager.status(run_id)
    except KeyError:
        pass  # not currently active — DB row + recent events is all there is
    events = await app.db.fetch_all(
        "SELECT ts, level, kind, payload_json FROM run_events WHERE run_id=? "
        "ORDER BY ts DESC LIMIT 200", (run_id,))
    d["recent_events"] = [dict(e) for e in events]
    return d


# ---- kill switch -------------------------------------------------------------

@router.get("/kill")
async def kill_status(request: Request) -> dict:
    return {"active": await request.app.state.gateway.kill_switch_active()}


@router.post("/kill")
async def activate_kill(request: Request, body: dict) -> dict:
    app = request.app.state
    await app.gateway.activate_kill_switch(actor="user")
    failed_cancels_by_run: dict[str, list[str]] = {}
    if bool(body.get("cancel_all", False)):
        failed_cancels_by_run = await app.run_manager.cancel_all_working_orders(actor="user")
    return {"active": True, "failed_cancels_by_run": failed_cancels_by_run}


@router.delete("/kill")
async def deactivate_kill(request: Request) -> dict:
    await request.app.state.gateway.deactivate_kill_switch(actor="user")
    return {"active": False}


# ---- global risk-limit overrides ---------------------------------------------

@router.get("/risk/limits")
async def get_risk_limits(request: Request) -> dict:
    return request.app.state.run_manager.get_global_limits()


@router.put("/risk/limits")
async def put_risk_limits(request: Request, body: dict) -> dict:
    try:
        return await request.app.state.run_manager.set_global_limits(actor="user", **body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
