"""SimBroker — the mandatory backtest→live gate (docs/plan/07-paper-trading.md).

No Schwab-supported paper route exists (investigated; see plan 07), so this
simulated-fill engine, driven by REAL streaming quotes, is the paper-trading
mechanism. It implements the same `Broker` Protocol as the future
`SchwabBroker` and emits the same `BrokerEvent` shape, so the StrategyRunner
code path is IDENTICAL in paper and live — only the injected Broker differs.

Design notes (deliberate v1 simplifications, documented per plan 07 §Known
fidelity limits):
- No order-book depth: fills always execute in full at the quoted price
  (± slippage), never partial. This is CONSERVATIVE relative to reality
  (real fills can be worse when size exceeds displayed depth), never optimistic.
- Market orders self-schedule their own fill after `sim_latency_ms`, re-querying
  the quote at that time (not the quote at placement) — mirrors real
  order-to-fill latency.
- Limit/stop orders are passive: they sit WORKING until `process_tick()` is
  called (the runtime wires live streamer quotes to this) and fire on a
  strict price cross, never on mere proximity — pessimistic by design.
- OCO/bracket linkage (cancel-sibling-on-fill) is NOT modeled inside SimBroker
  — that composition lives one layer up, in the runtime (same as it will for
  SchwabBroker, which needs the runtime to submit Schwab's native OCO/TRIGGER
  structures). SimBroker only knows about individual orders.
- Positions are SIGNED (plan/15): a long is positive, a short negative. `sell`
  closes a long and `buy_to_cover` closes a short; each rejects cleanly if it
  would exceed the position it is closing, so an over-sized close can never
  silently flip the position to the other side. `sell_short` requires Reg-T
  style initial margin, mirroring the backtest engine so paper and backtest
  agree on what was affordable.
- Cash is RESERVED at placement, not only debited at fill. A strategy that
  signals on several symbols at one bar close submits them back-to-back, and
  every placement check would otherwise see the same full balance — so they all
  pass, then all but the first are rejected at fill time once the winner's debit
  lands. Reserving the (slippage-inclusive) estimated cost at placement makes
  the affordability check see the money already spoken for, so the unaffordable
  orders are declined up front instead of dying downstream.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from ..logging import get_logger
from ..options.symbology import is_occ
from .base import (BrokerEvent, BrokerEventHandler, BrokerOrderHandle, BrokerRejection,
                   Fill, OrderRequest, side_direction)

log = get_logger("sim-broker")

QuoteProvider = Callable[[str], dict | None]  # symbol -> {"bid","ask","last"} or None


@dataclass
class _WorkingOrder:
    req: OrderRequest
    broker_order_id: str
    eligible_at: float          # monotonic time; process_tick ignores orders before this
    fill_task: asyncio.Task | None = None
    # Cash earmarked for this order from placement until it fills/cancels/rejects.
    # Without this, N orders placed in the same instant each pass the placement
    # check against the same undebited balance, and all but the first are
    # rejected at fill time (see class docstring §cash reservation).
    reserved_cash: float = 0.0


@dataclass
class _Fill:
    ts: float
    symbol: str
    side: str
    qty: float
    price: float


class SimBroker:
    mode = "paper"

    def __init__(self, quote_provider: QuoteProvider, starting_cash: float = 100_000,
                sim_latency_ms: float = 250, sim_slippage_bps: float = 1.0,
                short_margin_pct: float = 50.0,
                option_commission_per_contract: float = 0.65,
                option_quote_provider: QuoteProvider | None = None):
        self._quotes = quote_provider
        #: Option quotes come from a DIFFERENT source than equity ones. The
        #: equity provider is the LEVELONE_EQUITIES cache, which has never
        #: heard of an OCC symbol and returns None for every one — so without
        #: this every option order was rejected at placement with "no quote
        #: available". Injected by RunManager for options runs; None otherwise,
        #: and a shares run never asks for it.
        self._option_quotes: QuoteProvider | None = option_quote_provider
        self.cash = starting_cash
        self._positions: dict[str, float] = {}
        self._avg_price: dict[str, float] = {}
        #: contract multiplier per symbol (1.0 for equities). Set on fill
        #: and on seed_position, so equity marking never has to guess.
        self._multiplier: dict[str, float] = {}
        self._working: dict[str, _WorkingOrder] = {}   # broker_order_id -> order
        self._fills: list[_Fill] = []
        self._latency_s = sim_latency_ms / 1000.0
        self._slippage = sim_slippage_bps / 10_000.0
        # Reg-T style initial margin for shorts, mirroring the backtest engine
        # so paper results match a backtest of the same strategy
        self._short_margin_pct = short_margin_pct
        # Schwab charges $0.65/contract on options and nothing on equities, so
        # a paper options run that ignored it would overstate results by a
        # material margin at small position sizes.
        self._option_commission = option_commission_per_contract
        self._handlers: list[BrokerEventHandler] = []

    def set_option_quote_provider(self, provider: QuoteProvider | None) -> None:
        """Injected after construction: the provider is the run's OptionsLive,
        which does not exist until the runner is built (RunManager._activate)."""
        self._option_quotes = provider

    def _quote_for(self, symbol: str) -> dict | None:
        """The one place a symbol is routed to its quote source. Every quote
        lookup in this class goes through here, so an OCC symbol can never
        silently fall through to the equity cache and read as 'no market'."""
        if is_occ(symbol) and self._option_quotes is not None:
            return self._option_quotes(symbol)
        # No option provider injected: fall through to the single quote
        # provider, which is what a standalone SimBroker (and every test that
        # builds one) uses to answer for ALL symbols. In the real runtime that
        # provider is the equity cache and returns None for an OCC symbol —
        # the same "no usable market" answer, so the fallback is inert there.
        return self._quotes(symbol)

    def subscribe_events(self, handler: BrokerEventHandler) -> None:
        self._handlers.append(handler)

    def working_order_ids(self) -> list[str]:
        return list(self._working.keys())

    def seed_position(self, symbol: str, qty: float, avg_price: float,
                      multiplier: float = 1.0) -> None:
        """Recovery-only (app/runtime/recovery.py): seed a position this
        SimBroker instance never actually filled — the instance that did is
        gone, along with the crashed process. Book-keeping only; cash is NOT
        adjusted here (recovery.py's caller passes a `starting_cash` to the
        constructor that already accounts for this position's cost basis
        having left the till, and for all realized P&L to date — see its
        `_reconstruct_cash` derivation)."""
        self._positions[symbol] = qty
        self._avg_price[symbol] = avg_price
        self._multiplier[symbol] = multiplier

    async def _emit(self, event: BrokerEvent) -> None:
        for h in self._handlers:
            try:
                await h(event)
            except Exception as e:  # noqa: BLE001 — one bad handler can't break the sim
                log.error("sim_event_handler_error", error=str(e))

    # ---- cash reservation ---------------------------------------------
    @property
    def reserved_cash(self) -> float:
        return sum(o.reserved_cash for o in self._working.values())

    @property
    def available_cash(self) -> float:
        """Cash not already earmarked by a working order."""
        return self.cash - self.reserved_cash

    # ---- placement ----------------------------------------------------
    async def place(self, req: OrderRequest) -> BrokerOrderHandle:
        reserve = 0.0
        if req.is_option:
            reserve = await self._check_option_order(req)
        elif req.side == "sell":
            have = self._positions.get(req.symbol, 0.0)
            if req.qty > have + 1e-9:
                raise BrokerRejection(
                    f"insufficient position to sell {req.qty} {req.symbol} (have {have})")
        elif req.side == "buy_to_cover":
            # positions are SIGNED: a short is negative. Covering more than is
            # short would flip the position long, which is never intended.
            short_qty = -min(self._positions.get(req.symbol, 0.0), 0.0)
            if req.qty > short_qty + 1e-9:
                raise BrokerRejection(
                    f"insufficient short position to cover {req.qty} {req.symbol} "
                    f"(short {short_qty})")
        elif req.side == "sell_short":
            quote = self._quote_for(req.symbol)
            if quote is None:
                raise BrokerRejection(f"no quote available for {req.symbol}")
            est_price = quote.get("bid") or quote.get("last")
            # Reg-T style: a short needs initial margin beyond the sale proceeds
            if est_price:
                margin = req.qty * est_price * self._short_margin_pct / 100
                if margin > self.available_cash:
                    raise BrokerRejection(
                        f"insufficient margin to short {req.qty} {req.symbol}")
                reserve = margin
        elif req.side == "buy":
            quote = self._quote_for(req.symbol)
            if quote is None:
                raise BrokerRejection(f"no quote available for {req.symbol}")
            est_price = quote.get("ask") or quote.get("last")
            if est_price:
                # match what _execute_fill will actually debit: the fill re-quotes
                # and adds slippage, so budget for it here rather than passing the
                # check by pennies and being rejected at fill time
                need = req.qty * est_price * (1 + self._slippage)
                if need > self.available_cash:
                    raise BrokerRejection(
                        f"insufficient cash: need ~{need:.2f}, "
                        f"have {self.available_cash:.2f}")
                reserve = need

        broker_order_id = str(uuid.uuid4())
        order = _WorkingOrder(req=req, broker_order_id=broker_order_id,
                              eligible_at=time.monotonic() + self._latency_s,
                              reserved_cash=reserve)
        self._working[broker_order_id] = order
        await self._emit(BrokerEvent("accepted", broker_order_id, req.intent_id))

        if req.order_type == "market":
            order.fill_task = asyncio.create_task(self._fill_market_after_latency(order))
        # limit/stop/stop_limit/trailing_stop: passive, wait for process_tick()
        return BrokerOrderHandle(broker_order_id, "WORKING")

    async def _check_option_order(self, req: OrderRequest) -> float:
        """Validate an option order and return the cash to reserve.

        Options in a PAPER run are priced from real streamed quotes (the runner
        injects a live pricer), so the simulation here is of EXECUTION, not of
        price — which is what makes a paper soak meaningful evidence rather
        than a second copy of the backtest.

        The `sell_to_close` bound is the important one: selling more contracts
        than are held would open a NAKED SHORT option, which plan/16 D3 forbids
        outright. The gateway checks this too; both exist because the
        consequence is unbounded risk, not a bad fill."""
        held = self._positions.get(req.symbol, 0.0)
        if req.side == "sell_to_close":
            if req.qty > held + 1e-9:
                raise BrokerRejection(
                    f"insufficient contracts to close {req.qty} {req.symbol} "
                    f"(hold {held}) — would open a naked short option")
            return 0.0
        if req.side == "buy_to_open":
            quote = self._quote_for(req.symbol)
            if quote is None:
                raise BrokerRejection(f"no quote available for {req.symbol}")
            est = quote.get("ask") or quote.get("last")
            if not est:
                raise BrokerRejection(f"no usable ask for {req.symbol}")
            need = req.qty * est * req.multiplier * (1 + self._slippage)
            if need > self.available_cash:
                raise BrokerRejection(
                    f"insufficient cash for {req.qty} contracts of {req.symbol}: "
                    f"need ~{need:.2f}, have {self.available_cash:.2f}")
            return need
        raise BrokerRejection(
            f"option side {req.side!r} is not permitted — v1 trades long premium "
            "only (plan/16 D3)")

    async def cancel(self, broker_order_id: str) -> None:
        order = self._working.pop(broker_order_id, None)
        if order is None:
            log.warning("sim_cancel_unknown_or_already_terminal", broker_order_id=broker_order_id)
            return
        if order.fill_task and not order.fill_task.done():
            order.fill_task.cancel()
        await self._emit(BrokerEvent("canceled", broker_order_id, order.req.intent_id))

    async def replace(self, broker_order_id: str, req: OrderRequest) -> BrokerOrderHandle:
        await self.cancel(broker_order_id)
        return await self.place(req)

    async def get_positions(self) -> dict[str, float]:
        return dict(self._positions)

    async def get_account(self) -> dict:
        equity = self.cash
        for sym, qty in self._positions.items():
            quote = self._quote_for(sym)
            px = (quote.get("last") or quote.get("bid")) if quote else self._avg_price.get(sym, 0)
            # An option position is worth qty x price x MULTIPLIER. Omitting the
            # multiplier here would understate paper equity by 100x — and
            # equity feeds position sizing, so the error compounds into every
            # subsequent order rather than staying a display bug.
            equity += qty * (px or 0) * self._multiplier.get(sym, 1.0)
        return {"cash": self.cash, "equity": equity, "buying_power": self.available_cash,
                "positions": dict(self._positions)}

    # ---- fill execution -------------------------------------------------
    async def _fill_market_after_latency(self, order: _WorkingOrder) -> None:
        try:
            await asyncio.sleep(self._latency_s)
        except asyncio.CancelledError:
            return
        if order.broker_order_id not in self._working:
            return  # canceled while waiting
        quote = self._quote_for(order.req.symbol)
        if quote is None:
            await self._reject_working(order, "no quote available at fill time")
            return
        # Whether this order BUYS is derived from side_direction, not from a
        # hardcoded tuple. The tuple version silently excluded `buy_to_open`,
        # so option purchases filled at the BID with a FAVOURABLE slippage
        # adjustment — free money on every entry. Deriving it means a future
        # side is handled correctly by construction.
        buys = side_direction(order.req.side) > 0
        raw_px = quote.get("ask") if buys else quote.get("bid")
        if raw_px is None:
            raw_px = quote.get("last")
        if raw_px is None:
            await self._reject_working(order, "no usable price at fill time")
            return
        px = raw_px * (1 + self._slippage) if buys else raw_px * (1 - self._slippage)
        await self._execute_fill(order, px)

    async def process_tick(self, symbol: str, bid: float | None, ask: float | None,
                           last: float | None) -> None:
        """Called by the runtime for every live quote update. Evaluates
        resting limit/stop orders for `symbol` against the new tick."""
        now = time.monotonic()
        for order in [o for o in self._working.values()
                     if o.req.symbol == symbol and now >= o.eligible_at]:
            req = order.req
            # Direction is DERIVED, so option sides (buy_to_open /
            # sell_to_close) are handled by construction. Matching on hardcoded
            # side tuples left resting option orders permanently unfillable —
            # which for a protective stop means silently unprotected.
            buys = side_direction(req.side) > 0
            if req.order_type == "limit":
                if buys and ask is not None and ask <= (req.limit_px or 0):
                    await self._execute_fill(order, req.limit_px)
                elif not buys and bid is not None \
                        and bid >= (req.limit_px or float("inf")):
                    await self._execute_fill(order, req.limit_px)
            elif req.order_type == "stop":
                # A protective stop on a LONG (shares or options) is a SELL
                # triggering on weakness; a short's is a BUY_TO_COVER
                # triggering on STRENGTH.
                if not buys and last is not None \
                        and last <= (req.stop_px or float("-inf")):
                    fill_px = (bid or last) * (1 - self._slippage)
                    await self._execute_fill(order, fill_px)
                elif buys and req.side == "buy_to_cover" and last is not None \
                        and last >= (req.stop_px or float("inf")):
                    fill_px = (ask or last) * (1 + self._slippage)
                    await self._execute_fill(order, fill_px)

    async def process_option_ticks(self) -> None:
        """Evaluate every resting OPTION order against its current quote.

        Equity orders are driven by `process_tick`, which the runtime calls
        from the LEVELONE_EQUITIES stream. No such tick ever arrives for an OCC
        symbol on that stream, so an option limit order — including a
        protective stop, where the consequence of never firing is an unmanaged
        position — would rest forever. The option quote provider is pull-based
        (stream first, cached chain second), so here the caller supplies the
        CLOCK and the provider supplies the price."""
        if self._option_quotes is None:
            return
        symbols = {o.req.symbol for o in self._working.values() if is_occ(o.req.symbol)}
        for sym in symbols:
            q = self._quote_for(sym)
            if q is None:
                continue        # no usable market: leave the order resting
            await self.process_tick(sym, q.get("bid"), q.get("ask"),
                                    q.get("last") or q.get("mid"))

    async def _reject_working(self, order: _WorkingOrder, reason: str) -> None:
        self._working.pop(order.broker_order_id, None)
        await self._emit(BrokerEvent("rejected", order.broker_order_id, order.req.intent_id,
                                     reason=reason))

    async def _execute_fill(self, order: _WorkingOrder, price: float) -> None:
        req = order.req
        if req.is_option:
            await self._execute_option_fill(order, price)
            return
        if req.side == "buy":
            # this order's own reservation is what it is about to spend, so only
            # OTHER working orders' reservations are unavailable to it
            spendable = self.cash - (self.reserved_cash - order.reserved_cash)
            if req.qty * price > spendable + 1e-6:
                await self._reject_working(order, "insufficient cash at fill time")
                return
        self._working.pop(order.broker_order_id, None)
        now = time.time()
        if req.side == "buy":
            self.cash -= req.qty * price
            prev_qty = self._positions.get(req.symbol, 0.0)
            prev_avg = self._avg_price.get(req.symbol, 0.0)
            new_qty = prev_qty + req.qty
            self._avg_price[req.symbol] = (
                (prev_qty * prev_avg + req.qty * price) / new_qty if new_qty else 0.0)
            self._positions[req.symbol] = new_qty
        elif req.side == "sell_short":
            # proceeds land in cash; the position goes NEGATIVE (signed), which
            # is what makes get_account()'s equity subtract the liability
            self.cash += req.qty * price
            prev_qty = self._positions.get(req.symbol, 0.0)
            prev_avg = self._avg_price.get(req.symbol, 0.0)
            new_qty = prev_qty - req.qty
            self._avg_price[req.symbol] = (
                (abs(prev_qty) * prev_avg + req.qty * price) / abs(new_qty) if new_qty else 0.0)
            self._positions[req.symbol] = new_qty
        elif req.side == "buy_to_cover":
            self.cash -= req.qty * price
            remaining = self._positions.get(req.symbol, 0.0) + req.qty
            if remaining >= -1e-9:
                self._positions.pop(req.symbol, None)
                self._avg_price.pop(req.symbol, None)
            else:
                self._positions[req.symbol] = remaining
        else:   # plain sell (closing a long)
            self.cash += req.qty * price
            remaining = self._positions.get(req.symbol, 0.0) - req.qty
            if remaining <= 1e-9:
                self._positions.pop(req.symbol, None)
                self._avg_price.pop(req.symbol, None)
                self._multiplier.pop(req.symbol, None)
            else:
                self._positions[req.symbol] = remaining
        self._fills.append(_Fill(now, req.symbol, req.side, req.qty, price))
        await self._emit(BrokerEvent(
            "fill", order.broker_order_id, req.intent_id,
            fill=Fill(order.broker_order_id, now, req.qty, price)))

    async def _execute_option_fill(self, order: _WorkingOrder, price: float) -> None:
        """Fill an option order.

        `price` is PER CONTRACT; cash moves by price x qty x multiplier. That
        multiplier is the whole reason this is a separate method rather than a
        branch — folding it into the equity path would put a x100 inside
        expressions that must never have one.

        Positions are held in CONTRACTS keyed by the OCC symbol, always
        positive: v1 is long-premium only, so there is no signed-quantity case
        to model."""
        req = order.req
        mult = req.multiplier or 100.0
        cost = req.qty * price * mult
        if req.side == "buy_to_open":
            spendable = self.cash - (self.reserved_cash - order.reserved_cash)
            if cost > spendable + 1e-6:
                await self._reject_working(order, "insufficient cash at fill time")
                return
        self._working.pop(order.broker_order_id, None)
        now = time.time()
        if req.side == "buy_to_open":
            self.cash -= cost + self._option_commission * req.qty
            self._multiplier[req.symbol] = mult
            prev_qty = self._positions.get(req.symbol, 0.0)
            prev_avg = self._avg_price.get(req.symbol, 0.0)
            new_qty = prev_qty + req.qty
            self._avg_price[req.symbol] = (
                (prev_qty * prev_avg + req.qty * price) / new_qty if new_qty else 0.0)
            self._positions[req.symbol] = new_qty
        else:   # sell_to_close
            self.cash += cost - self._option_commission * req.qty
            remaining = self._positions.get(req.symbol, 0.0) - req.qty
            if remaining <= 1e-9:
                self._positions.pop(req.symbol, None)
                self._avg_price.pop(req.symbol, None)
                self._multiplier.pop(req.symbol, None)
            else:
                self._positions[req.symbol] = remaining
        self._fills.append(_Fill(now, req.symbol, req.side, req.qty, price))
        await self._emit(BrokerEvent(
            "fill", order.broker_order_id, req.intent_id,
            fill=Fill(order.broker_order_id, now, req.qty, price)))
