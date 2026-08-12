"""Shared broker interface (docs/plan/01 §Process model, plan/06 §Mode gate).

SimBroker (Phase 5) and SchwabBroker (Phase 5.5/6) implement this identically
so runtime code — StrategyRunner, OrderGateway — never knows or cares which
mode it's talking to. The mode gate safety property comes from WHICH class is
constructed for a given run (paper runs are only ever given a SimBroker
instance), not from a runtime type check deep in the call stack."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, Protocol

# Four values rather than a (direction, open/close) pair because brokers model
# it this way: Schwab needs SELL_SHORT vs SELL and BUY_TO_COVER vs BUY as
# distinct instructions, and sending the wrong one is the expensive failure —
# a "sell" against no long position can open an unintended short, and a "buy"
# against an open short can leave you accidentally long.
OrderSide = Literal[
    # equities
    "buy", "sell", "sell_short", "buy_to_cover",
    # options — Schwab uses a DISJOINT instruction vocabulary for them, and
    # sending an equity instruction on an option leg is rejected outright
    # (schwab/trader-api--individual/README.md lines 373-382). Long positions
    # use buy_to_open/sell_to_close; the *_to_open short pair exists only for
    # the covered short leg of a debit spread (plan/16 D4) and is refused by
    # the gateway anywhere else.
    "buy_to_open", "sell_to_close", "sell_to_open", "buy_to_close",
]

AssetType = Literal["EQUITY", "OPTION"]

#: which sides OPEN vs CLOSE exposure, and in which direction
OPENING_SIDES = frozenset({"buy", "sell_short", "buy_to_open", "sell_to_open"})
CLOSING_SIDES = frozenset({"sell", "buy_to_cover", "sell_to_close", "buy_to_close"})
SHORT_SIDES = frozenset({"sell_short", "buy_to_cover"})
#: sides that are only valid on an OPTION leg, and never on an equity one
OPTION_SIDES = frozenset({"buy_to_open", "sell_to_close",
                          "sell_to_open", "buy_to_close"})
#: sides that are only valid on an EQUITY leg
EQUITY_SIDES = frozenset({"buy", "sell", "sell_short", "buy_to_cover"})


def side_direction(side: str) -> int:
    """+1 if the order increases the position, -1 if it decreases it.

    For an option "position" means contracts held long: buy_to_open adds,
    sell_to_close removes."""
    return 1 if side in ("buy", "buy_to_cover", "buy_to_open", "buy_to_close") else -1


def is_option_side(side: str) -> bool:
    return side in OPTION_SIDES


def valid_side_for(side: str, asset_type: str) -> bool:
    """Schwab's instruction/asset-type matrix, encoded.

    Enforced locally because the failure is expensive and asymmetric: an
    equity instruction on an option leg is cleanly rejected by Schwab, but the
    reverse combination is the kind of thing that produces a position nobody
    intended."""
    return side in (OPTION_SIDES if asset_type == "OPTION" else EQUITY_SIDES)
OrderType = Literal["market", "limit", "stop", "stop_limit", "trailing_stop"]
BrokerMode = Literal["paper", "live"]


class BrokerRejection(Exception):
    """A broker CLEANLY declined the order (insufficient funds/shares, invalid
    symbol, etc.) — the order definitely was never placed. Distinct from a
    generic exception (network error, timeout), which leaves the outcome
    genuinely unknown; the gateway marks this REJECTED (safe, certain) vs
    UNKNOWN (never retried, needs reconciliation) for any other exception."""


@dataclass
class OrderLeg:
    """One leg of a multi-leg order (plan/16 §10.1, v1.1 debit spreads)."""
    symbol: str
    side: OrderSide
    qty: float
    asset_type: AssetType = "EQUITY"


@dataclass
class OrderRequest:
    """Broker-facing order request. `tag` is the correlation string stamped
    into the broker order (Schwab's `tag` field / SimBroker's internal tag) —
    always the gateway-issued intent_id, so fills/status can be matched back
    to the intent journal without ambiguity.

    Every options field below defaults to its equity value, so an existing
    construction is byte-identical in meaning."""

    intent_id: str
    symbol: str
    side: OrderSide
    qty: float
    order_type: OrderType
    limit_px: float | None = None
    stop_px: float | None = None
    tag: str = ""
    # ---- options (all inert for equities) ----
    asset_type: AssetType = "EQUITY"
    #: 100 for a standard option, 1 for shares. Carried rather than assumed:
    #: mini and adjusted contracts exist and differ.
    multiplier: float = 1.0
    #: the underlying ticker, for risk aggregation and logging. For an equity
    #: order this is the symbol itself.
    underlying: str | None = None
    #: multi-leg detail. When set, `symbol`/`side`/`qty` describe the NET order
    #: and the legs carry the specifics.
    legs: list[OrderLeg] | None = None
    net_price_type: Literal["NET_DEBIT", "NET_CREDIT", "NET_ZERO"] | None = None
    #: Schwab's `complexOrderStrategyType` for a multi-leg order (VERTICAL,
    #: STRADDLE, ...). Ignored for single-leg orders.
    complex_strategy: str | None = None

    def __post_init__(self) -> None:
        if not self.tag:
            self.tag = self.intent_id
        if self.underlying is None:
            self.underlying = self.symbol

    @property
    def is_option(self) -> bool:
        return self.asset_type == "OPTION"

    @property
    def notional_multiplier(self) -> float:
        """What `qty x price` must be multiplied by to get dollars. The single
        easiest thing to forget in this codebase and the most expensive."""
        return self.multiplier if self.is_option else 1.0


@dataclass
class BrokerOrderHandle:
    broker_order_id: str
    status: str  # WORKING | FILLED | CANCELED | REJECTED | ...


@dataclass
class Fill:
    broker_order_id: str
    ts: float
    qty: float
    price: float
    fees: float = 0.0


@dataclass
class BrokerEvent:
    """Uniform lifecycle event — consumed by StrategyRunner the same way
    regardless of which broker emitted it."""

    kind: Literal["accepted", "fill", "canceled", "rejected"]
    broker_order_id: str
    intent_id: str
    fill: Fill | None = None
    reason: str | None = None
    ts: float = field(default_factory=time.time)


BrokerEventHandler = Callable[[BrokerEvent], Awaitable[None]]


class Broker(Protocol):
    mode: BrokerMode

    async def place(self, req: OrderRequest) -> BrokerOrderHandle: ...
    async def cancel(self, broker_order_id: str) -> None: ...
    async def replace(self, broker_order_id: str, req: OrderRequest) -> BrokerOrderHandle: ...
    async def get_positions(self) -> dict[str, float]: ...
    async def get_account(self) -> dict: ...

    def subscribe_events(self, handler: BrokerEventHandler) -> None:
        """Register a callback invoked for every BrokerEvent this broker emits."""
        ...
