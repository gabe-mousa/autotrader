"""SchwabBroker — the live-trading `Broker` implementation (docs/plan/06,
Phase 5.5). Implements the exact same Protocol as `SimBroker` (`base.py`) so
`StrategyRunner`/`OrderGateway` code never knows or cares which one it's
talking to — the safety property that a paper run can never reach this class
comes from WHICH broker object is constructed for a given run, not from a
type check here.

v1 fill-detection is POLLING, not ACCT_ACTIVITY, and that's deliberate.
Schwab's ACCT_ACTIVITY streaming service has a documented ENVELOPE but the
message-type-specific JSON shapes are NOT published anywhere (see
schwab/market-data-production/README.md §ACCT_ACTIVITY) — Phase 5.5's whole
purpose is observing them empirically with real orders
(`app/streamer/acct_activity.py` captures every raw message to the DB for
exactly that). Guessing at an undocumented wire format that moves real money
is not acceptable, so this class uses the fully-documented, guaranteed-
correct mechanism plan 06 already names as the fallback ("GET order polling,
5s, while any order open") as the PRIMARY path.

STATUS 2026-07-29 — real payloads have now been captured, and polling still
stands. `acct_activity.py`'s docstring records the observed wire format in
full; the parts relevant here:
  * The correlation key is solved. Message data's `SchwabOrderID` is
    byte-identical to the id `place_order` returns and `_working` is keyed
    by, so a fast path would need no id translation.
  * A COMPLETE fill of a market order is legible: `OrderFillCompleted` with
    LegStatus "LegClosed" / LegSubStatus "LegSubStatusFilled" carries
    QuantityInfo.CumulativeQuantity and .AveragePrice in Schwab's packed
    {"lo","signScale"} decimal.
  * A client cancel is legible: `OrderUROutCompleted` with LegSubStatus
    "LegSubStatusCancelled".
Not built anyway, because the cases where a mis-read is most dangerous have
STILL never been observed even once: no PARTIAL fill (a partial mistaken for
a complete one would leave the runner believing a position is fully
established), no rejection event, no replace/modify event, and no short-sale
event. Worse, the feed is not currently proving itself in production — the
capture holds zero order events after 2026-07-24, including for the four
real orders the live run filled on 2026-07-29. A fast path on a feed that
delivered none of the last four fills adds mis-read risk for no latency
benefit, so the guaranteed-correct poll remains the only fill source.

To finish the parser, a future session needs: captured examples of a partial
fill, a rejection, and a replace; an independent cross-check of a decoded
AveragePrice against the REST `get_order` price for the SAME order id; and
an explanation of the post-07-24 capture blackout. It can then be layered in
without changing this class's public shape or the runner's expectations —
with polling left running as the reconciling authority, and any message the
parser does not positively recognise falling through to it rather than being
interpreted.

Order translation is deliberately narrow — SINGLE-strategy market/limit/
stop/stop_limit EQUITY orders, NORMAL session, DAY duration — mirroring
`OrderRequest`'s own scope (`base.py`) and `SimBroker`'s. OCO/bracket linkage
is composed one layer up by the runner via `broker.cancel()` on the sibling,
exactly like SimBroker — this class only ever places single orders.

Rejection semantics (docs/plan/06 order lifecycle): a 4xx from Schwab means
the order CERTAINLY never got placed → raises `BrokerRejection` (the gateway
marks REJECTED, safe/certain). A 5xx or any other exception leaves the
outcome genuinely ambiguous → propagates un-wrapped (the gateway marks
UNKNOWN, never blind-retried)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ..logging import get_logger
from ..options.symbology import underlying_of
from ..schwab.client import SchwabAPIError, SchwabClient
from .base import (
    BrokerEvent,
    BrokerEventHandler,
    BrokerOrderHandle,
    BrokerRejection,
    Fill,
    OrderRequest,
    valid_side_for,
)

log = get_logger("schwab-broker")

DEFAULT_POLL_INTERVAL_S = 5.0
TERMINAL_STATUSES = {"CANCELED", "REJECTED", "EXPIRED"}
# Schwab's order-history endpoint serves ~60 days back and errors on more.
_ORDER_LOOKBACK_DAYS = 55
_ORDER_TYPE_MAP = {"market": "MARKET", "limit": "LIMIT", "stop": "STOP", "stop_limit": "STOP_LIMIT"}
# Schwab distinguishes opening/closing a short from an ordinary buy/sell. Using
# SELL where SELL_SHORT is meant (or BUY where BUY_TO_COVER is meant) is
# accepted by the API but produces the WRONG position, so the mapping is exact.
_INSTRUCTION_MAP = {
    "buy": "BUY", "sell": "SELL",
    "sell_short": "SELL_SHORT", "buy_to_cover": "BUY_TO_COVER",
    # Options use a DISJOINT instruction set. Schwab's validity matrix
    # (schwab/trader-api--individual/README.md lines 373-382) rejects BUY/SELL
    # on an OPTION leg and rejects *_TO_OPEN/*_TO_CLOSE on an EQUITY leg, so
    # these can never be used interchangeably.
    "buy_to_open": "BUY_TO_OPEN", "sell_to_close": "SELL_TO_CLOSE",
    "sell_to_open": "SELL_TO_OPEN", "buy_to_close": "BUY_TO_CLOSE",
}

#: Net pricing for multi-leg orders. Single-leg option orders use the ordinary
#: LIMIT/MARKET types.
_NET_PRICE_TYPES = frozenset({"NET_DEBIT", "NET_CREDIT", "NET_ZERO"})

# Substrings identifying a short-sale locate/borrow refusal. Schwab does not
# use a distinct status code for these and the wording varies, so match on the
# message. Being generous here is SAFE: the worst case is classifying a genuine
# ambiguity as a definite rejection, which leaves the run flat rather than
# holding an unknown position.
_BORROW_FAILURE_MARKERS = (
    "not shortable", "no shares available", "hard to borrow", "hard-to-borrow",
    "htb", "locate", "borrow", "short sale not allowed", "unable to short",
    "shortability",
)


def is_borrow_failure(message: str) -> bool:
    """True when a broker error indicates the shares could not be borrowed."""
    m = (message or "").lower()
    return any(marker in m for marker in _BORROW_FAILURE_MARKERS)


@dataclass
class _WorkingOrder:
    req: OrderRequest
    broker_order_id: str
    filled_qty: float = 0.0


class SchwabBroker:
    mode = "live"

    def __init__(self, schwab: SchwabClient, account_hash: str,
                poll_interval_s: float = DEFAULT_POLL_INTERVAL_S):
        self._schwab = schwab
        self._account_hash = account_hash
        self._poll_interval_s = poll_interval_s
        self._working: dict[str, _WorkingOrder] = {}
        self._handlers: list[BrokerEventHandler] = []
        self._poll_task: asyncio.Task | None = None

    def subscribe_events(self, handler: BrokerEventHandler) -> None:
        self._handlers.append(handler)

    async def _emit(self, event: BrokerEvent) -> None:
        for h in self._handlers:
            try:
                await h(event)
            except Exception as e:  # noqa: BLE001 — one bad handler can't break the broker
                log.error("schwab_broker_event_handler_error", error=str(e))

    def _build_order(self, req: OrderRequest) -> dict:
        # NOTE: deliberately no "tag" key. Schwab's trading API 400s every
        # previewOrder/placeOrder call with a generic "A validation error
        # occurred" for this app registration whenever `tag` is present at
        # all — confirmed by testing single-char and empty-string values,
        # not just req.tag's UUID default. `req.tag`/`OrderRequest.tag`
        # still exists as a purely local correlation id (journaled into our
        # own `orders` table) and is never sent to Schwab.
        #
        # OPTIONS (plan/16 §10.2). The shapes below match Schwab's own
        # documented order samples verbatim — single-leg option limit at
        # trader-api--individual/README.md line 813, and the NET_DEBIT vertical
        # at line 840 — and tests/test_schwab_broker_options.py asserts that
        # byte-for-byte, so a future edit that drifts from the documented
        # request body fails loudly.
        if req.legs:
            return self._build_multileg_order(req)

        asset_type = req.asset_type
        if not valid_side_for(req.side, asset_type):
            # Local guard on Schwab's instruction/asset-type matrix. Catching it
            # here turns a confusing remote 400 into a precise local error, and
            # keeps a mis-mapped side from ever reaching the wire.
            raise ValueError(
                f"side {req.side!r} is not valid for assetType {asset_type!r}")

        order: dict = {
            "orderType": _ORDER_TYPE_MAP[req.order_type],
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [{
                "instruction": _INSTRUCTION_MAP[req.side],
                "quantity": req.qty,
                "instrument": {"symbol": req.symbol, "assetType": asset_type},
            }],
        }
        if asset_type == "OPTION":
            # Schwab's single-option sample carries this explicitly; it
            # distinguishes a plain option order from a leg of a complex one.
            order["complexOrderStrategyType"] = "NONE"
        if req.order_type in ("limit", "stop_limit"):
            order["price"] = req.limit_px
        if req.order_type in ("stop", "stop_limit"):
            order["stopPrice"] = req.stop_px
        return order

    def _build_multileg_order(self, req: OrderRequest) -> dict:
        """Multi-leg (v1.1 debit spreads). Net-priced at the order level, with
        one entry in orderLegCollection per leg."""
        for leg in req.legs:
            if not valid_side_for(leg.side, leg.asset_type):
                raise ValueError(
                    f"leg side {leg.side!r} is not valid for assetType "
                    f"{leg.asset_type!r}")
        net_type = req.net_price_type or "NET_DEBIT"
        if net_type not in _NET_PRICE_TYPES:
            raise ValueError(f"unsupported net price type {net_type!r}")
        order: dict = {
            "orderType": net_type,
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "complexOrderStrategyType": req.complex_strategy or "VERTICAL",
            "orderLegCollection": [{
                "instruction": _INSTRUCTION_MAP[leg.side],
                "quantity": leg.qty,
                "instrument": {"symbol": leg.symbol, "assetType": leg.asset_type},
            } for leg in req.legs],
        }
        if req.limit_px is not None:
            order["price"] = req.limit_px
        return order

    # ---- placement ----------------------------------------------------
    async def place(self, req: OrderRequest) -> BrokerOrderHandle:
        order = self._build_order(req)
        try:
            broker_order_id = await self._schwab.place_order(self._account_hash, order)
        except SchwabAPIError as e:
            if e.status < 500:
                # certainly never placed (bad symbol/qty/funds/etc.) — safe REJECTED
                raise BrokerRejection(str(e)) from e
            # A borrow/locate failure on a short can surface as a 5xx even
            # though the order definitively did NOT go through. Classifying it
            # UNKNOWN would strand the run waiting on reconciliation for an
            # order that never existed, so match it explicitly (plan/15 §B4).
            if is_borrow_failure(str(e)):
                raise BrokerRejection(f"short locate/borrow failed: {e}") from e
            raise  # other 5xx: genuinely ambiguous, gateway marks UNKNOWN, never retried
        self._working[broker_order_id] = _WorkingOrder(req=req, broker_order_id=broker_order_id)
        self._ensure_poller()
        await self._emit(BrokerEvent("accepted", broker_order_id, req.intent_id))
        return BrokerOrderHandle(broker_order_id, "WORKING")

    async def cancel(self, broker_order_id: str) -> None:
        await self._schwab.cancel_order(self._account_hash, broker_order_id)
        # No optimistic "canceled" emit here — the next poll confirms the
        # status Schwab actually settled on (a cancel can race a fill).

    async def replace(self, broker_order_id: str, req: OrderRequest) -> BrokerOrderHandle:
        order = self._build_order(req)
        new_id = await self._schwab.replace_order(self._account_hash, broker_order_id, order)
        self._working.pop(broker_order_id, None)
        self._working[new_id] = _WorkingOrder(req=req, broker_order_id=new_id)
        self._ensure_poller()
        return BrokerOrderHandle(new_id, "WORKING")

    # ---- restart recovery ----------------------------------------------
    def adopt(self, broker_order_id: str, req: OrderRequest, filled_qty: float = 0.0) -> None:
        """Re-attach an order that is ALREADY resting at Schwab to a freshly
        constructed broker (crash/restart recovery — see
        `runtime/recovery.py`).

        This is the whole reason live recovery differs from paper recovery.
        SimBroker dies with the process and takes its resting orders with it,
        so paper recovery RE-PLACES protective orders. A Schwab DAY order
        survives our process entirely: re-placing it would leave two stops
        resting for one position and oversell on the way out. So the order is
        adopted instead — registered in `_working` under the same
        broker_order_id and intent_id it had before, which restores all three
        things that were lost with the old instance: the poller detects its
        fill, `cancel()`/`replace()` can reach it (a trailing stop keeps
        ratcheting), and the emitted event still carries the intent_id the
        runner's `_orders` map is keyed by.

        `filled_qty` seeds the partial-fill watermark so an order that was
        already partially filled before the restart does not re-report the
        shares it filled BEFORE we lost track of it as a fresh fill (which
        would double-count them into the position)."""
        self._working[broker_order_id] = _WorkingOrder(
            req=req, broker_order_id=broker_order_id, filled_qty=filled_qty)
        self._ensure_poller()
        log.warning("schwab_order_adopted", broker_order_id=broker_order_id,
                    symbol=req.symbol, side=req.side, order_type=req.order_type,
                    filled_qty=filled_qty)

    async def get_open_orders(self) -> dict[str, dict]:
        """{broker_order_id: raw order} for every non-terminal order in the
        account, over the widest window Schwab's order history serves. Used by
        recovery to find out which of OUR journaled orders are genuinely still
        resting before deciding whether to adopt or re-place."""
        now = time.time()
        from_iso = time.strftime("%Y-%m-%dT00:00:00.000Z",
                                 time.gmtime(now - _ORDER_LOOKBACK_DAYS * 86_400))
        to_iso = time.strftime("%Y-%m-%dT23:59:59.000Z", time.gmtime(now))
        orders = await self._schwab.get_orders(self._account_hash, from_iso, to_iso)
        return {str(o["orderId"]): o for o in orders
                if o.get("orderId") is not None
                and o.get("status") not in TERMINAL_STATUSES | {"FILLED"}}

    async def get_positions(self) -> dict[str, float]:
        account = await self._schwab.get_account(self._account_hash, positions=True)
        acct = account.get("securitiesAccount", {})
        out: dict[str, float] = {}
        for pos in acct.get("positions", []) or []:
            symbol = pos.get("instrument", {}).get("symbol")
            if not symbol:
                continue
            qty = (pos.get("longQuantity", 0) or 0) - (pos.get("shortQuantity", 0) or 0)
            out[symbol] = out.get(symbol, 0.0) + qty
        return out

    async def get_positions_detail(self) -> dict[str, dict]:
        """{symbol: {"qty": signed, "avg_price": float, ...}} — `get_positions()`
        plus the broker's own average price, which recovery needs when Schwab
        holds a position our journal has no `positions` row for (it must still
        be adopted and protected, and Schwab's average price is the only
        entry price that exists for it).

        OPTION positions additionally carry their contract identity, read from
        Schwab's `AccountOption` instrument rather than inferred: putCall,
        optionMultiplier and underlyingSymbol. Recovery needs the multiplier in
        particular — adopting an option position at multiplier 1 would
        understate it by 100x — and it must come from the broker, because mini
        and adjusted contracts really do differ (plan/16 D10)."""
        account = await self._schwab.get_account(self._account_hash, positions=True)
        acct = account.get("securitiesAccount", {})
        out: dict[str, dict] = {}
        for pos in acct.get("positions", []) or []:
            inst = pos.get("instrument", {}) or {}
            symbol = inst.get("symbol")
            if not symbol:
                continue
            qty = (pos.get("longQuantity", 0) or 0) - (pos.get("shortQuantity", 0) or 0)
            prev = out.get(symbol)
            entry: dict = {
                "qty": (prev["qty"] if prev else 0.0) + qty,
                "avg_price": float(pos.get("averagePrice", 0) or 0)
                or (prev["avg_price"] if prev else 0.0),
            }
            asset_type = (inst.get("assetType") or "EQUITY").upper()
            entry["asset_type"] = asset_type
            if asset_type == "OPTION":
                entry["multiplier"] = float(inst.get("optionMultiplier") or 100.0)
                entry["put_call"] = (inst.get("putCall") or "").upper()
                entry["underlying"] = (inst.get("underlyingSymbol")
                                       or underlying_of(symbol))
                entry["market_value"] = float(pos.get("marketValue", 0) or 0)
            else:
                entry["multiplier"] = 1.0
                entry["underlying"] = symbol
            out[symbol] = entry
        return out

    async def preview(self, req: OrderRequest) -> dict:
        """Validate + estimate commission/fees WITHOUT placing anything —
        safe to call any time (no confirmation needed at this layer; the
        caller — app/live/probe.py — still requires one for place/replace)."""
        return await self._schwab.preview_order(self._account_hash, self._build_order(req))

    def working_order_ids(self) -> list[str]:
        return list(self._working.keys())

    async def get_account(self) -> dict:
        account = await self._schwab.get_account(self._account_hash, positions=True)
        acct = account.get("securitiesAccount", {})
        bal = acct.get("currentBalances", {}) or {}
        return {
            "cash": bal.get("cashBalance", bal.get("totalCash")),
            "equity": bal.get("equity", bal.get("liquidationValue")),
            "buying_power": bal.get("buyingPower", bal.get("cashAvailableForTrading")),
            # CASH | MARGIN — the gateway REFUSES to short in anything but a
            # margin account (plan/15 §B4). Surfaced here so the runtime never
            # has to reach into Schwab's raw payload shape.
            "account_type": str(acct.get("type", "") or "").upper(),
            "positions": await self.get_positions(),
        }

    # ---- fill polling (primary path — see module docstring) --------------
    def _ensure_poller(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop(), name="schwab-broker-poll")

    async def _poll_loop(self) -> None:
        while self._working:
            await asyncio.sleep(self._poll_interval_s)
            for working in list(self._working.values()):
                try:
                    await self._poll_one(working)
                except Exception as e:  # noqa: BLE001 — one bad poll can't kill the loop
                    log.error("schwab_broker_poll_failed",
                             broker_order_id=working.broker_order_id, error=str(e))

    async def _poll_one(self, working: _WorkingOrder) -> None:
        order = await self._schwab.get_order(self._account_hash, working.broker_order_id)
        status = order.get("status")
        filled_qty = float(order.get("filledQuantity", 0) or 0)
        delta = filled_qty - working.filled_qty
        if delta > 1e-9:
            price = self._avg_fill_price(order) or 0.0
            working.filled_qty = filled_qty
            await self._emit(BrokerEvent(
                "fill", working.broker_order_id, working.req.intent_id,
                fill=Fill(working.broker_order_id, time.time(), delta, price)))
        if status in TERMINAL_STATUSES:
            self._working.pop(working.broker_order_id, None)
            kind = "canceled" if status == "CANCELED" else "rejected"
            await self._emit(BrokerEvent(
                kind, working.broker_order_id, working.req.intent_id,
                reason=order.get("statusDescription") or status))
        elif status == "FILLED":
            self._working.pop(working.broker_order_id, None)

    @staticmethod
    def _avg_fill_price(order: dict) -> float | None:
        """Weighted average across every execution leg reported so far — an
        approximation (not incremental per-poll leg tracking), acceptable
        for v1's tiny single-share probe orders where multi-price partial
        fills are the rare exception, not the common case."""
        legs = []
        for activity in order.get("orderActivityCollection", []) or []:
            legs.extend(activity.get("executionLegs", []) or [])
        total_qty = sum(leg.get("quantity", 0) or 0 for leg in legs)
        if total_qty <= 0:
            return None
        return sum((leg.get("price", 0) or 0) * (leg.get("quantity", 0) or 0)
                   for leg in legs) / total_qty

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
