"""Execution-safety API — closing the loop on UNKNOWN orders (backend/app/
execution/gateway.py's `resolve_unknown_intent`). An UNKNOWN order_intent
means the broker call raised something ambiguous (5xx/timeout); a human must
confirm the order's true state before it stops counting toward the
reconciliation engine's "unresolved UNKNOWN" auto kill-switch trip. No
app-level auth (server binds 127.0.0.1 only, same as every other route).

Also exposes a live, direct-from-Schwab view of open orders + cancel
(`/open-orders`). This is DELIBERATELY independent of RunManager/any run's
SchwabBroker instance: an order placed by a run that has since stopped (or
crashed, or was force-closed after a restart — see runtime/manager.py's
`_force_close_dangling`) has no in-process broker left tracking it at all,
so it would otherwise be invisible and uncancelable through this app
forever (found live 2026-07-24 — a stopped run's real order kept sitting
open on Schwab with no way to see or cancel it here). Cancel is a real,
irreversible action against a real account — this route only ever executes
it on an explicit user-initiated request, never automatically."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request

from ..auth.manager import NotAuthenticatedError
from ..logging import get_logger
from ..schwab.client import SchwabAPIError

router = APIRouter(prefix="/api/execution")
log = get_logger("api-execution")

# Schwab's full apiOrderStatus enum (schwab/trader-api--individual/README.md
# §apiOrderStatus) minus the terminal/closed ones -- everything else still
# has a chance of filling or being canceled, i.e. counts as "open" here.
_CLOSED_ORDER_STATUSES = {"FILLED", "CANCELED", "REJECTED", "REPLACED", "EXPIRED"}
_OPEN_ORDERS_LOOKBACK_DAYS = 7  # this app only ever places DAY orders (base.py)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@router.get("/open-orders")
async def list_open_orders(request: Request) -> dict:
    app = request.app.state
    try:
        hashes = [h["hashValue"] for h in await app.schwab.get_account_numbers()]
    except NotAuthenticatedError:
        raise HTTPException(status_code=424, detail="Not connected to Schwab")
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    now = datetime.now(timezone.utc)
    from_iso = _iso(now - timedelta(days=_OPEN_ORDERS_LOOKBACK_DAYS))
    to_iso = _iso(now)
    out = []
    for account_hash in hashes:
        try:
            orders = await app.schwab.get_orders(account_hash, from_iso, to_iso)
        except SchwabAPIError as e:
            log.error("open_orders_fetch_failed", account_hash=account_hash, error=str(e))
            continue
        for o in orders:
            if o.get("status") in _CLOSED_ORDER_STATUSES:
                continue
            out.append({**o, "account_hash": account_hash})
    out.sort(key=lambda o: o.get("enteredTime", ""), reverse=True)
    return {"orders": out}


@router.get("/orders/{order_id}")
async def get_order_status(request: Request, order_id: str, account_hash: str) -> dict:
    """Single-order status lookup, used to back the expand-to-cancel UI on a
    run's order-kind event log entries (those only carry a broker_order_id,
    not the full order, and may have already gone stale by the time a user
    clicks into them)."""
    app = request.app.state
    try:
        order = await app.schwab.get_order(account_hash, order_id)
    except NotAuthenticatedError:
        raise HTTPException(status_code=424, detail="Not connected to Schwab")
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return order


@router.post("/open-orders/{order_id}/cancel")
async def cancel_open_order(request: Request, order_id: str, body: dict) -> dict:
    account_hash = body.get("account_hash")
    if not account_hash:
        raise HTTPException(status_code=400, detail="account_hash required")
    app = request.app.state
    try:
        await app.schwab.cancel_order(account_hash, order_id)
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    await app.db.audit("user", "run_control", {
        "event": "order_canceled_manual", "order_id": order_id, "account_hash": account_hash})
    log.warning("order_canceled_manual", order_id=order_id, account_hash=account_hash)
    return {"order_id": order_id, "canceled_at": int(time.time() * 1000)}


@router.get("/unknown-orders")
async def list_unknown_orders(request: Request) -> dict:
    rows = await request.app.state.db.fetch_all(
        "SELECT * FROM order_intents WHERE status='UNKNOWN' ORDER BY ts DESC")
    return {"intents": [dict(r) for r in rows]}


@router.post("/unknown-orders/{intent_id}/resolve")
async def resolve_unknown_order(request: Request, intent_id: str, body: dict) -> dict:
    resolution = body.get("resolution")
    if not resolution:
        raise HTTPException(status_code=400, detail="resolution is required")
    try:
        await request.app.state.gateway.resolve_unknown_intent(
            intent_id, actor="user", resolution=resolution)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"intent_id": intent_id, "status": "RESOLVED_MANUAL"}
