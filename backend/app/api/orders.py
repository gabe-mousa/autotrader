"""Orders & Transaction History API (docs/plan/09-ui-ux.md §7) — READ-ONLY
reporting over data other subsystems already write: `order_intents`/`orders`
(gateway + runner + live-probe journaling, see app/execution/gateway.py and
app/live/probe.py) and `run_events` (fill detail for paper runs, since the
`orders.status` column isn't kept live-updated for paper — see those
modules' docstrings). This file does not place, cancel, or modify any order.

Special case: Phase 5.5's manual live-order-probe tool journals under a
fixed sentinel run_id='live-probe' that has NO matching `runs` row (by
design — see app/live/probe.py). Every query here LEFT JOINs runs/strategies
and reports that case as strategy name "(live probe)" rather than dropping
the row or crashing.
"""

from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from ..logging import get_logger

router = APIRouter(prefix="/api")
log = get_logger("api-orders")

_LIVE_PROBE_RUN_ID = "live-probe"

# order_intents LEFT JOIN orders (an intent rejected at the gateway never
# gets an `orders` row) LEFT JOIN runs/strategies (the live-probe sentinel
# run_id has no `runs` row at all).
_BASE_SELECT = """
SELECT
    oi.id AS intent_id, oi.run_id, oi.ts, oi.symbol, oi.side, oi.qty,
    oi.order_type, oi.limit_px, oi.stop_px, oi.purpose,
    oi.status AS intent_status, oi.gateway_decision_json,
    -- options: `symbol` is the OCC contract for an option intent, so the UI
    -- needs `underlying` to group and filter the way a user thinks ("my SPY
    -- trades"), and the multiplier to show real dollar amounts.
    oi.asset_type, oi.underlying, oi.multiplier,
    o.id AS order_id, o.mode, o.schwab_order_id, o.tag,
    o.status AS order_status, o.submitted_at, o.closed_at,
    r.strategy_id, r.mode AS run_mode, s.slug, s.name
FROM order_intents oi
LEFT JOIN orders o ON o.intent_id = oi.id
LEFT JOIN runs r ON r.id = oi.run_id
LEFT JOIN strategies s ON s.id = r.strategy_id
"""


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["is_live_probe"] = d["run_id"] == _LIVE_PROBE_RUN_ID
    # Rows written before options existed have no asset_type; they are equities.
    d["asset_type"] = (d.get("asset_type") or "EQUITY").upper()
    d["underlying"] = d.get("underlying") or d["symbol"]
    d["multiplier"] = d.get("multiplier") or 1.0
    if d["asset_type"] == "OPTION":
        from ..options.symbology import describe as _describe
        d["contract_label"] = _describe(d["symbol"])
    if d["is_live_probe"]:
        d["slug"] = None
        d["name"] = "(live probe)"
    # mode: prefer the orders row's mode (kept live-updated for probe orders);
    # fall back to the run's mode; live-probe orders always have mode='live'
    # on their `orders` row anyway.
    order_mode = d.pop("mode", None)
    run_mode = d.pop("run_mode", None)
    d["mode"] = order_mode or run_mode
    # effective status: an intent that never got an orders row was rejected
    # before ever reaching the broker — surface the intent's own status.
    d["status"] = d["order_status"] or d["intent_status"]
    return d


def _apply_filters(sql: str, params: list, mode, status, symbol, run_id, start, end) -> tuple[str, list]:
    clauses = []
    if mode:
        # o.mode is NULL for any intent that never got an `orders` row
        # (rejected before reaching the broker, paper or live) -- fall back
        # to the run's own mode so a rejected order still matches its real
        # mode filter instead of silently vanishing from every mode filter.
        clauses.append("COALESCE(o.mode, r.mode) = ?")
        params.append(mode)
    if status:
        clauses.append("COALESCE(o.status, oi.status) = ?")
        params.append(status)
    if symbol:
        clauses.append("oi.symbol = ?")
        params.append(symbol.upper())
    if run_id:
        clauses.append("oi.run_id = ?")
        params.append(run_id)
    if start is not None:
        clauses.append("oi.ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("oi.ts <= ?")
        params.append(end)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return sql, params


@router.get("/orders")
async def list_orders(
    request: Request,
    mode: str | None = None,
    status: str | None = None,
    symbol: str | None = None,
    run_id: str | None = None,
    start: int | None = None,
    end: int | None = None,
    limit: int = 200,
) -> dict:
    app = request.app.state
    sql, params = _apply_filters(_BASE_SELECT, [], mode, status, symbol, run_id, start, end)
    sql += " ORDER BY oi.ts DESC LIMIT ?"
    params.append(limit)
    rows = await app.db.fetch_all(sql, tuple(params))
    return {"orders": [_row_to_dict(r) for r in rows]}


@router.get("/orders/export.csv")
async def export_orders_csv(
    request: Request,
    mode: str | None = None,
    status: str | None = None,
    symbol: str | None = None,
    run_id: str | None = None,
    start: int | None = None,
    end: int | None = None,
    limit: int = 200,
) -> Response:
    app = request.app.state
    sql, params = _apply_filters(_BASE_SELECT, [], mode, status, symbol, run_id, start, end)
    sql += " ORDER BY oi.ts DESC LIMIT ?"
    params.append(limit)
    rows = await app.db.fetch_all(sql, tuple(params))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "time", "strategy", "mode", "symbol", "side", "qty", "order_type",
        "limit_px", "stop_px", "purpose", "status", "submitted_at", "closed_at",
    ])
    for r in rows:
        d = _row_to_dict(r)
        strategy_label = "(live probe)" if d["is_live_probe"] else \
            f"{d['name']} ({d['slug']})" if d["name"] else d["run_id"]
        writer.writerow([
            d["ts"], strategy_label, d["mode"], d["symbol"], d["side"], d["qty"],
            d["order_type"], d["limit_px"], d["stop_px"], d["purpose"], d["status"],
            d["submitted_at"], d["closed_at"],
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders.csv"},
    )


@router.get("/orders/{intent_id}/timeline")
async def order_timeline(request: Request, intent_id: str) -> dict:
    app = request.app.state
    intent_row = await app.db.fetch_one(
        "SELECT * FROM order_intents WHERE id=?", (intent_id,))
    if intent_row is None:
        raise HTTPException(status_code=404, detail="order not found")
    intent = dict(intent_row)
    intent["gateway_decision"] = json.loads(intent["gateway_decision_json"]) \
        if intent["gateway_decision_json"] else []

    order_row = await app.db.fetch_one(
        "SELECT * FROM orders WHERE intent_id=?", (intent_id,))
    order = dict(order_row) if order_row else None

    # Best-effort correlation of run_events to this intent: same run_id,
    # same symbol, kind in ('fill','order'), within a generous +/- 1 day
    # window of the intent's ts. There's no FK from run_events to
    # order_intents (the runner just logs a fill event when it happens), so
    # this is a heuristic, not a guarantee — good enough for a human
    # eyeballing "what happened to this order" without overthinking it.
    window_ms = 24 * 60 * 60 * 1000
    event_rows = await app.db.fetch_all(
        """SELECT ts, level, kind, payload_json FROM run_events
           WHERE run_id=? AND kind IN ('fill','order')
           AND ts BETWEEN ? AND ?
           ORDER BY ts""",
        (intent["run_id"], intent["ts"] - window_ms, intent["ts"] + window_ms),
    )
    events = []
    for e in event_rows:
        d = dict(e)
        payload = json.loads(d["payload_json"])
        if payload.get("symbol") == intent["symbol"]:
            d["payload"] = payload
            events.append(d)

    return {
        "intent": intent,
        "order": order,
        "is_live_probe": intent["run_id"] == _LIVE_PROBE_RUN_ID,
        "matched_events": events,
    }
