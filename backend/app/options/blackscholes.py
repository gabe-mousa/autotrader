"""Generalized Black-Scholes-Merton: price, greeks, implied volatility.

Pure functions, scalar in / scalar out, no I/O and no pandas. Deliberately
dependency-free — the project has no scipy, so the normal CDF comes from
`math.erf` and the implied-vol solver is written here. That is a feature as
much as a constraint: both are then deterministic, seed-free and testable.

CONVENTIONS, all of which have burned someone before:

  * Continuous dividend yield `q` (Merton). Ignoring it biases calls cheap and
    puts rich; on SPY's ~1.2% yield that is not negligible at longer DTEs.
  * `theta` is returned PER CALENDAR DAY, not per year. Per-year theta is ~365x
    larger and silently corrupts any P&L attribution that mixes the two.
  * `vega` is per ONE VOL POINT (a 0.01 change in sigma), which is how every
    trading screen quotes it — not per unit of sigma.
  * `rho` is likewise per one percentage point of rate.
  * European exercise. Equity options are American, but for the LONG positions
    v1 trades (plan/16 D2/D3) the early-exercise premium is zero for calls on
    non-dividend payers and small for puts at the DTEs and moneyness in scope.
    The bias is documented in the backtest assumptions block rather than
    silently absorbed: it makes long puts marginally CHEAP, i.e. optimistic for
    a buyer, so it is a bias worth stating.
  * Every function returns finite numbers or None. Deep ITM/OTM contracts and
    T -> 0 are the normal case at expiry, not an edge case, so they are handled
    by construction rather than by exception.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Right = Literal["C", "P"]

#: Numerical floors. `T` and `sigma` both appear in a denominator via
#: sigma*sqrt(T), so both are floored rather than guarded at every call site.
MIN_T = 1.0 / (365.0 * 24)      # one hour, in years
MIN_SIGMA = 1e-6
#: d1/d2 are clamped before entering the CDF: beyond ~±10 the CDF is 0 or 1 to
#: double precision anyway, and clamping keeps exp() from underflowing to a NaN
#: in the greeks.
MAX_D = 12.0

DAYS_PER_YEAR = 365.0
_INV_SQRT_2PI = 0.3989422804014327

#: Minimum extrinsic value (in price units) for an implied vol to be
#: recoverable at all. Options tick at $0.01 (or $0.05 above $3), so a contract
#: carrying less than a millionth of a dollar of time value has none to speak
#: of, and any "implied vol" read off it is noise.
_EXTRINSIC_EPS = 1e-6


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float    # per calendar day
    vega: float     # per 1 vol point (0.01 of sigma)
    rho: float      # per 1 percentage point of rate


def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function. `math.erf` is correctly
    rounded in CPython, so this is accurate to ~1e-16 — better than the
    polynomial approximations usually reached for, and with no dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def _clamp_d(x: float) -> float:
    return MAX_D if x > MAX_D else (-MAX_D if x < -MAX_D else x)


def d1_d2(S: float, K: float, T: float, r: float, q: float,
          sigma: float) -> tuple[float, float]:
    """The two Black-Scholes arguments. Inputs are floored, not validated, so
    this never raises for a live position approaching expiry."""
    T = max(T, MIN_T)
    sigma = max(sigma, MIN_SIGMA)
    vol_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_t
    return _clamp_d(d1), _clamp_d(d1 - vol_t)


def intrinsic(S: float, K: float, right: Right) -> float:
    return max(S - K, 0.0) if right == "C" else max(K - S, 0.0)


def price(S: float, K: float, T: float, r: float, q: float, sigma: float,
          right: Right) -> float:
    """Option price. Returns discounted intrinsic at T<=0 or sigma<=0, which is
    the correct limit and keeps expiry handling from needing a special case in
    every caller."""
    if S <= 0 or K <= 0:
        raise ValueError(f"S and K must be positive, got S={S}, K={K}")
    if T <= 0:
        return intrinsic(S, K, right)
    if sigma <= 0:
        # zero-vol limit: the forward is known, so the payoff is deterministic
        fwd = S * math.exp((r - q) * T)
        return math.exp(-r * T) * intrinsic(fwd, K, right)

    d1, d2 = d1_d2(S, K, T, r, q, sigma)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if right == "C":
        return S * disc_q * norm_cdf(d1) - K * disc_r * norm_cdf(d2)
    return K * disc_r * norm_cdf(-d2) - S * disc_q * norm_cdf(-d1)


def greeks(S: float, K: float, T: float, r: float, q: float, sigma: float,
           right: Right) -> Greeks:
    """All five greeks in one pass — they share d1/d2 and the discount factors,
    and computing them separately would triple the cost of the engine's hot
    loop for no benefit."""
    if S <= 0 or K <= 0:
        raise ValueError(f"S and K must be positive, got S={S}, K={K}")
    T = max(T, MIN_T)
    sigma = max(sigma, MIN_SIGMA)

    d1, d2 = d1_d2(S, K, T, r, q, sigma)
    sqrt_t = math.sqrt(T)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    pdf_d1 = norm_pdf(d1)
    is_call = right == "C"

    delta = disc_q * (norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0)
    gamma = disc_q * pdf_d1 / (S * sigma * sqrt_t)
    # vega is identical for calls and puts; /100 puts it per VOL POINT
    vega = S * disc_q * pdf_d1 * sqrt_t / 100.0

    term1 = -(S * disc_q * pdf_d1 * sigma) / (2.0 * sqrt_t)
    if is_call:
        theta_year = (term1
                      - r * K * disc_r * norm_cdf(d2)
                      + q * S * disc_q * norm_cdf(d1))
        rho = K * T * disc_r * norm_cdf(d2) / 100.0
    else:
        theta_year = (term1
                      + r * K * disc_r * norm_cdf(-d2)
                      - q * S * disc_q * norm_cdf(-d1))
        rho = -K * T * disc_r * norm_cdf(-d2) / 100.0

    return Greeks(delta=delta, gamma=gamma, theta=theta_year / DAYS_PER_YEAR,
                  vega=vega, rho=rho)


def delta(S: float, K: float, T: float, r: float, q: float, sigma: float,
          right: Right) -> float:
    """Delta alone — the hot path for strike selection by delta, where the
    other four greeks are not needed."""
    if T <= 0:
        return (1.0 if S > K else 0.0) if right == "C" else (-1.0 if S < K else 0.0)
    d1, _ = d1_d2(S, K, T, r, q, sigma)
    disc_q = math.exp(-q * max(T, MIN_T))
    return disc_q * (norm_cdf(d1) if right == "C" else norm_cdf(d1) - 1.0)


# ---- no-arbitrage bounds --------------------------------------------------

def price_bounds(S: float, K: float, T: float, r: float, q: float,
                 right: Right) -> tuple[float, float]:
    """(lower, upper) no-arbitrage bounds. Used by `implied_vol` to reject an
    unattainable target BEFORE spending iterations on it, and by the surface
    sanity checks."""
    T = max(T, 0.0)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if right == "C":
        return max(S * disc_q - K * disc_r, 0.0), S * disc_q
    return max(K * disc_r - S * disc_q, 0.0), K * disc_r


def implied_vol(target: float, S: float, K: float, T: float, r: float, q: float,
                right: Right, *, lo: float = 1e-4, hi: float = 5.0,
                tol: float = 1e-7, max_iter: int = 100) -> float | None:
    """Invert `price` for sigma. Returns None — never raises, never 0.0 — when
    the target is outside no-arbitrage bounds or the solve does not converge.

    None is a meaningful answer that callers must handle: it means "this quote
    is not consistent with any volatility", which for a recorded chain row
    usually means the quote is stale or crossed rather than that the maths
    failed.

    Newton's method with a bisection fallback. Newton alone is unreliable here:
    vega collapses toward zero for deep ITM/OTM contracts, so the step
    explodes. Bracketing first and falling back to bisection makes convergence
    unconditional within the bracket."""
    if S <= 0 or K <= 0 or T <= 0 or target <= 0:
        return None
    low_bound, high_bound = price_bounds(S, K, T, r, q, right)
    # a hair of tolerance: a quote exactly AT intrinsic implies zero vol
    if target < low_bound - 1e-9 or target > high_bound + 1e-9:
        return None

    px_lo = price(S, K, T, r, q, lo, right)
    px_hi = price(S, K, T, r, q, hi, right)

    # UNIDENTIFIABLE. Implied vol is recoverable only from a contract's
    # EXTRINSIC value; `px_lo` is the zero-vol price, so `target - px_lo` is
    # how much premium the market is actually paying for uncertainty. A deep
    # ITM contract (or any contract at the last moment before expiry) has none
    # of it — the extrinsic value has rounded away in double precision — and
    # then no volatility can be inferred, however wide the price range across
    # sigma happens to be.
    #
    # This MUST return None rather than the bracket's lower end. Returning
    # `lo` yields 0.0001, which is indistinguishable from a genuine 0.01% vol
    # reading and would silently drag any calibration that averaged it toward
    # zero — precisely the kind of quiet corruption plan/16 §6 exists to avoid.
    if target - px_lo <= _EXTRINSIC_EPS:
        return None
    if px_hi - target < 0:
        return None          # richer than 500% vol — not a real quote
    if abs(px_hi - target) < tol:
        return hi

    # The bracket is now known valid: the checks above established
    # price(lo) < target <= price(hi), and price is strictly increasing in
    # sigma, so a root exists in [lo, hi].
    #
    # Newton from a Brenner-Subrahmanyam style seed, which is a good starting
    # point for near-the-money contracts and harmless elsewhere.
    sigma = max(min(math.sqrt(2 * math.pi / T) * target / S, hi), lo)
    a, b = lo, hi
    for _ in range(max_iter):
        px = price(S, K, T, r, q, sigma, right)
        diff = px - target
        if abs(diff) < tol:
            return sigma
        if diff > 0:
            b = sigma
        else:
            a = sigma
        v = greeks(S, K, T, r, q, sigma, right).vega * 100.0   # per unit sigma
        if v > 1e-10:
            step = diff / v
            nxt = sigma - step
            if a < nxt < b:
                sigma = nxt
                continue
        sigma = 0.5 * (a + b)          # Newton left the bracket — bisect
        if b - a < tol:
            return sigma
    return sigma if abs(price(S, K, T, r, q, sigma, right) - target) < 1e-4 else None
