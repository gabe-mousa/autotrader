"""Phase 5.5's hidden dev screen API (docs/plan/11-roadmap.md) — manually
place/preview/cancel/replace tiny REAL orders against the user's real Schwab
account, for validating the broker/streaming plumbing before any strategy
gets live autonomy. Every mutating endpoint requires `confirm: true` in the
body on top of the `live_probe_enabled` config gate (checked here, before
`app.state.live_probe` is ever touched)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..auth.manager import NotAuthenticatedError
from ..live.probe import LiveProbeError
from ..logging import get_logger
from ..schwab.client import SchwabAPIError

router = APIRouter(prefix="/api/live-probe")
log = get_logger("api-live-probe")


def _require_enabled(request: Request) -> None:
    if not request.app.state.settings.live_probe_enabled:
        raise HTTPException(
            status_code=403,
            detail="Live probe is disabled — set live_probe_enabled=true in .env to use it. "
                   "This is the only path in the app that can place a real order.")


def _order_fields(body: dict) -> dict:
    return {
        "account_hash": body.get("account_hash"),
        "symbol": str(body.get("symbol", "")).upper(),
        "side": body.get("side"),
        "qty": float(body.get("qty", 0)),
        "order_type": body.get("order_type"),
        "limit_px": body.get("limit_px"),
        "stop_px": body.get("stop_px"),
    }


@router.get("/status")
async def status(request: Request) -> dict:
    app = request.app.state
    enabled = app.settings.live_probe_enabled
    out = {"enabled": enabled, "max_qty": app.settings.live_probe_max_qty,
          "max_notional": app.settings.live_probe_max_notional, "accounts": []}
    if not enabled:
        return out
    out["kill_switch_active"] = await app.gateway.kill_switch_active()
    try:
        hashes = await app.schwab.get_account_numbers()
        accounts = await app.schwab.get_accounts()
    except NotAuthenticatedError:
        raise HTTPException(status_code=424, detail="Not connected to Schwab")
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    by_number = {str(a.get("securitiesAccount", {}).get("accountNumber")): a for a in accounts}
    for h in hashes:
        number = h.get("accountNumber", "")
        acct = by_number.get(number, {}).get("securitiesAccount", {})
        out["accounts"].append({
            "hash": h.get("hashValue"),
            "account_number_masked": f"••••{number[-4:]}" if number else "unknown",
            "type": acct.get("type", "?"),
        })
    return out


@router.post("/preview")
async def preview(request: Request, body: dict) -> dict:
    _require_enabled(request)
    f = _order_fields(body)
    try:
        return await request.app.state.live_probe.preview(
            f["account_hash"], f["symbol"], f["side"], f["qty"], f["order_type"],
            f["limit_px"], f["stop_px"])
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/orders")
async def place(request: Request, body: dict) -> dict:
    _require_enabled(request)
    f = _order_fields(body)
    try:
        return await request.app.state.live_probe.place(
            f["account_hash"], f["symbol"], f["side"], f["qty"], f["order_type"],
            f["limit_px"], f["stop_px"], confirm=bool(body.get("confirm", False)))
    except LiveProbeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/orders/{broker_order_id}/cancel")
async def cancel(request: Request, broker_order_id: str, body: dict) -> dict:
    _require_enabled(request)
    account_hash = body.get("account_hash")
    try:
        await request.app.state.live_probe.cancel(
            account_hash, broker_order_id, confirm=bool(body.get("confirm", False)))
    except LiveProbeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"broker_order_id": broker_order_id, "event": "cancel_requested"}


@router.put("/orders/{broker_order_id}")
async def replace(request: Request, broker_order_id: str, body: dict) -> dict:
    _require_enabled(request)
    f = _order_fields(body)
    try:
        return await request.app.state.live_probe.replace(
            f["account_hash"], broker_order_id, f["symbol"], f["side"], f["qty"],
            f["order_type"], f["limit_px"], f["stop_px"],
            confirm=bool(body.get("confirm", False)))
    except LiveProbeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/orders")
async def list_orders(request: Request) -> dict:
    _require_enabled(request)
    return {"orders": await request.app.state.live_probe.list_orders()}


@router.get("/reconcile")
async def reconcile(request: Request, account_hash: str) -> dict:
    _require_enabled(request)
    try:
        return await request.app.state.live_probe.reconcile(account_hash)
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/activity-log")
async def activity_log(request: Request) -> dict:
    _require_enabled(request)
    return {"messages": await request.app.state.live_probe.activity_log()}
