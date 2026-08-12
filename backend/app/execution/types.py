"""Gateway-facing types. Distinct from app.broker.base's OrderRequest (which is
what the gateway hands to a Broker AFTER all checks pass) — OrderIntentRequest
carries the extra context (run_id, purpose, risk/price references) the safety
stack needs but a broker doesn't."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ..broker.base import BrokerMode, OrderSide, OrderType

# "pyramid" is a scale-IN add to a position that is already open (audit A6).
# It is a discretionary order like "entry" and deliberately NOT protective, but
# it is a distinct purpose because it must escape exactly one of the entry gates
# — see gateway.py's check 4.
Purpose = Literal["entry", "pyramid", "exit", "stop", "target", "scale_out",
                  "cancel", "replace"]


@dataclass
class OrderIntentRequest:
    run_id: str
    symbol: str
    side: OrderSide
    qty: float
    order_type: OrderType
    purpose: Purpose
    limit_px: float | None = None
    stop_px: float | None = None
    reference_price: float | None = None       # last-known price, for sanity checks
    reference_price_age_s: float | None = None  # how stale reference_price is
    max_wait_s: float = 5.0                     # rate-limiter acquire timeout
    # ---- options (plan/16 §11.2). All default to their equity values, so an
    # existing construction is unchanged in meaning. ----
    asset_type: Literal["EQUITY", "OPTION"] = "EQUITY"
    multiplier: float = 1.0
    underlying: str | None = None
    #: |delta| of the contract, used for the delta-adjusted exposure cap.
    #: Premium and exposure differ by ~100x, and a system that capped only one
    #: would be blind to the other.
    delta: float | None = None
    #: quoted bid/ask at submission — the option price-sanity band is measured
    #: against the SPREAD, because a marketable limit on a wide option book is
    #: legitimately far from mid
    quote_bid: float | None = None
    quote_ask: float | None = None
    #: the UNDERLYING's price. `reference_price` for an option is its premium,
    #: so delta-adjusted exposure cannot be computed without this. When absent
    #: the exposure check is SKIPPED rather than computed from the wrong price.
    underlying_price: float | None = None
    #: multi-leg detail, when this intent is a spread. A `sell_to_open` leg is
    #: only permitted as part of a structure that covers it.
    legs: list | None = None

    @property
    def is_option(self) -> bool:
        return self.asset_type == "OPTION"

    @property
    def premium_notional(self) -> float:
        """Dollars actually at risk: qty x price x multiplier."""
        px = self.reference_price or self.limit_px or 0.0
        return abs(px * self.qty * (self.multiplier if self.is_option else 1.0))

    @property
    def legs_are_covered(self) -> bool:
        """Is every short leg protected by a long leg in the SAME order?

        This is what separates a defined-risk debit spread from a naked short.
        A bare `sell_to_open` — no legs at all — is never covered, which is the
        default and the safe answer.

        v1.1 will make this meaningful; today it exists so the gateway's
        naked-short check is written against the real condition rather than
        against 'legs is None', and so enabling spreads later cannot
        accidentally open the gate wider than intended."""
        if not self.legs:
            return False
        shorts = [leg for leg in self.legs if getattr(leg, "side", "") == "sell_to_open"]
        longs = [leg for leg in self.legs if getattr(leg, "side", "") == "buy_to_open"]
        if not shorts:
            return True
        # every short contract must be matched by a long one of at least equal
        # quantity — otherwise part of the position is genuinely uncovered
        return bool(longs) and sum(leg.qty for leg in longs) >= sum(
            leg.qty for leg in shorts)


@dataclass
class GatewayResult:
    accepted: bool
    intent_id: str
    broker_order_id: str | None
    status: str          # REJECTED_LOCAL | SUBMITTED | UNKNOWN
    reason: str | None = None


class RiskContext(Protocol):
    """Per-run state the gateway consults for checks 3-4. Implemented by
    RunRiskTracker (owned by StrategyRunner, step 5) — kept as a Protocol so
    the gateway is fully unit-testable before the runtime exists."""

    run_id: str
    mode: BrokerMode
    max_open_positions: int
    max_daily_orders: int
    max_daily_loss: float

    def open_position_count(self) -> int: ...
    def orders_today_count(self) -> int: ...
    def daily_pnl(self) -> float: ...  # negative = losing
    def has_open_entry_order(self, symbol: str) -> bool: ...

    # ---- short-selling context (plan/15 §B4) -------------------------------
    # Read via getattr with SAFE defaults in the gateway, so an implementation
    # that predates shorting simply cannot short rather than accidentally
    # being permitted to. Every default denies.
    #
    #   short_enabled: bool                 strategy opted into shorting
    #   account_type: str                   "MARGIN" | "CASH" — cash cannot short
    #   hard_to_borrow: frozenset[str]      names that must never be shorted
    #   max_short_notional: float           per-run cap
    #   def short_qty(symbol) -> float      current short size, for cover clamp

    # ---- options context (plan/16 §11.2) ------------------------------------
    # Read the same way, with the same discipline: EVERY default denies, so a
    # RiskContext written before options existed cannot accidentally permit an
    # option order. This is the pattern that made the shorting gates safe and
    # it is repeated deliberately.
    #
    #   options_enabled: bool                       run opted into options
    #   max_premium_at_risk_remaining: float        headroom on total open debit
    #   max_contracts_per_position: int             hard per-order cap
    #   max_portfolio_delta: float | None           delta-shares cap
    #   max_portfolio_vega: float | None            $/vol-point cap
    #   def contract_qty(symbol) -> float           held contracts, for the
    #                                               close clamp — selling more
    #                                               than held opens a NAKED
    #                                               SHORT option, which is the
    #                                               one thing v1 must never do
    #   def portfolio_delta() -> float
    #   def portfolio_vega() -> float


class GlobalRiskContext(Protocol):
    """Cross-run aggregation for check 5. Optional — omitted checks default
    permissive (RunManager, step 6, will supply the real implementation)."""

    max_total_notional: float
    max_total_daily_loss: float
    symbol_blocklist: frozenset[str]
    symbol_allowlist: frozenset[str] | None  # None = all symbols allowed

    def total_notional(self) -> float: ...
    def total_daily_pnl(self) -> float: ...
