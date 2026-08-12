"""OrderGateway — THE single chokepoint every order intent must pass through
(docs/plan/06-execution-safety.md). Every dangerous action funnels through
here; every decision is journaled BEFORE any broker call; every failure mode
degrades toward "flat and stopped," never "unknown position."

The 8-check safety stack runs IN ORDER, exactly as specified in the plan.
Kill-switch mass-cancel is a SEPARATE method (`kill_switch_cancel_all`) that
bypasses the stack entirely — cancels must still work while the kill switch
is active, which check #1 would otherwise block if cancels went through
`submit()` too."""

from __future__ import annotations

import time
import uuid

from ..broker.base import Broker, BrokerRejection, OrderRequest
from ..db import Database
from ..logging import get_logger
from ..options.symbology import InvalidOccSymbol, is_occ, parse_occ
from .rate_limiter import OrderRateLimiter
from .types import GatewayResult, GlobalRiskContext, OrderIntentRequest, RiskContext

log = get_logger("gateway")


def _today_et():
    """Exchange-local date. A contract expiring today is still tradeable all
    day, so the expiry check must use ET rather than UTC — otherwise a
    late-afternoon order on expiration day would be rejected as expired."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    return _dt.datetime.now(tz=ZoneInfo("America/New_York")).date()

DUPLICATE_WINDOW_S = 10.0
PRICE_SANITY_BAND = 0.05          # 5% around reference price for limit/stop
#: Options move far more in percentage terms than shares and quote much wider,
#: so the equity band would reject most honest option orders. Used only when no
#: live bid/ask is available; with a quote the band is spread-relative instead.
OPTION_PRICE_SANITY_BAND = 0.30
MAX_QUOTE_AGE_S = 10.0            # market orders need a fresher-than-this quote
UNFILLED_ACK_TIMEOUT_S = 10.0     # SUBMITTED with no ack -> caller should poll/resolve


class OrderGateway:
    def __init__(self, db: Database, orders_per_minute: int = 60,
                max_single_order_notional: float = 100_000):
        self._db = db
        self._limiter = OrderRateLimiter(orders_per_minute)
        self._max_single_order_notional = max_single_order_notional
        self._consecutive_broker_errors = 0

    # ---- kill switch (app_state-backed, single source of truth) ---------
    async def kill_switch_active(self) -> bool:
        return bool(await self._db.state_get("kill_switch_active", False))

    async def activate_kill_switch(self, actor: str = "user", reason: str | None = None) -> None:
        await self._db.state_set("kill_switch_active", True)
        await self._db.audit(actor, "kill_switch",
                             {"event": "activated", "reason": reason or "manual"})
        log.warning("kill_switch_activated", actor=actor, reason=reason or "manual")

    async def deactivate_kill_switch(self, actor: str = "user") -> None:
        await self._db.state_set("kill_switch_active", False)
        await self._db.audit(actor, "kill_switch", {"event": "deactivated"})
        log.warning("kill_switch_deactivated", actor=actor)

    async def kill_switch_cancel_all(self, broker: Broker, broker_order_ids: list[str],
                                     actor: str = "system") -> list[str]:
        """Mass-cancel — bypasses the 8-check stack (cancel is always safe;
        this is what lets you cancel WHILE the kill switch blocks new orders).
        Returns broker_order_ids that failed to cancel (for manual review)."""
        failed = []
        for oid in broker_order_ids:
            try:
                await broker.cancel(oid)
                await self._db.audit(actor, "kill_switch", {"event": "canceled", "order_id": oid})
            except Exception as e:  # noqa: BLE001 — never let one failure stop the sweep
                failed.append(oid)
                await self._db.audit(actor, "kill_switch",
                                     {"event": "cancel_failed", "order_id": oid, "error": str(e)})
                log.error("kill_switch_cancel_failed", order_id=oid, error=str(e))
        return failed

    # ---- the safety stack -------------------------------------------------
    async def submit(self, req: OrderIntentRequest, broker: Broker, risk: RiskContext,
                     global_risk: GlobalRiskContext | None = None,
                     auth_ok: bool = True, market_open: bool = True) -> GatewayResult:
        intent_id = str(uuid.uuid4())
        checks: list[dict] = []

        async def reject(check: str, reason: str) -> GatewayResult:
            checks.append({"check": check, "reason": reason})
            await self._journal_intent(intent_id, req, "REJECTED_LOCAL", checks)
            await self._db.audit("gateway", "order_rejected", {
                "intent_id": intent_id, "check": check, "reason": reason,
                "run_id": req.run_id, "symbol": req.symbol, "purpose": req.purpose})
            log.info("order_rejected", intent_id=intent_id, check=check, reason=reason)
            return GatewayResult(False, intent_id, None, "REJECTED_LOCAL", f"{check}: {reason}")

        # `scale_out` is a resting partial take-profit on an ALREADY-OPEN
        # position — same category as `target` for every check that keys off
        # this flag (never blocked by entry-side limits; price-sanity band
        # doesn't apply to a deliberately-away-from-market resting price).
        is_protective = req.purpose in ("exit", "stop", "target", "scale_out")

        # 1. kill switch — blocks ALL new placements, protective or not
        #    (protective exits on ALREADY-OPEN positions rely on resting
        #    broker-side stops, which is exactly why those are mandatory —
        #    see plan 06 §Stops: resting at the broker)
        if await self.kill_switch_active():
            return await reject("kill_switch", "global kill switch is active")

        # 2. auth gate — live only; paper's equivalent concern is streamer
        #    connectivity (DEGRADED_DATA), handled elsewhere
        if broker.mode == "live" and not auth_ok:
            return await reject("auth", "no valid Schwab access token")

        # 3. mode gate — defense in depth; real protection is which Broker
        #    class was constructed for this run in the first place
        if broker.mode != risk.mode:
            return await reject("mode_gate",
                                f"broker mode {broker.mode!r} != run mode {risk.mode!r}")

        # 4. per-strategy limits — daily-loss breach blocks ENTRIES ONLY;
        #    an exit must never be blocked by the very condition it fixes
        # "pyramid" (a scale-IN add) draws on the same DAILY budgets as an entry
        # and is blocked by the same daily-loss breach, but two of these gates
        # are about OPENING a position and must not apply to growing one that is
        # already open: `max_open_positions` counted it when it opened, and
        # `has_open_entry_order` refers to the original entry.
        if req.purpose in ("entry", "pyramid"):
            if req.purpose == "entry" and \
                    risk.open_position_count() >= risk.max_open_positions:
                return await reject("max_open_positions",
                                    f"{risk.open_position_count()} >= {risk.max_open_positions}")
            if risk.orders_today_count() >= risk.max_daily_orders:
                return await reject("max_daily_orders",
                                    f"{risk.orders_today_count()} >= {risk.max_daily_orders}")
            if req.purpose == "entry" and risk.has_open_entry_order(req.symbol):
                return await reject("duplicate_entry", f"entry already working for {req.symbol}")
            if risk.daily_pnl() <= -risk.max_daily_loss:
                return await reject("max_daily_loss",
                                    f"daily P&L {risk.daily_pnl():.2f} <= -{risk.max_daily_loss}")

        # 4b. SHORT-SPECIFIC GATES (plan/15 §B4).
        #
        # Shorting is not symmetric with buying and each asymmetry gets its own
        # check. Every value is read with a DENYING default, so a RiskContext
        # that predates shorting can never accidentally permit one.
        #
        # The cash-account block is the most important check in this file's
        # short path: a cash account cannot short at all, and without this the
        # intent would be journalled and sent before the broker rejected it.
        if req.side == "sell_short":
            if not getattr(risk, "short_enabled", False):
                return await reject("short_not_enabled",
                                    "this run is not opted into short selling")
            account_type = str(getattr(risk, "account_type", "") or "").upper()
            if account_type != "MARGIN":
                return await reject(
                    "cash_account_cannot_short",
                    f"account type {account_type or 'UNKNOWN'!r} cannot sell short — "
                    "shorting requires a margin account")
            if req.symbol in (getattr(risk, "hard_to_borrow", None) or frozenset()):
                return await reject("hard_to_borrow",
                                    f"{req.symbol} is not borrowable")
            max_short = getattr(risk, "max_short_notional", None)
            if max_short and req.reference_price is not None:
                notional = req.reference_price * req.qty
                if notional > max_short:
                    return await reject("max_short_notional",
                                        f"{notional:.2f} > {max_short}")
        # A cover must never exceed the open short: covering more would buy
        # through zero and leave an UNINTENDED LONG position.
        if req.side == "buy_to_cover":
            short_qty_fn = getattr(risk, "short_qty", None)
            if callable(short_qty_fn):
                open_short = short_qty_fn(req.symbol)
                if req.qty > open_short + 1e-9:
                    return await reject(
                        "cover_exceeds_short",
                        f"cover {req.qty} > open short {open_short} for {req.symbol} — "
                        "would reverse into a long position")

        # 4c. OPTION-SPECIFIC GATES (plan/16 §11.2).
        #
        # Same discipline as 4b: every value is read with a DENYING default, so
        # a RiskContext that predates options cannot accidentally permit one.
        #
        # The most important check in this block is `close_exceeds_position`.
        # Selling more contracts than are held does not merely oversell — it
        # opens a NAKED SHORT OPTION, which is the single thing v1 must never
        # do (plan/16 D3), because it is the one position whose loss is not
        # bounded by the premium and cannot be capped by a resting order.
        if req.is_option:
            if not getattr(risk, "options_enabled", False):
                return await reject("options_not_enabled",
                                    "this run is not opted into options trading")
            if not is_occ(req.symbol):
                return await reject("bad_occ_symbol",
                                    f"{req.symbol!r} is not a valid OCC option symbol")
            try:
                expiry = parse_occ(req.symbol).expiry
            except InvalidOccSymbol as e:
                return await reject("bad_occ_symbol", str(e))
            if expiry < _today_et():
                return await reject("expired_contract",
                                    f"{req.symbol} expired on {expiry.isoformat()}")

            # An option MARKET order on a wide or one-sided book can fill
            # catastrophically far from mid — a well-known way to lose a large
            # fraction of a position in a single fill. Limit orders only, always.
            if req.order_type == "market":
                return await reject("option_market_order",
                                    "option orders must be limit orders; a market "
                                    "order on a wide book can fill far from mid")

            if req.side == "sell_to_open" and not req.legs_are_covered:
                return await reject(
                    "naked_short_option",
                    "uncovered SELL_TO_OPEN is not permitted — v1 trades long "
                    "premium only, which is what bounds the loss at the debit paid")

            if req.side in ("sell_to_close", "buy_to_close"):
                qty_fn = getattr(risk, "contract_qty", None)
                if callable(qty_fn):
                    held = qty_fn(req.symbol)
                    if req.qty > held + 1e-9:
                        return await reject(
                            "close_exceeds_position",
                            f"closing {req.qty} of {req.symbol} but only {held} held "
                            "— would open a naked short option")

            if req.purpose in ("entry", "pyramid"):
                headroom = getattr(risk, "max_premium_at_risk_remaining", 0.0)
                if req.premium_notional > headroom + 1e-9:
                    return await reject(
                        "max_premium_at_risk",
                        f"premium {req.premium_notional:.2f} exceeds remaining "
                        f"budget {headroom:.2f}")
                max_contracts = getattr(risk, "max_contracts_per_position", 0)
                if req.qty > max_contracts:
                    return await reject("max_contracts_per_position",
                                        f"{req.qty} > {max_contracts}")

        # 5. global limits — ALL of these are entry-only (is_protective exempt),
        #    same "exits are never blocked" invariant checks #4 and #7 already
        #    enforce. Originally only global_daily_loss had this exemption;
        #    symbol_allowlist/blocklist and global_max_notional did not, which
        #    meant a resting protective stop/target for an ALREADY-OPEN
        #    position could get rejected by a blocklist/cap change (or, for
        #    max_total_notional, simply by the position's own notional being
        #    counted once by RunManager.total_notional() and then AGAIN by
        #    this order's own qty*price) — the same class of bug already
        #    found twice in this file (the price-sanity band, atr_multiple
        #    stops). Caught here before any real GlobalRiskContext existed to
        #    exercise the path (RunManager is the first one — runtime/manager.py).
        if global_risk is not None and not is_protective:
            if global_risk.symbol_allowlist is not None and \
                    req.symbol not in global_risk.symbol_allowlist:
                return await reject("symbol_allowlist", f"{req.symbol} not in allowlist")
            if req.symbol in global_risk.symbol_blocklist:
                return await reject("symbol_blocklist", f"{req.symbol} is blocklisted")
            if global_risk.total_daily_pnl() <= -global_risk.max_total_daily_loss:
                result = await reject("global_daily_loss", "global daily-loss limit breached")
                await self.activate_kill_switch(actor="system", reason="global_daily_loss_breach")
                return result
            if req.reference_price is not None:
                notional = req.reference_price * req.qty
                if global_risk.total_notional() + notional > global_risk.max_total_notional:
                    return await reject("global_max_notional", "would exceed global max notional")
        if not is_protective and req.reference_price is not None:
            # TWO caps for an option, because premium and exposure are different
            # numbers by a factor of ~100. A 100-lot at $0.50 is $5,000 of
            # premium — under any sane premium cap — but can carry $500,000+ of
            # delta-adjusted underlying exposure. Naming them distinctly in the
            # rejection tells the user WHICH bound bound.
            notional = req.premium_notional
            if notional > self._max_single_order_notional:
                return await reject(
                    "max_single_order_premium" if req.is_option
                    else "max_single_order_notional",
                    f"{notional:.2f} > {self._max_single_order_notional}")
            # The reference price for an option is its PREMIUM, so exposure
            # needs the UNDERLYING's price; when the caller did not supply one
            # the check is skipped rather than computed from the wrong number.
            if req.is_option and req.delta is not None and req.underlying_price:
                exposure = (abs(req.delta) * req.qty * req.multiplier
                            * req.underlying_price)
                if exposure > self._max_single_order_notional:
                    return await reject(
                        "max_single_order_exposure",
                        f"delta-adjusted exposure {exposure:.2f} > "
                        f"{self._max_single_order_notional}")
        if req.purpose in ("entry", "pyramid") and not market_open:
            return await reject("market_hours", "market is closed; entries not allowed")

        # 6. duplicate suppression — any purpose (protects against re-firing
        #    the same exit twice due to a bug, not just entries)
        if await self._is_duplicate(req):
            return await reject("duplicate_suppression",
                                f"identical intent submitted within {DUPLICATE_WINDOW_S}s")

        # 7. price sanity
        if req.order_type == "market":
            if req.reference_price is None or req.reference_price_age_s is None or \
                    req.reference_price_age_s > MAX_QUOTE_AGE_S:
                return await reject("price_sanity", "no fresh quote for market order")
        # The band check exists to catch fat-finger / bad-price bugs on
        # discretionary (entry) orders. It must NOT apply to protective
        # stop/target orders: their price is intentionally far from the
        # current market by design (that's what makes it a stop), derived
        # directly from the strategy's own configured, already-validated risk
        # distance — rejecting a wide, deliberate stop would defeat the exact
        # protection it exists to provide.
        if not is_protective:
            # OPTIONS NEED A DIFFERENT BAND, and this is the third carve-out
            # this check has needed (protective orders, atr_multiple stops, now
            # options). The pattern behind all three: the 5% band assumes a
            # TIGHT TWO-SIDED MARKET, and rejects anything deliberately away
            # from it. An option book is legitimately wide — a marketable limit
            # on a $1.20 contract is routinely 5-15% from mid — so a 5% band
            # would reject most honest option orders.
            #
            # Where a real quote is available the band is measured against the
            # SPREAD, which is the actual measure of what "far from market"
            # means for this instrument; otherwise a wider percentage band is
            # used.
            if req.is_option and req.quote_bid is not None and req.quote_ask is not None:
                spread = max(req.quote_ask - req.quote_bid, 0.01)
                lo = req.quote_bid - 2 * spread
                hi = req.quote_ask + 2 * spread
                for px in (req.limit_px, req.stop_px):
                    if px is not None and not (lo <= px <= hi):
                        return await reject(
                            "price_sanity",
                            f"option price {px} is outside [{lo:.2f}, {hi:.2f}] "
                            f"(bid {req.quote_bid}, ask {req.quote_ask}, "
                            f"2x spread either side)")
            else:
                band_limit = OPTION_PRICE_SANITY_BAND if req.is_option else PRICE_SANITY_BAND
                for px in (req.limit_px, req.stop_px):
                    if px is not None:
                        if req.reference_price is None or req.reference_price <= 0:
                            return await reject("price_sanity",
                                                "no reference price to sanity-check against")
                        band = abs(px - req.reference_price) / req.reference_price
                        if band > band_limit:
                            return await reject("price_sanity",
                                                f"price {px} is {band:.1%} from reference "
                                                f"{req.reference_price} (band {band_limit:.0%})")

        # 8. rate limiter (protective purposes draw from the reserved bucket)
        if not await self._limiter.acquire(req.purpose, req.max_wait_s):
            return await reject("rate_limited", "order rate limit exhausted")

        # --- all checks passed: journal intent BEFORE the broker call ---
        checks.append({"check": "all", "result": "accept"})
        await self._journal_intent(intent_id, req, "PENDING_SUBMIT", checks)
        await self._db.audit("gateway", "order_accepted", {
            "intent_id": intent_id, "run_id": req.run_id, "symbol": req.symbol,
            "purpose": req.purpose, "protective": is_protective})

        try:
            handle = await broker.place(OrderRequest(
                intent_id=intent_id, symbol=req.symbol, side=req.side, qty=req.qty,
                order_type=req.order_type, limit_px=req.limit_px, stop_px=req.stop_px,
                asset_type=req.asset_type, multiplier=req.multiplier,
                underlying=req.underlying))
        except BrokerRejection as e:
            # Broker CERTAINLY never placed it (bad funds/shares/symbol) — safe
            # to mark REJECTED outright, no reconciliation ambiguity. The API
            # responded (just with a rejection), so it's reachable — reset the
            # consecutive-error streak same as a real success would.
            self._consecutive_broker_errors = 0
            await self._update_intent_status(intent_id, "REJECTED")
            await self._db.audit("gateway", "order_rejected_by_broker", {
                "intent_id": intent_id, "run_id": req.run_id, "reason": str(e)})
            log.info("order_rejected_by_broker", intent_id=intent_id, reason=str(e))
            return GatewayResult(False, intent_id, None, "REJECTED", str(e))
        except Exception as e:  # noqa: BLE001 — NEVER blind-retry a possibly-placed order
            # SchwabBroker.place() already converts any <500 status into
            # BrokerRejection above — reaching here means 5xx or a genuinely
            # ambiguous failure (timeout, network). >=3 in a row without a
            # reachable response in between trips the kill switch (plan 06
            # §Automatic kill-switch trips: "≥3 consecutive Schwab 5xx").
            self._consecutive_broker_errors += 1
            await self._update_intent_status(intent_id, "UNKNOWN")
            await self._db.audit("gateway", "order_unknown", {
                "intent_id": intent_id, "run_id": req.run_id, "error": str(e),
                "consecutive_broker_errors": self._consecutive_broker_errors})
            log.error("order_submit_unknown", intent_id=intent_id, error=str(e),
                     consecutive_broker_errors=self._consecutive_broker_errors)
            if self._consecutive_broker_errors >= 3:
                await self.activate_kill_switch(actor="system",
                                                reason="consecutive_broker_errors")
            return GatewayResult(False, intent_id, None, "UNKNOWN", str(e))

        self._consecutive_broker_errors = 0
        await self._record_order(intent_id, req.run_id, broker.mode, handle.broker_order_id,
                                  intent_id, handle.status)
        await self._update_intent_status(intent_id, "SUBMITTED")
        log.info("order_submitted", intent_id=intent_id, broker_order_id=handle.broker_order_id)
        return GatewayResult(True, intent_id, handle.broker_order_id, "SUBMITTED")

    # ---- closing the loop on UNKNOWN orders --------------------------------
    async def resolve_unknown_intent(self, intent_id: str, actor: str, resolution: str) -> None:
        """A human has manually confirmed the true state of an order that was
        left UNKNOWN (broker call raised something ambiguous — 5xx/timeout —
        so we genuinely don't know if it placed). Marks it reviewed so the
        reconciliation sweep's "unresolved UNKNOWN" trip condition stops
        firing on it. Raises if the intent isn't currently UNKNOWN — you
        can't "resolve" something that was never ambiguous or is already
        resolved."""
        row = await self._db.fetch_one(
            "SELECT status FROM order_intents WHERE id=?", (intent_id,))
        if row is None:
            raise ValueError(f"no order_intent {intent_id!r}")
        if row["status"] != "UNKNOWN":
            raise ValueError(
                f"order_intent {intent_id!r} is {row['status']!r}, not UNKNOWN — nothing to resolve")
        await self._db.execute(
            "UPDATE order_intents SET status='RESOLVED_MANUAL' WHERE id=?", (intent_id,))
        await self._db.audit(actor, "unknown_order_resolved",
                             {"intent_id": intent_id, "resolution": resolution})
        log.warning("unknown_order_resolved", intent_id=intent_id, actor=actor,
                   resolution=resolution)

    # ---- journaling --------------------------------------------------------
    async def _journal_intent(self, intent_id: str, req: OrderIntentRequest, status: str,
                              checks: list[dict]) -> None:
        import json
        await self._db.execute(
            """INSERT INTO order_intents (id, run_id, ts, symbol, side, qty, order_type,
               limit_px, stop_px, purpose, status, gateway_decision_json,
               asset_type, underlying, multiplier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (intent_id, req.run_id, int(time.time() * 1000), req.symbol, req.side, req.qty,
             req.order_type, req.limit_px, req.stop_px, req.purpose, status,
             json.dumps(checks), req.asset_type, req.underlying or req.symbol,
             req.multiplier),
        )

    async def _update_intent_status(self, intent_id: str, status: str) -> None:
        await self._db.execute(
            "UPDATE order_intents SET status=? WHERE id=?", (status, intent_id))

    async def _record_order(self, intent_id: str, run_id: str, mode: str,
                            broker_order_id: str, tag: str, status: str) -> None:
        await self._db.execute(
            """INSERT INTO orders (id, intent_id, run_id, mode, schwab_order_id, tag, status,
               submitted_at) VALUES (?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), intent_id, run_id, mode, broker_order_id, tag, status,
             int(time.time() * 1000)),
        )

    async def _is_duplicate(self, req: OrderIntentRequest) -> bool:
        """IDENTICAL intent within the window — same symbol/side/purpose AND
        the same quantity and prices.

        qty/limit_px/stop_px are part of the key because the check must not
        swallow a legitimate RE-SIZE of a resting order. When a `scale_outs`
        partial fills, the runtime cancels the protective stop and re-places
        it at the reduced quantity; without qty in the key that re-place looks
        exactly like a re-fire of the original stop and gets rejected — which
        then trips the "a configured protective order must place" invariant
        and crashes the run to ERROR, leaving the remainder unprotected. The
        bug this check exists to catch (the same order fired twice) is still
        caught: a true re-fire matches on every field."""
        cutoff = int((time.time() - DUPLICATE_WINDOW_S) * 1000)
        row = await self._db.fetch_one(
            """SELECT id FROM order_intents WHERE run_id=? AND symbol=? AND side=? AND
               purpose=? AND qty=? AND ts>=? AND status != 'REJECTED_LOCAL'
               AND limit_px IS ? AND stop_px IS ? LIMIT 1""",
            (req.run_id, req.symbol, req.side, req.purpose, req.qty, cutoff,
             req.limit_px, req.stop_px),
        )
        return row is not None
