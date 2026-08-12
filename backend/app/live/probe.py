"""LiveProbeService — Phase 5.5's "hidden dev screen" backend (docs/plan/
11-roadmap.md): manually place/preview/cancel/replace tiny REAL orders
directly through `SchwabBroker`, to validate the plumbing before any
strategy ever touches live money. "No strategy autonomy yet" per the plan —
this is a human clicking a button once, not an automated run.

Deliberately does NOT go through `OrderGateway`/`RunManager`. Those are
shaped around autonomous strategy runs (`run_id`, `RiskContext`, the
promotion gate) that don't fit a one-off manual action, and reusing them
would mean inventing a fake `RiskContext` just to satisfy a Protocol that
doesn't conceptually apply here. Instead this service has its OWN narrower
gate: the `live_probe_enabled` config flag (checked by the API layer before
this is ever constructed), a hard qty cap, the SAME global kill switch
`OrderGateway` already owns (reused via `OrderGateway.kill_switch_active()`,
never duplicated), and `confirm=True` required on every mutating call.

Every action is still journaled into the exact same `order_intents`/`orders`
tables the gateway uses, under a fixed sentinel `run_id="live-probe"` — that
id never appears in the `runs` table, so `RunManager`/`recovery.py`'s
queries (all scoped to real `runs` rows) never see or touch it. This keeps
the probe's own audit trail consistent with everything else in the app
without giving it any of RunManager's autonomy."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from ..broker.base import BrokerEvent, BrokerRejection, OrderRequest
from ..broker.schwab import SchwabBroker
from ..db import Database
from ..execution.gateway import OrderGateway
from ..logging import get_logger
from ..marketdata.service import MarketDataService
from ..schwab.client import SchwabClient
from ..streamer.acct_activity import AcctActivityRecorder
from ..streamer.client import StreamerClient

log = get_logger("live-probe")

PROBE_RUN_ID = "live-probe"
_STATUS_ON_EVENT = {"fill": "FILLED", "canceled": "CANCELED", "rejected": "REJECTED"}
MAX_QUOTE_AGE_S = 10.0  # same freshness bar as OrderGateway's own price-sanity check
QUOTE_WAIT_TIMEOUT_S = 3.0  # how long to wait for the stream to deliver a first quote


class LiveProbeError(Exception):
    """A probe-level rejection (not confirmed, over the qty/notional cap, kill
    switch active, or the broker itself certainly rejected it) — always
    safe/certain, never means "an order might have gone through unknown.\""""


class LiveProbeService:
    def __init__(self, db: Database, schwab: SchwabClient, gateway: OrderGateway,
                max_qty: float, activity: AcctActivityRecorder,
                marketdata: MarketDataService, max_notional: float,
                streamer: StreamerClient, quote_wait_timeout_s: float = QUOTE_WAIT_TIMEOUT_S):
        self._db = db
        self._schwab = schwab
        self._gateway = gateway
        self._max_qty = max_qty
        self._activity = activity
        self._marketdata = marketdata
        self._max_notional = max_notional
        self._streamer = streamer
        self._quote_wait_timeout_s = quote_wait_timeout_s
        self._brokers: dict[str, SchwabBroker] = {}  # account_hash -> SchwabBroker

    def _broker_for(self, account_hash: str) -> SchwabBroker:
        broker = self._brokers.get(account_hash)
        if broker is None:
            broker = SchwabBroker(self._schwab, account_hash)
            broker.subscribe_events(self._on_broker_event)
            self._brokers[account_hash] = broker
        return broker

    async def _ensure_quote(self, symbol: str) -> dict | None:
        """A market order has no explicit price, so the notional check needs
        our own streamed quote (Schwab's previewOrder has its own internal
        pricing and never populates it — confirmed empirically, not just by
        reading the API docs). Rather than requiring the human to click
        Preview first and race the 10s freshness window before clicking
        Place, subscribe here and wait briefly for the stream to deliver
        one. Still fails closed (returns None) if nothing arrives in time —
        e.g. market closed or a bad symbol."""
        quote = self._marketdata.latest_cached_quote(symbol, max_age_s=MAX_QUOTE_AGE_S)
        if quote is not None:
            return quote
        await self._streamer.subscribe("LEVELONE_EQUITIES", [symbol])
        deadline = time.monotonic() + self._quote_wait_timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            quote = self._marketdata.latest_cached_quote(symbol, max_age_s=MAX_QUOTE_AGE_S)
            if quote is not None:
                return quote
        return None

    async def _reference_price(self, symbol: str, limit_px: float | None,
                               stop_px: float | None) -> float | None:
        """Best available price to sanity-check notional against: the order's
        own limit/stop price if it has one (exact, no lookup needed), else a
        fresh streamed quote for market orders. None means "can't tell" —
        callers must fail closed on that, not assume it's fine."""
        if limit_px is not None:
            return limit_px
        if stop_px is not None:
            return stop_px
        quote = await self._ensure_quote(symbol)
        if quote is None:
            return None
        last = quote.get("last")
        if last:
            return last
        bid, ask = quote.get("bid"), quote.get("ask")
        if bid and ask:
            return (bid + ask) / 2
        return None

    async def _require_confirmed(self, confirm: bool, qty: float | None = None,
                                 symbol: str | None = None, limit_px: float | None = None,
                                 stop_px: float | None = None) -> None:
        if not confirm:
            raise LiveProbeError(
                "confirm=true is required to place/cancel/replace a real order")
        if qty is not None and qty > self._max_qty:
            raise LiveProbeError(f"qty {qty} exceeds the probe cap of {self._max_qty}")
        if qty is not None and symbol is not None:
            price = await self._reference_price(symbol, limit_px, stop_px)
            if price is None:
                raise LiveProbeError(
                    f"no quote available for {symbol} after waiting "
                    f"{self._quote_wait_timeout_s:.0f}s for the stream to price it — "
                    "market may be closed/symbol may be wrong, or use a limit order")
            notional = qty * price
            if notional > self._max_notional:
                raise LiveProbeError(
                    f"{qty} @ ~{price:.2f} = ${notional:.2f}, over the probe cap of "
                    f"${self._max_notional:.2f}")
        if await self._gateway.kill_switch_active():
            raise LiveProbeError(
                "the global kill switch is active — no live orders can be placed")

    # ---- read-only ----------------------------------------------------------
    async def preview(self, account_hash: str, symbol: str, side: str, qty: float,
                      order_type: str, limit_px: float | None, stop_px: float | None) -> dict:
        req = OrderRequest(intent_id=str(uuid.uuid4()), symbol=symbol, side=side, qty=qty,
                           order_type=order_type, limit_px=limit_px, stop_px=stop_px)
        return await self._broker_for(account_hash).preview(req)

    async def list_orders(self, limit: int = 100) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT * FROM order_intents WHERE run_id=? ORDER BY ts DESC LIMIT ?",
            (PROBE_RUN_ID, limit))
        return [dict(r) for r in rows]

    async def activity_log(self, limit: int = 100) -> list[dict]:
        return await self._activity.recent(limit)

    async def reconcile(self, account_hash: str) -> dict:
        """Side-by-side broker truth vs the local journal for every probe
        order — the whole point of Phase 5.5 is confirming these agree."""
        broker = self._broker_for(account_hash)
        positions = await broker.get_positions()
        account = await broker.get_account()
        now_ms = int(time.time() * 1000)
        from_iso = time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime(now_ms / 1000))
        to_iso = time.strftime("%Y-%m-%dT23:59:59.000Z", time.gmtime(now_ms / 1000))
        broker_orders_today = await self._schwab.get_orders(account_hash, from_iso, to_iso)
        return {
            "broker_positions": positions,
            "broker_account": account,
            "broker_orders_today": broker_orders_today,
            "local_orders": await self.list_orders(),
        }

    # ---- mutating (real money) ------------------------------------------
    async def place(self, account_hash: str, symbol: str, side: str, qty: float,
                    order_type: str, limit_px: float | None, stop_px: float | None,
                    confirm: bool) -> dict:
        await self._require_confirmed(confirm, qty, symbol, limit_px, stop_px)
        broker = self._broker_for(account_hash)
        intent_id = str(uuid.uuid4())
        req = OrderRequest(intent_id=intent_id, symbol=symbol, side=side, qty=qty,
                           order_type=order_type, limit_px=limit_px, stop_px=stop_px)
        await self._journal_intent(intent_id, req, "PENDING_SUBMIT")
        try:
            handle = await broker.place(req)
        except BrokerRejection as e:
            await self._update_intent(intent_id, "REJECTED")
            await self._db.audit("user", "live_probe",
                                 {"event": "rejected", "intent_id": intent_id, "reason": str(e)})
            raise LiveProbeError(str(e)) from e
        except Exception as e:  # noqa: BLE001 — genuinely ambiguous, never retried
            await self._update_intent(intent_id, "UNKNOWN")
            await self._db.audit("user", "live_probe",
                                 {"event": "unknown", "intent_id": intent_id, "error": str(e)})
            raise
        await self._record_order(intent_id, handle.broker_order_id)
        await self._update_intent(intent_id, "SUBMITTED")
        await self._db.audit("user", "live_probe", {
            "event": "placed", "intent_id": intent_id, "broker_order_id": handle.broker_order_id,
            "account_hash": account_hash, "symbol": symbol, "side": side, "qty": qty,
            "order_type": order_type})
        log.warning("live_probe_order_placed", intent_id=intent_id,
                   broker_order_id=handle.broker_order_id, symbol=symbol, side=side, qty=qty)
        return {"intent_id": intent_id, "broker_order_id": handle.broker_order_id,
               "status": handle.status}

    async def cancel(self, account_hash: str, broker_order_id: str, confirm: bool) -> None:
        await self._require_confirmed(confirm)
        await self._broker_for(account_hash).cancel(broker_order_id)
        await self._db.audit("user", "live_probe",
                             {"event": "cancel_requested", "broker_order_id": broker_order_id})

    async def replace(self, account_hash: str, broker_order_id: str, symbol: str, side: str,
                      qty: float, order_type: str, limit_px: float | None,
                      stop_px: float | None, confirm: bool) -> dict:
        await self._require_confirmed(confirm, qty, symbol, limit_px, stop_px)
        broker = self._broker_for(account_hash)
        intent_id = str(uuid.uuid4())
        req = OrderRequest(intent_id=intent_id, symbol=symbol, side=side, qty=qty,
                           order_type=order_type, limit_px=limit_px, stop_px=stop_px)
        await self._journal_intent(intent_id, req, "PENDING_SUBMIT")
        handle = await broker.replace(broker_order_id, req)
        await self._record_order(intent_id, handle.broker_order_id)
        await self._update_intent(intent_id, "SUBMITTED")
        await self._db.audit("user", "live_probe", {
            "event": "replaced", "old_broker_order_id": broker_order_id,
            "new_broker_order_id": handle.broker_order_id, "intent_id": intent_id})
        return {"intent_id": intent_id, "broker_order_id": handle.broker_order_id,
               "status": handle.status}

    # ---- journaling (same tables OrderGateway uses) ---------------------
    async def _journal_intent(self, intent_id: str, req: OrderRequest, status: str) -> None:
        await self._db.execute(
            """INSERT INTO order_intents (id, run_id, ts, symbol, side, qty, order_type,
               limit_px, stop_px, purpose, status, gateway_decision_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (intent_id, PROBE_RUN_ID, int(time.time() * 1000), req.symbol, req.side, req.qty,
             req.order_type, req.limit_px, req.stop_px, "probe", status,
             # gateway_decision_json is always a LIST of check records
             # elsewhere (OrderGateway.submit's `checks`, api/orders.py's
             # order_timeline reads it as one) — probe orders skip the
             # gateway's 8-check stack entirely (module docstring), but must
             # still match that shape, or the Orders page's timeline view
             # crashes trying to .map() over it (found live: every live-probe
             # order 500'd the whole SPA when expanded).
             json.dumps([{"source": "live_probe"}])))

    async def _update_intent(self, intent_id: str, status: str) -> None:
        await self._db.execute(
            "UPDATE order_intents SET status=? WHERE id=?", (status, intent_id))

    async def _record_order(self, intent_id: str, broker_order_id: str) -> None:
        await self._db.execute(
            """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag, status,
               submitted_at) VALUES (?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), intent_id, PROBE_RUN_ID, "live", broker_order_id, intent_id,
             "SUBMITTED", int(time.time() * 1000)))

    async def _on_broker_event(self, event: BrokerEvent) -> None:
        new_status = _STATUS_ON_EVENT.get(event.kind)
        if new_status is None:
            return
        await self._db.execute(
            "UPDATE orders SET status=?, closed_at=? WHERE schwab_order_id=? AND run_id=?",
            (new_status, int(time.time() * 1000), event.broker_order_id, PROBE_RUN_ID))
        await self._db.audit("system", "live_probe", {
            "event": event.kind, "broker_order_id": event.broker_order_id, "reason": event.reason,
            "fill_qty": event.fill.qty if event.fill else None,
            "fill_price": event.fill.price if event.fill else None})
