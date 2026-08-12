"""The volatility surface: sigma(K, T) for a synthetic backtest.

This module is where an options backtest is won or lost. Black-Scholes is
arithmetic; the volatility fed into it is the entire modelling problem, and a
wrong answer here produces confident, plausible, completely fictional results.

THE FAILURE MODE THIS MODULE EXISTS TO PREVENT (plan/16 decision D6). The
obvious approach — compute the underlying's REALIZED volatility and price
options off that — is wrong in a specific and expensive direction. Options
trade at IMPLIED vol, which has exceeded subsequent realized vol on index
products in roughly 85% of months since 1990: sellers charge for tail risk.
Price 2015's options off 2015's realized vol and every one of them is ~15% too
cheap, for ten straight years, and every long-premium strategy shows a large
profit that never existed. So the variance risk premium is an explicit,
required, conservatively-defaulted parameter, and lint warns when it is
weakened.

FOUR COMPONENTS, each independently defensible and independently calibratable:

  1. LEVEL — ATM 30-day implied vol. Preference order:
       recorded chain IV  >  volatility-index anchor  >  realized vol x VRP
     The anchor is the high-value path: VIX *is* 30-day ATM implied vol on the
     S&P, it is a real quoted instrument Schwab's /pricehistory serves like any
     other symbol, and it reaches back to 1990. That gives an OBSERVED,
     forward-looking vol series covering 1998, 2008 and 2020 — every regime a
     realized-vol model would smooth into mush.

  2. TERM — sigma_atm(T) = sigma_30 * (T/30)^h. Normally mildly upward-sloping.

  3. SKEW — in standardised log-moneyness m = ln(K/F)/(sigma_atm*sqrt(T)),
       sigma(K,T) = sigma_atm(T) * (1 + b*m + c*m^2)
     Not a detail: a 25-delta SPY put routinely trades 4-6 vol points above
     ATM. A flat surface makes protective puts cheap and OTM calls rich, which
     would systematically distort put-buying — and puts are half of what this
     feature is for (plan/16 D2).

  4. CLAMPS — the wings extrapolate, so the result is bounded both relative to
     ATM and absolutely. An unclamped quadratic skew goes negative eventually,
     and a negative vol is an arbitrage the backtest would happily "harvest".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Volatility-index anchors: underlying -> the index whose close IS that
#: underlying's 30-day ATM implied vol. Backfilled through the ordinary
#: CandleStore like any other symbol.
VOL_INDEX_ANCHORS: dict[str, str] = {
    "SPY": "$VIX", "SPX": "$VIX", "VOO": "$VIX", "IVV": "$VIX", "SSO": "$VIX",
    "UPRO": "$VIX", "SPXL": "$VIX", "SH": "$VIX", "SDS": "$VIX", "SPXU": "$VIX",
    "QQQ": "$VXN", "NDX": "$VXN", "TQQQ": "$VXN", "SQQQ": "$VXN", "QLD": "$VXN",
    "IWM": "$RVX", "TNA": "$RVX", "TZA": "$RVX",
    "DIA": "$VXD",
}

#: The index used to scale any underlying with no anchor of its own.
REFERENCE_VOL_INDEX = "$VIX"

#: Defaults, applied when calibration has not produced parameters for an
#: underlying. Index/ETF products have materially steeper skew than single
#: names, so the two get different defaults rather than one blended compromise.
DEFAULT_SKEW_B_INDEX = -0.12
DEFAULT_SKEW_C_INDEX = 0.06
DEFAULT_SKEW_B_SINGLE = -0.06
DEFAULT_SKEW_C_SINGLE = 0.04
DEFAULT_TERM_SLOPE = 0.03
DEFAULT_VRP_MULT = 1.15

#: Products treated as index-like for the skew default.
_INDEX_LIKE = frozenset({
    "SPY", "SPX", "VOO", "IVV", "QQQ", "NDX", "IWM", "DIA", "EEM", "EFA",
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLP", "XLU", "XLY", "XLB", "SMH",
    "SOXX", "TLT", "GLD", "SLV", "HYG", "LQD", "VTI", "RSP",
    "TQQQ", "SQQQ", "SOXL", "SOXS", "UPRO", "SPXU", "SPXL", "TNA", "TZA",
    "QLD", "SSO", "SDS", "SH", "UVXY", "VXX", "SVXY",
})

#: Absolute bounds on any returned volatility. 2% is below anything liquid;
#: 500% is above anything sane. Both exist so a far-wing extrapolation cannot
#: produce a nonsense price that a backtest then treats as an opportunity.
MIN_VOL = 0.02
MAX_VOL = 5.0
#: Bounds relative to ATM, which catch a bad skew fit long before the absolute
#: clamps would.
MIN_REL_TO_ATM = 0.40
MAX_REL_TO_ATM = 3.00

#: Standardised-moneyness saturation point for the skew (see
#: `skew_multiplier`). Beyond this many standard deviations the smile flattens
#: instead of continuing to curve upward. This is what makes the surface
#: arbitrage-free; do not raise it without re-running the arbitrage tests in
#: tests/test_volsurface.py, which are what caught the unsaturated version.
SKEW_SATURATION = 3.0

#: EWMA decay for the realized-vol fallback (RiskMetrics convention).
EWMA_LAMBDA = 0.94
TRADING_DAYS = 252.0


@dataclass(frozen=True)
class SurfaceParams:
    """Per-underlying surface shape. Defaults are the documented fallbacks;
    `calibrated` records whether these came from real recorded chains, and is
    carried into the backtest's assumptions block so a user can tell a fitted
    surface from a guessed one."""

    underlying: str
    atm_iv_beta: float = 1.0        # sigma_atm = beta * anchor_index_vol
    term_slope: float = DEFAULT_TERM_SLOPE
    skew_b: float = DEFAULT_SKEW_B_INDEX
    skew_c: float = DEFAULT_SKEW_C_INDEX
    vrp_mult: float = DEFAULT_VRP_MULT
    calibrated: bool = False
    n_obs: int = 0
    rmse: float | None = None

    @classmethod
    def default_for(cls, underlying: str, *, vrp_mult: float = DEFAULT_VRP_MULT
                    ) -> "SurfaceParams":
        u = (underlying or "").strip().upper()
        index_like = u in _INDEX_LIKE
        return cls(
            underlying=u,
            skew_b=DEFAULT_SKEW_B_INDEX if index_like else DEFAULT_SKEW_B_SINGLE,
            skew_c=DEFAULT_SKEW_C_INDEX if index_like else DEFAULT_SKEW_C_SINGLE,
            vrp_mult=vrp_mult)

    def to_dict(self) -> dict:
        return {"underlying": self.underlying, "atm_iv_beta": self.atm_iv_beta,
                "term_slope": self.term_slope, "skew_b": self.skew_b,
                "skew_c": self.skew_c, "vrp_mult": self.vrp_mult,
                "calibrated": self.calibrated, "n_obs": self.n_obs,
                "rmse": self.rmse}


def anchor_for(underlying: str) -> str | None:
    """The volatility index that IS this underlying's ATM implied vol, if one
    exists. Leveraged ETFs map to their parent index's anchor — TQQQ's own vol
    is roughly 3x QQQ's, which `atm_iv_beta` handles, but the SHAPE of the
    regime is the parent's."""
    return VOL_INDEX_ANCHORS.get((underlying or "").strip().upper())


def realized_vol_ewma(closes, *, lam: float = EWMA_LAMBDA) -> float | None:
    """Annualized EWMA close-to-close volatility.

    The LAST-RESORT level input. Whatever this returns is multiplied by the VRP
    (see `atm_vol`) precisely because realized vol systematically understates
    what options trade at."""
    if closes is None or len(closes) < 3:
        return None
    var = None
    prev = None
    for c in closes:
        c = float(c)
        if c <= 0 or c != c:
            continue
        if prev is None:
            prev = c
            continue
        ret = math.log(c / prev)
        prev = c
        var = ret * ret if var is None else lam * var + (1 - lam) * ret * ret
    if var is None or var <= 0:
        return None
    return math.sqrt(var * TRADING_DAYS)


def atm_vol(*, recorded_iv: float | None = None,
            anchor_vol: float | None = None,
            realized: float | None = None,
            params: SurfaceParams | None = None) -> tuple[float | None, str]:
    """The 30-day ATM volatility level, plus WHICH source produced it.

    The source string is not decoration: it is reported in every backtest's
    assumptions block, because "this result came from recorded IV" and "this
    result came from realized vol times a guess" deserve very different levels
    of trust.

    Priority: recorded > anchor index > realized x VRP.
    """
    p = params or SurfaceParams.default_for("")
    if recorded_iv is not None and recorded_iv > 0:
        return _clamp_abs(recorded_iv), "recorded"
    if anchor_vol is not None and anchor_vol > 0:
        # The anchor is already an IMPLIED vol, so the variance risk premium is
        # inside it — applying the VRP again here would double-count it and
        # make every option too expensive.
        return _clamp_abs(anchor_vol * p.atm_iv_beta), "vol_index_anchor"
    if realized is not None and realized > 0:
        # Realized vol is NOT implied vol. This is the multiplier that stops a
        # long-premium strategy from manufacturing profit out of options that
        # never traded that cheap (plan/16 D6).
        return _clamp_abs(realized * p.vrp_mult), "realized_ewma_x_vrp"
    return None, "unavailable"


def term_vol(atm_30: float, dte: float, params: SurfaceParams) -> float:
    """Scale the 30-day level to this contract's tenor."""
    d = max(float(dte), 1.0)
    return _clamp_abs(atm_30 * (d / 30.0) ** params.term_slope)


def standardized_moneyness(S: float, K: float, T: float, r: float, q: float,
                           atm_sigma: float) -> float:
    """m = ln(K/F) / (sigma*sqrt(T)), with F the forward.

    Expressing skew in these units rather than in raw strike distance is what
    makes ONE set of (b, c) parameters describe a 7-day and a 180-day
    expiration at the same time: a $10 strike move is a big deal on a 7-day
    option and noise on a 180-day one, but both are the same number of standard
    deviations."""
    if S <= 0 or K <= 0:
        return 0.0
    T = max(T, 1e-6)
    sigma = max(atm_sigma, 1e-6)
    fwd = S * math.exp((r - q) * T)
    return math.log(K / fwd) / (sigma * math.sqrt(T))


def skew_multiplier(m: float, params: SurfaceParams) -> float:
    """1 + b*m' + c*m'^2, where m' is a SATURATED moneyness.

    The saturation is not cosmetic — it is what makes the surface
    arbitrage-free, and it was added because the naive quadratic demonstrably
    was not.

    A raw quadratic keeps growing without bound in the wings: at |m| ~ 8 the
    default parameters ask for ~4x the ATM volatility, which makes a far-OTM
    call MORE expensive than a nearer one. That is a butterfly arbitrage, and a
    backtest run on such a surface will find and "harvest" it enthusiastically
    — profit that is purely a pricing bug. Measured across a grid of vol levels
    (5%-200%), tenors (1-730 DTE) and strikes (+/-60%), the unsaturated form
    produced up to 164 violations; the saturated form produces zero.

    Real smiles behave this way too: they flatten in the wings rather than
    curving upward forever, so this is closer to the market as well as safer.
    `tanh` is used because it is smooth (no kink to confuse a delta-based
    strike search) and saturates at exactly +/-SKEW_SATURATION.

    Near the money the correction is negligible — at |m| = 1 the compression is
    ~3% — so the fitted skew that matters is preserved intact."""
    m_eff = SKEW_SATURATION * math.tanh(m / SKEW_SATURATION)
    mult = 1.0 + params.skew_b * m_eff + params.skew_c * m_eff * m_eff
    return min(max(mult, MIN_REL_TO_ATM), MAX_REL_TO_ATM)


def _clamp_abs(v: float) -> float:
    return min(max(v, MIN_VOL), MAX_VOL)


@dataclass
class VolSurface:
    """A fully-specified surface for one underlying at one point in time.

    Built per bar by the pricer. `atm_30` is the level, `params` the shape, and
    `source` records where the level came from."""

    underlying: str
    atm_30: float
    params: SurfaceParams
    source: str = "unknown"
    r: float = 0.04
    q: float = 0.0
    _cache: dict = field(default_factory=dict, repr=False)

    def sigma(self, S: float, K: float, T: float, dte: float) -> float:
        """sigma(K, T) — the number Black-Scholes actually consumes."""
        atm_t = term_vol(self.atm_30, dte, self.params)
        m = standardized_moneyness(S, K, T, self.r, self.q, atm_t)
        return _clamp_abs(atm_t * skew_multiplier(m, self.params))

    def to_dict(self) -> dict:
        return {"underlying": self.underlying, "atm_30": self.atm_30,
                "source": self.source, "r": self.r, "q": self.q,
                **self.params.to_dict()}


# ---- arbitrage sanity -----------------------------------------------------

def check_butterfly_arbitrage(surface: VolSurface, S: float, strikes: list[float],
                              T: float, dte: float, right: str = "C",
                              tol: float = 1e-6) -> list[str]:
    """Call prices must be CONVEX and decreasing in strike.

    A surface that violates this admits a butterfly arbitrage, and a backtest
    run on it can discover "edge" that is purely a pricing bug — a documented
    and very real failure mode of synthetic options backtesting. Returns a list
    of human-readable violations (empty = clean)."""
    from .blackscholes import price

    problems: list[str] = []
    ks = sorted(k for k in strikes if k > 0)
    pxs = [price(S, k, T, surface.r, surface.q, surface.sigma(S, k, T, dte), right)
           for k in ks]

    for i in range(len(ks) - 1):
        if right == "C" and pxs[i + 1] > pxs[i] + tol:
            problems.append(
                f"call price rises with strike: K={ks[i]:.2f} -> {pxs[i]:.4f}, "
                f"K={ks[i + 1]:.2f} -> {pxs[i + 1]:.4f}")
        if right == "P" and pxs[i + 1] < pxs[i] - tol:
            problems.append(
                f"put price falls with strike: K={ks[i]:.2f} -> {pxs[i]:.4f}, "
                f"K={ks[i + 1]:.2f} -> {pxs[i + 1]:.4f}")

    for i in range(1, len(ks) - 1):
        # a butterfly must cost something: C(K-) - 2C(K) + C(K+) >= 0
        fly = pxs[i - 1] - 2 * pxs[i] + pxs[i + 1]
        if fly < -tol:
            problems.append(
                f"negative butterfly at K={ks[i]:.2f}: {fly:.6f} "
                f"(strikes {ks[i - 1]:.2f}/{ks[i]:.2f}/{ks[i + 1]:.2f})")
    return problems


def check_calendar_arbitrage(surface: VolSurface, S: float, K: float,
                             dtes: list[float], tol: float = 1e-9) -> list[str]:
    """Total variance sigma^2 * T must be NON-DECREASING in T.

    If a longer-dated option carries less total variance than a shorter one,
    the calendar spread is free money — again, an artefact a backtest would
    happily trade."""
    problems: list[str] = []
    prev_var = None
    prev_dte = None
    for dte in sorted(d for d in dtes if d > 0):
        T = dte / 365.0
        sig = surface.sigma(S, K, T, dte)
        total_var = sig * sig * T
        if prev_var is not None and total_var < prev_var - tol:
            problems.append(
                f"total variance falls from {prev_dte:.0f}d ({prev_var:.6f}) to "
                f"{dte:.0f}d ({total_var:.6f}) at K={K:.2f}")
        prev_var, prev_dte = total_var, dte
    return problems
