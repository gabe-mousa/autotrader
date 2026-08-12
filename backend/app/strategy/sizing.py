"""Shared position-sizing logic — used by BOTH the backtest engine and the
live/paper runtime, so a strategy's sizing behavior can never diverge between
the two (docs/plan/05, plan/02 §sizing types, decision log #5 cash-behavior).

Options get a SEPARATE function (`compute_contract_qty`) rather than an
overload of the share path. The units differ (contracts, not shares), the
multiplier is not 1, and — the reason it matters most — the failure modes
differ: for a long option the maximum loss is known exactly at entry, which
makes risk-based sizing simpler and safer than it is for shares, while the
delta-adjusted exposure is 100x larger than the premium and needs its own cap.
Folding both into one function would hide both facts."""

from __future__ import annotations

from dataclasses import dataclass

from .schema import OptionRisk, Sizing


def compute_position_size(sizing: Sizing, entry_px: float, stop_px: float | None,
                          equity: float, available_cash: float,
                          commission_per_order: float = 0.0) -> int:
    """Whole-share quantity, rounded down to `round_lot`, capped by
    `max_position_notional` and never exceeding `available_cash`
    (cash-behavior — no margin/leverage in v1). `commission_per_order` lets
    the backtest engine reserve room for a modeled commission; live/paper
    callers pass the default 0.0 (Schwab equities are commission-free)."""
    if entry_px <= 0:
        return 0
    if sizing.type == "fixed_shares":
        qty = int(sizing.value)
        qty = min(qty, int(sizing.max_position_notional // entry_px))
    else:
        if sizing.type == "fixed_dollars":
            dollars = sizing.value
        elif sizing.type == "percent_of_equity":
            dollars = equity * sizing.value / 100
        else:  # risk_based
            risk_dollars = equity * sizing.value / 100
            # DISTANCE, not signed difference: a short's protective stop sits
            # ABOVE the entry, so `entry_px - stop_px` is negative there and the
            # guard below silently returned qty=0 — risk_based sizing could
            # never open a short position at all, in backtest OR live.
            dist = abs(entry_px - stop_px) if stop_px else None
            if not dist or dist <= 0:
                return 0
            dollars = (risk_dollars / dist) * entry_px
        dollars = min(dollars, sizing.max_position_notional, available_cash)
        qty = int(dollars // entry_px)
    lot = sizing.round_lot
    qty = (qty // lot) * lot
    while qty > 0 and qty * entry_px + commission_per_order > available_cash:
        qty -= lot
    return max(qty, 0)


# ---- options (docs/plan/16-options-trading.md §7.2) -----------------------

@dataclass(frozen=True)
class ContractSizing:
    """The sizing decision, WITH the reason it landed where it did.

    Returning a bare 0 is what makes "my strategy takes no trades" an
    afternoon of debugging — the equity research campaigns hit exactly that
    with the highest/donchian current-bar trap (`research/README.md`). So the
    binding constraint is always named."""

    qty: int
    bound_by: str          # which cap decided the answer
    premium: float = 0.0   # total debit if this order fills
    exposure: float = 0.0  # delta-adjusted underlying notional

    def __bool__(self) -> bool:
        return self.qty > 0


#: Names for `bound_by`, so callers can branch/report without string-matching.
BOUND_SIZING = "sizing_rule"
BOUND_MAX_POSITION_NOTIONAL = "sizing.max_position_notional"
BOUND_PREMIUM_PER_TRADE = "risk.options.max_premium_per_trade_pct"
BOUND_PREMIUM_AT_RISK = "risk.options.max_premium_at_risk"
BOUND_MAX_CONTRACTS = "risk.options.max_contracts_per_position"
BOUND_CASH = "available_cash"
BOUND_PREMIUM_INVALID = "premium_invalid"


def compute_contract_qty(sizing: Sizing, opt_risk: OptionRisk,
                         premium_per_contract: float, multiplier: float,
                         delta: float, underlying_px: float,
                         equity: float, available_cash: float,
                         premium_at_risk_open: float = 0.0,
                         commission_per_contract: float = 0.65) -> ContractSizing:
    """Whole contracts for a LONG option position.

    Every cap below applies simultaneously and the MINIMUM wins; `bound_by`
    names whichever one actually bound.

    Two things make this different from share sizing:

      * MAX LOSS IS KNOWN AT ENTRY. A long option cannot lose more than its
        premium, so `risk_based` sizing needs no stop distance — it divides the
        risk budget by the premium itself. That is both simpler and stricter
        than the share path, where the stop might gap through.

      * PREMIUM AND EXPOSURE ARE DIFFERENT NUMBERS, by a factor of ~100. Ten
        SPY 35-delta calls cost ~$3,000 of premium but carry ~$210,000 of
        delta-adjusted underlying exposure. `max_position_notional` caps the
        exposure; the premium caps bound what can actually be lost. A system
        that checked only one would be blind to the other.
    """
    if premium_per_contract <= 0 or multiplier <= 0:
        return ContractSizing(0, BOUND_PREMIUM_INVALID)

    cost_per_contract = premium_per_contract * multiplier
    bound = BOUND_SIZING

    # 1. the strategy's own sizing rule, expressed in PREMIUM dollars
    if sizing.type == "fixed_shares":
        qty = int(sizing.value)
    else:
        if sizing.type == "fixed_dollars":
            dollars = sizing.value
        elif sizing.type == "percent_of_equity":
            dollars = equity * sizing.value / 100.0
        else:  # risk_based — the risk IS the premium, no stop distance needed
            dollars = equity * sizing.value / 100.0
        qty = int(dollars // cost_per_contract)

    # 2. premium per trade, as a share of equity
    cap = equity * opt_risk.max_premium_per_trade_pct / 100.0
    n = int(cap // cost_per_contract)
    if n < qty:
        qty, bound = n, BOUND_PREMIUM_PER_TRADE

    # 3. portfolio premium at risk (total open debit)
    remaining = max(opt_risk.max_premium_at_risk - premium_at_risk_open, 0.0)
    n = int(remaining // cost_per_contract)
    if n < qty:
        qty, bound = n, BOUND_PREMIUM_AT_RISK

    # 4. delta-adjusted UNDERLYING exposure (see docstring)
    exposure_per_contract = abs(delta) * multiplier * underlying_px
    if exposure_per_contract > 0:
        n = int(sizing.max_position_notional // exposure_per_contract)
        if n < qty:
            qty, bound = n, BOUND_MAX_POSITION_NOTIONAL

    # 5. hard integer backstop
    if opt_risk.max_contracts_per_position < qty:
        qty, bound = opt_risk.max_contracts_per_position, BOUND_MAX_CONTRACTS

    # 6. cash, including commission
    while qty > 0 and qty * cost_per_contract + qty * commission_per_contract > available_cash:
        qty -= 1
        bound = BOUND_CASH

    qty = max(qty, 0)
    return ContractSizing(
        qty=qty, bound_by=bound if qty > 0 else bound,
        premium=qty * cost_per_contract,
        exposure=qty * exposure_per_contract)
