"""Signal -> contract: the expression layer.

Shared by the backtest engine and the live runner, for the same reason
`sizing.py` and `stops.py` are shared — if contract selection were implemented
twice, a strategy would eventually pick a different contract in backtest than in
life, and the backtest would stop meaning anything.

Two chain sources sit behind one interface:

  RealChainView       contracts that genuinely exist, from a live Schwab chain
                      or a recorded snapshot. Full liquidity filtering.
  SyntheticChainView  a generated strike ladder over real expiration DATES, for
                      historical backtests where no chain data exists. Strikes
                      are snapped to a plausible ladder and expirations come
                      from the real listing calendar, so the simulation never
                      trades a contract that could not have existed — but open
                      interest and volume are UNKNOWABLE and cannot be filtered.
                      That limitation is reported, never silently skipped.

EVERY REJECTION IS EXPLAINED. `select_contract` returns a `Rejection` with a
machine-readable reason rather than None. A strategy that took 3 trades instead
of 300 needs to say why — the equity research campaigns lost real time to
exactly this class of silence (`research/README.md`: the highest/donchian
current-bar trap produced zero trades with no explanation).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..options.blackscholes import delta as bs_delta
from ..options.contracts import (RIGHT_FROM_WORD, Contract, ContractQuote,
                                 snap_strike, strike_ladder)
from ..options.expiry import (Expiration, days_to_expiry, generate_expirations,
                              ms_to_expiry, year_fraction)
from .schema import Liquidity, SideExpression, StrikeSelector

#: Machine-readable rejection reasons. The engine counts these by reason and
#: reports the histogram in the backtest result.
REASON_NO_EXPIRY = "no_expiry_in_window"
REASON_NO_STRIKE = "no_strike_within_tolerance"
REASON_SPREAD_TOO_WIDE = "spread_too_wide"
REASON_OI_TOO_LOW = "open_interest_too_low"
REASON_VOLUME_TOO_LOW = "volume_too_low"
REASON_NON_STANDARD = "non_standard_excluded"
REASON_NO_QUOTE = "no_usable_quote"
REASON_NO_CHAIN = "no_chain_data"
REASON_ATR_UNAVAILABLE = "atr_unavailable"
REASON_EXPIRED = "contract_expired"


@dataclass(frozen=True)
class Rejection:
    """Why no contract could be selected. Never None — silence is the enemy."""
    reason: str
    detail: str = ""

    def __bool__(self) -> bool:
        return False


@dataclass(frozen=True)
class Selection:
    """A chosen contract, with the quote that justified it."""
    contract: Contract
    quote: ContractQuote | None = None
    #: how far the chosen strike landed from what was asked for
    strike_error: float = 0.0
    expiry_dte: int = 0

    def __bool__(self) -> bool:
        return True


class ChainView(Protocol):
    """The contracts available for one underlying at one moment."""

    def expirations(self, as_of_ms: int) -> list[Expiration]: ...

    def strikes(self, expiry: dt.date, right: str) -> list[Decimal]: ...

    def quote_for(self, contract: Contract, underlying_px: float,
                  ts: int) -> ContractQuote | None: ...


@dataclass
class SyntheticChainView:
    """A generated ladder over REAL expiration dates.

    The expirations are the point. Strike granularity being slightly wrong
    shifts a selection by one increment — a second-order error next to the
    volatility assumption. Inventing an expiration that never listed is a
    different class of mistake: it makes the backtest trade an instrument that
    did not exist."""

    underlying: str
    pricer: object                       # SyntheticPricer
    recorded_expirations: list[Expiration] | None = None
    n_strikes_each_side: int = 30
    max_dte: int = 400

    def expirations(self, as_of_ms: int) -> list[Expiration]:
        as_of = ms_to_expiry(as_of_ms)
        if self.recorded_expirations:
            found = [e for e in self.recorded_expirations if e.expiry >= as_of]
            if found:
                return found
        return generate_expirations(self.underlying, as_of, max_dte=self.max_dte)

    def strikes(self, expiry: dt.date, right: str) -> list[Decimal]:
        raise NotImplementedError("synthetic strikes need the spot price; "
                                  "use strikes_at()")

    def strikes_at(self, underlying_px: float) -> list[Decimal]:
        return strike_ladder(self.underlying, underlying_px,
                             n_each_side=self.n_strikes_each_side)

    def quote_for(self, contract: Contract, underlying_px: float,
                  ts: int) -> ContractQuote | None:
        return self.pricer.quote(contract, underlying_px, ts)


@dataclass
class RealChainView:
    """Contracts that genuinely exist, from a live or recorded chain.

    `rows` are the flattened, already-validated snapshot rows (see
    options/chain_service.py), so everything here has passed the data-quality
    guards."""

    underlying: str
    rows: list                            # SnapshotRow-like
    quotes: dict[str, ContractQuote] | None = None

    def __post_init__(self) -> None:
        self._by_expiry: dict[dt.date, dict[str, list]] = {}
        for r in self.rows:
            exp = ms_to_expiry(r.expiry)
            self._by_expiry.setdefault(exp, {"C": [], "P": []})
            self._by_expiry[exp][r.opt_right].append(r)

    def expirations(self, as_of_ms: int) -> list[Expiration]:
        as_of = ms_to_expiry(as_of_ms)
        return [Expiration(expiry=e, expiration_type="S")
                for e in sorted(self._by_expiry) if e >= as_of]

    def strikes(self, expiry: dt.date, right: str) -> list[Decimal]:
        rows = (self._by_expiry.get(expiry) or {}).get(right, [])
        return sorted(Decimal(str(r.strike)) for r in rows)

    def row_for(self, expiry: dt.date, right: str, strike: Decimal):
        for r in (self._by_expiry.get(expiry) or {}).get(right, []):
            if abs(r.strike - float(strike)) < 1e-9:
                return r
        return None

    def quote_for(self, contract: Contract, underlying_px: float,
                  ts: int) -> ContractQuote | None:
        if self.quotes is not None:
            return self.quotes.get(contract.symbol)
        return None


# ---- selection ------------------------------------------------------------

def select_expiry(view: ChainView, expr: SideExpression, ts: int
                  ) -> Expiration | Rejection:
    """The expiration nearest `dte_target` within [dte_min, dte_max].

    Ties break toward the LONGER tenor (less gamma and theta risk) and then
    toward the standard monthly (deeper liquidity) — both deterministic, so a
    backtest is reproducible."""
    e = expr.expiry
    candidates: list[tuple[int, int, Expiration]] = []
    for exp in view.expirations(ts):
        if exp.expiration_type not in e.allow:
            continue
        dte = days_to_expiry(exp.expiry, ts)
        if dte < e.dte_min or dte > e.dte_max:
            continue
        # sort key: distance from target, then prefer longer, then standard
        candidates.append((abs(dte - e.dte_target), -dte, exp))
    if not candidates:
        return Rejection(REASON_NO_EXPIRY,
                         f"no {'/'.join(e.allow)} expiration in "
                         f"{e.dte_min}-{e.dte_max} DTE")
    candidates.sort(key=lambda t: (t[0], t[1], 0 if t[2].standard else 1))
    return candidates[0][2]


def target_strike(sel: StrikeSelector, right: str, underlying_px: float,
                  expiry: dt.date, ts: int, *, atr: float | None = None,
                  sigma: float = 0.20, r: float = 0.04, q: float = 0.0
                  ) -> float | Rejection:
    """The strike the selector is asking for, before snapping to a ladder."""
    is_call = right == "C"
    if sel.by == "atm":
        return underlying_px
    if sel.by == "moneyness_pct":
        # calls go UP from spot, puts DOWN — "5% out of the money" is
        # direction-relative, exactly like exit distances elsewhere
        f = sel.target / 100.0
        return underlying_px * (1 + f) if is_call else underlying_px * (1 - f)
    if sel.by == "offset_dollars":
        return underlying_px + sel.target if is_call else underlying_px - sel.target
    if sel.by == "atr_multiple":
        if atr is None or atr != atr or atr <= 0:
            return Rejection(REASON_ATR_UNAVAILABLE,
                             f"strike.by=atr_multiple needs {sel.atr_indicator!r}")
        return (underlying_px + sel.target * atr if is_call
                else underlying_px - sel.target * atr)

    # by == "delta": invert Black-Scholes for the strike with the target delta.
    # Solved by bisection on strike — delta is monotone in K, so this always
    # converges, and it avoids needing an analytic inverse of the normal CDF.
    T = year_fraction(expiry, ts)
    want = abs(sel.target)
    lo, hi = underlying_px * 0.2, underlying_px * 3.0
    for _ in range(80):
        mid = (lo + hi) / 2
        d = abs(bs_delta(underlying_px, mid, T, r, q, sigma, right))
        # call delta FALLS as strike rises; put |delta| RISES as strike rises
        if (d > want) == is_call:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return (lo + hi) / 2


def select_contract(expr: SideExpression, liq: Liquidity, view: ChainView,
                    underlying: str, underlying_px: float, ts: int, *,
                    atr: float | None = None, sigma: float = 0.20,
                    r: float = 0.04, q: float = 0.0) -> Selection | Rejection:
    """Pick the contract that expresses this signal, or explain why not."""
    if underlying_px is None or underlying_px <= 0:
        return Rejection(REASON_NO_CHAIN, "no underlying price")

    right = RIGHT_FROM_WORD.get(expr.right, "C")

    exp = select_expiry(view, expr, ts)
    if isinstance(exp, Rejection):
        return exp
    dte = days_to_expiry(exp.expiry, ts)

    want = target_strike(expr.strike, right, underlying_px, exp.expiry, ts,
                         atr=atr, sigma=sigma, r=r, q=q)
    if isinstance(want, Rejection):
        return want

    # ---- snap to an available strike ----
    if isinstance(view, RealChainView):
        available = view.strikes(exp.expiry, right)
        if not available:
            return Rejection(REASON_NO_STRIKE,
                             f"no {right} strikes listed for {exp.expiry}")
        chosen = min(available, key=lambda k: abs(float(k) - want))
    else:
        chosen = snap_strike(underlying, want, underlying_px)

    strike_error = abs(float(chosen) - want)
    # Tolerance is expressed in the selector's own units, so it only bounds the
    # search where that is meaningful. For a delta selector the check is on the
    # realised delta below, not on the dollar distance.
    if expr.strike.by in ("moneyness_pct", "offset_dollars", "atr_multiple"):
        limit = max(underlying_px * 0.02, expr.strike.tolerance * underlying_px / 100)
        if strike_error > limit:
            return Rejection(
                REASON_NO_STRIKE,
                f"nearest strike {float(chosen)} is {strike_error:.2f} from the "
                f"requested {want:.2f}")

    contract = Contract(underlying=underlying.strip().upper(), expiry=exp.expiry,
                        right=right, strike=chosen)

    if days_to_expiry(contract.expiry, ts) < 0:
        return Rejection(REASON_EXPIRED, contract.symbol)

    # ---- liquidity, where it can be known ----
    row = view.row_for(exp.expiry, right, chosen) if isinstance(view, RealChainView) else None
    if row is not None:
        if liq.exclude_non_standard and getattr(row, "is_non_standard", 0):
            return Rejection(REASON_NON_STANDARD, contract.symbol)
        oi = getattr(row, "open_interest", None)
        if liq.min_open_interest and oi is not None and oi < liq.min_open_interest:
            return Rejection(REASON_OI_TOO_LOW,
                             f"{contract.symbol}: OI {oi} < {liq.min_open_interest}")
        vol = getattr(row, "volume", None)
        if liq.min_volume and vol is not None and vol < liq.min_volume:
            return Rejection(REASON_VOLUME_TOO_LOW,
                             f"{contract.symbol}: volume {vol} < {liq.min_volume}")

    quote = view.quote_for(contract, underlying_px, ts)
    if quote is None:
        return Rejection(REASON_NO_QUOTE, contract.symbol)
    if quote.mid <= 0 or quote.ask <= 0:
        return Rejection(REASON_NO_QUOTE, f"{contract.symbol}: no two-sided market")

    if quote.spread_pct > liq.max_spread_pct:
        return Rejection(
            REASON_SPREAD_TOO_WIDE,
            f"{contract.symbol}: spread {quote.spread_pct:.1f}% > "
            f"{liq.max_spread_pct}%")
    if liq.max_spread_abs is not None and quote.spread > liq.max_spread_abs:
        return Rejection(
            REASON_SPREAD_TOO_WIDE,
            f"{contract.symbol}: spread ${quote.spread:.2f} > ${liq.max_spread_abs}")

    # For a delta selector, verify the contract we actually got is within
    # tolerance of the delta we asked for. This is the check that matters: the
    # strike is only a means to a delta.
    if expr.strike.by == "delta" and quote.greeks.delta:
        got = abs(quote.greeks.delta)
        if abs(got - abs(expr.strike.target)) > expr.strike.tolerance:
            return Rejection(
                REASON_NO_STRIKE,
                f"{contract.symbol}: delta {got:.3f} is outside "
                f"{expr.strike.target:.3f} +/- {expr.strike.tolerance:.3f}")

    return Selection(contract=contract, quote=quote, strike_error=strike_error,
                     expiry_dte=dte)


def right_for_side(expr, side: str):
    """The SideExpression governing a signal direction.

    A LONG signal buys calls and a SHORT signal buys puts (plan/16 D2) — both
    long-premium, so a 'short' options strategy needs no margin account, no
    borrow, and cannot lose more than its debit. That is a genuinely different
    risk profile from short SHARES and must never be routed through the
    equity short path."""
    if expr is None or expr.instrument != "options":
        return None
    return expr.long if side == "long" else expr.short
