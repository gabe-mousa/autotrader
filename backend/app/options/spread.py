"""Bid/ask spread and the fill convention.

Costs dominate options results far more than they dominate equity results, and
getting this wrong is the second-most-likely way (after volatility) to
manufacture edge that does not exist.

THE ARITHMETIC EVERY OPTIONS STRATEGY MUST CLEAR. A one-lot SPY 30-delta call
round trip costs roughly

    2 x $0.65 commission  +  2 x (half-spread x 100)

On a $3.00 option with a 0.8%-of-mid spread that is about $1.30 + $2.40 =
$3.70 against $300 of premium, i.e. ~1.2% round trip. On a tier-4 name at 12%
of mid it is over 12%. The equity research campaigns already learned the
general form of this lesson the hard way: `research/README.md` records a
0.3xATR take-profit family that was arithmetically dead because its edge was
0.028% against ~0.10% of friction. Options make the same trap an order of
magnitude deeper.

The model is parametric and calibratable, with per-tier defaults so it is
useful before any chain data has been recorded. Defaults are deliberately
PESSIMISTIC: a backtest that overstates costs merely rejects a marginal
strategy, while one that understates them recommends a losing one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Liquidity tiers. Assigning an underlying to too liquid a tier is the
#: dangerous direction, so unknown symbols default to the WIDEST tier.
TIER_INDEX_ETF = 1      # SPY, QQQ, IWM — pennies wide
TIER_MEGA_CAP = 2       # AAPL, NVDA, MSFT
TIER_LIQUID = 3         # most S&P names
TIER_ILLIQUID = 4       # everything else, incl. small caps and levered ETFs

_TIER_1 = frozenset({"SPY", "QQQ", "IWM", "SPX", "NDX", "DIA"})
_TIER_2 = frozenset({
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AMD",
    "NFLX", "SMH", "SOXX", "TQQQ", "SQQQ", "GLD", "TLT", "XLF", "XLE", "EEM",
})
_TIER_3 = frozenset({
    "MU", "INTC", "COIN", "PLTR", "BAC", "JPM", "XOM", "CVX", "WMT", "COST",
    "DIS", "V", "MA", "UNH", "JNJ", "PG", "KO", "PEP", "CRM", "ORCL", "ADBE",
    "AVGO", "QCOM", "TXN", "IBM", "GE", "F", "T", "VZ", "PFE", "MRK", "ABBV",
    "SOXL", "SPXL", "UPRO", "LABU", "ARKK", "UVXY", "VXX",
})

#: spread_pct = a + b*|m| + c/sqrt(dte), clamped. `a` is the at-the-money,
#: long-dated floor; `b` widens the wings; `c` widens short-dated contracts,
#: where the premium is small and the tick is a larger fraction of it.
_TIER_PARAMS: dict[int, tuple[float, float, float]] = {
    TIER_INDEX_ETF: (0.004, 0.010, 0.010),
    TIER_MEGA_CAP: (0.010, 0.020, 0.020),
    TIER_LIQUID: (0.025, 0.045, 0.045),
    TIER_ILLIQUID: (0.060, 0.090, 0.090),
}

#: Floor and cap on the modelled spread, as a fraction of mid.
MIN_SPREAD_PCT = 0.001
MAX_SPREAD_PCT = 0.60

#: Penny-pilot tick rules: $0.01 below $3.00, $0.05 above. The chain reports
#: `isPennyPilot` per contract; where it is unknown these are the safe general
#: rule. A spread can never be narrower than one tick.
PENNY_TICK = 0.01
NICKEL_TICK = 0.05
PENNY_PILOT_BOUNDARY = 3.00

#: Schwab's per-contract options commission. Equities are commission-free at
#: Schwab; options are not, and at typical position sizes this is a material
#: share of the round-trip cost.
DEFAULT_COMMISSION_PER_CONTRACT = 0.65


def tier_for(underlying: str) -> int:
    """Liquidity tier for an underlying. Unknown names get the WIDEST tier —
    assuming a name is more liquid than it is would understate costs, which is
    the dangerous direction."""
    u = (underlying or "").strip().upper()
    if u in _TIER_1:
        return TIER_INDEX_ETF
    if u in _TIER_2:
        return TIER_MEGA_CAP
    if u in _TIER_3:
        return TIER_LIQUID
    return TIER_ILLIQUID


def tick_size(mid: float, penny_pilot: bool = True) -> float:
    if not penny_pilot:
        return NICKEL_TICK
    return PENNY_TICK if mid < PENNY_PILOT_BOUNDARY else NICKEL_TICK


@dataclass(frozen=True)
class SpreadParams:
    """Per-underlying spread shape. `calibrated` is carried into the backtest's
    assumptions block for the same reason it is on SurfaceParams: a fitted cost
    model and a guessed one deserve different trust."""

    underlying: str
    a: float
    b: float
    c: float
    tier: int
    calibrated: bool = False
    n_obs: int = 0

    @classmethod
    def default_for(cls, underlying: str, tier: int | None = None) -> "SpreadParams":
        t = tier if tier is not None else tier_for(underlying)
        a, b, c = _TIER_PARAMS[t]
        return cls(underlying=(underlying or "").strip().upper(), a=a, b=b, c=c, tier=t)

    def to_dict(self) -> dict:
        return {"underlying": self.underlying, "tier": self.tier, "a": self.a,
                "b": self.b, "c": self.c, "calibrated": self.calibrated,
                "n_obs": self.n_obs}


def spread_pct(params: SpreadParams, moneyness: float, dte: float) -> float:
    """Modelled spread as a fraction of mid.

    `moneyness` is standardised log-moneyness (the same units the vol surface
    uses), so one parameter set covers every tenor."""
    d = max(float(dte), 1.0)
    raw = params.a + params.b * abs(moneyness) + params.c / math.sqrt(d)
    return min(max(raw, MIN_SPREAD_PCT), MAX_SPREAD_PCT)


def quote_from_mid(mid: float, params: SpreadParams, moneyness: float, dte: float,
                   *, penny_pilot: bool = True) -> tuple[float, float]:
    """(bid, ask) around a modelled mid.

    The absolute spread is floored at ONE TICK: a modelled 0.4% spread on a
    $0.30 option is a tenth of a cent, which no real market quotes. That floor
    matters more than it looks — cheap far-OTM options are exactly where a
    naive percentage model would understate costs most badly, and exactly where
    strategies like to trade."""
    if mid <= 0:
        return 0.0, 0.0
    pct = spread_pct(params, moneyness, dte)
    absolute = max(mid * pct, tick_size(mid, penny_pilot))
    half = absolute / 2.0
    bid = max(mid - half, 0.0)
    ask = mid + half
    return bid, ask


def fill_price(bid: float, ask: float, side: str, aggression: float = 1.0) -> float:
    """The price actually transacted.

    `side` is "open" (buying to open a long) or "close" (selling to close).
    `aggression` = 1.0 crosses the full spread and is the DEFAULT: assuming a
    passive mid fill that may never happen is how a backtest quietly awards
    itself free money on every trade. Lower values are permitted for
    sensitivity analysis and draw a lint warning below 0.5."""
    mid = (bid + ask) / 2.0
    half = max(ask - bid, 0.0) / 2.0
    adj = half * max(0.0, aggression)
    return mid + adj if side == "open" else max(mid - adj, 0.0)


def round_trip_cost(mid: float, params: SpreadParams, moneyness: float, dte: float,
                    qty: int = 1, multiplier: float = 100.0,
                    commission_per_contract: float = DEFAULT_COMMISSION_PER_CONTRACT,
                    aggression: float = 1.0) -> dict:
    """Total cost of opening AND closing a position, in dollars and as a
    percentage of premium.

    Exposed as a first-class function (not buried in the engine) because it is
    the number that decides whether a strategy is viable at all, and it belongs
    in the UI and the docs where a user will see it BEFORE running a backtest
    rather than after."""
    bid, ask = quote_from_mid(mid, params, moneyness, dte)
    spread_cost = (ask - bid) * aggression * qty * multiplier
    commission = 2 * commission_per_contract * qty
    premium = mid * qty * multiplier
    total = spread_cost + commission
    return {
        "premium": premium,
        "spread_cost": spread_cost,
        "commission": commission,
        "total": total,
        "pct_of_premium": (total / premium * 100.0) if premium > 0 else float("inf"),
        "spread_pct_of_mid": spread_pct(params, moneyness, dte) * 100.0,
    }
