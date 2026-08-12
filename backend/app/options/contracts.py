"""Contract identity, quotes, and the strike ladder.

`Contract` is the option analogue of a ticker: the immutable identity of the
instrument being traded. `ContractQuote` is one observation of its price and
greeks, whichever source produced it (recorded snapshot, live stream, or the
synthetic pricer) — one shape so the engine and the runner consume all three
identically.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from .symbology import OccSymbol, canonical_underlying, format_occ, parse_occ

Right = Literal["C", "P"]
#: The schema/UI spell these out; the wire and OCC use single letters.
RIGHT_FROM_WORD = {"call": "C", "put": "P", "C": "C", "P": "P"}
RIGHT_TO_WORD = {"C": "call", "P": "put"}

DEFAULT_MULTIPLIER = 100.0


@dataclass(frozen=True)
class Contract:
    """One option contract.

    `multiplier` is carried per contract rather than assumed to be 100
    (plan/16 D10): mini options and post-split adjusted contracts exist and
    have different multipliers, and a hardcoded 100 would misprice them by a
    factor of ten while looking entirely plausible. v1 EXCLUDES those contracts
    at selection time, but the field stays honest so that a position adopted
    from the broker during recovery carries the broker's real number."""

    underlying: str
    expiry: dt.date
    right: Right
    strike: Decimal
    multiplier: float = DEFAULT_MULTIPLIER
    is_non_standard: bool = False
    is_mini: bool = False

    @property
    def symbol(self) -> str:
        return format_occ(self.underlying, self.expiry, self.right, self.strike)

    @property
    def strike_f(self) -> float:
        """Float strike for the pricing maths. Deliberately a separate accessor
        from `strike` so that identity/serialization stay exact (Decimal) while
        Black-Scholes gets the float it needs."""
        return float(self.strike)

    @property
    def is_call(self) -> bool:
        return self.right == "C"

    def describe(self) -> str:
        return parse_occ(self.symbol).describe()

    @classmethod
    def from_symbol(cls, symbol: str, *, multiplier: float = DEFAULT_MULTIPLIER,
                    is_non_standard: bool = False, is_mini: bool = False) -> "Contract":
        occ: OccSymbol = parse_occ(symbol)
        return cls(underlying=canonical_underlying(occ.underlying), expiry=occ.expiry, right=occ.right,
                   strike=occ.strike, multiplier=multiplier,
                   is_non_standard=is_non_standard, is_mini=is_mini)

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "underlying": self.underlying,
                "expiry": self.expiry.isoformat(), "right": self.right,
                "right_word": RIGHT_TO_WORD[self.right],
                "strike": float(self.strike), "multiplier": self.multiplier,
                "is_non_standard": self.is_non_standard, "is_mini": self.is_mini}


@dataclass(frozen=True)
class Greeks:
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0     # PER CALENDAR DAY (see blackscholes.greeks)
    vega: float = 0.0      # per 1 vol POINT (0.01 of sigma)
    rho: float = 0.0


@dataclass(frozen=True)
class ContractQuote:
    """One price observation. `bid`/`ask` are what you can actually transact at;
    `mid` is the reference. `source` records which pricer produced it, and is
    carried all the way into the backtest's assumptions block and the UI —
    a user must always be able to tell a modelled price from an observed one."""

    contract: Contract
    ts: int                      # epoch ms
    mid: float
    bid: float
    ask: float
    underlying_px: float
    iv: float = 0.0
    greeks: Greeks = field(default_factory=Greeks)
    volume: int | None = None
    open_interest: int | None = None
    source: str = "synthetic"    # synthetic | recorded | live

    @property
    def spread(self) -> float:
        return max(self.ask - self.bid, 0.0)

    @property
    def spread_pct(self) -> float:
        """Spread as a percentage of mid — the number that decides whether a
        contract is tradeable at all. Guarded against a zero mid, which happens
        for deep-OTM contracts whose bid and ask are both 0."""
        return (self.spread / self.mid * 100.0) if self.mid > 0 else float("inf")

    def price_for(self, side: Literal["open", "close"]) -> float:
        """The price actually paid/received for a LONG option position: you buy
        at the ask to open and sell at the bid to close. Both are the adverse
        side, matching the equity engine's `_slip` convention."""
        return self.ask if side == "open" else self.bid

    def to_dict(self) -> dict:
        return {**self.contract.to_dict(), "ts": self.ts, "mid": self.mid,
                "bid": self.bid, "ask": self.ask, "spread": self.spread,
                "spread_pct": self.spread_pct, "iv": self.iv,
                "underlying_px": self.underlying_px,
                "delta": self.greeks.delta, "gamma": self.greeks.gamma,
                "theta": self.greeks.theta, "vega": self.greeks.vega,
                "rho": self.greeks.rho, "volume": self.volume,
                "open_interest": self.open_interest, "source": self.source}


@dataclass(frozen=True)
class ContractOHLC:
    """A contract's bar, derived from the underlying's bar. See
    `pricing.SyntheticPricer.bar` for why high/low are exact under a static-vol
    assumption and what that assumption costs."""
    ts: int
    open: float
    high: float
    low: float
    close: float
    source: str = "synthetic"


# ---- strike ladder --------------------------------------------------------

#: Listed strike increments by price band. An APPROXIMATION used only to build
#: the synthetic chain a historical backtest trades against; whenever a
#: recorded chain exists its real strikes win (see store.read_chain). Getting
#: this slightly wrong shifts a selected strike by one increment, which is a
#: second-order error next to the volatility assumption -- but snapping to a
#: ladder at all matters a lot, because an unsnapped strike is a contract that
#: never listed.
_STRIKE_BANDS: tuple[tuple[float, float], ...] = (
    (25.0, 0.50),
    (200.0, 1.00),
    (500.0, 2.50),
    (float("inf"), 5.00),
)

#: Products that list $1 strikes across their whole range regardless of price.
_DOLLAR_STRIKE_UNDERLYINGS = frozenset({"SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "EEM"})


def strike_increment(underlying: str, price: float) -> float:
    if (underlying or "").strip().upper() in _DOLLAR_STRIKE_UNDERLYINGS:
        return 1.0
    for ceiling, inc in _STRIKE_BANDS:
        if price < ceiling:
            return inc
    return 5.0


def snap_strike(underlying: str, target: float, price: float | None = None) -> Decimal:
    """Snap a computed strike to the nearest listed increment.

    `price` (the underlying's price) selects the increment band; it defaults to
    the target itself, which is right for at/near-the-money strikes and close
    enough for the wings."""
    inc = strike_increment(underlying, price if price is not None else target)
    if target <= 0:
        raise ValueError(f"strike target must be positive, got {target}")
    snapped = round(target / inc) * inc
    if snapped <= 0:
        snapped = inc
    # Quantize through Decimal so the result is exactly representable as an OCC
    # strike (thousandths) rather than carrying float noise into the symbol.
    return Decimal(str(round(snapped, 3)))


def strike_ladder(underlying: str, price: float, *, n_each_side: int = 30) -> list[Decimal]:
    """A synthetic chain's strike list around the current price."""
    inc = strike_increment(underlying, price)
    atm = float(snap_strike(underlying, price, price))
    out: list[Decimal] = []
    for k in range(-n_each_side, n_each_side + 1):
        s = atm + k * inc
        if s > 0:
            out.append(Decimal(str(round(s, 3))))
    return out
