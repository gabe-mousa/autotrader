"""Calibration and the model-error report.

Two jobs, of which the second is the more important.

(a) FIT. Given recorded chain snapshots, estimate the surface and spread
    parameters for an underlying by least squares, and store them so the
    synthetic pricer stops guessing.

(b) THE HONESTY REPORT. Replay the synthetic pricer over every recorded
    snapshot and measure how wrong it was, bucketed by tenor and moneyness.

(b) is what makes the whole synthetic-backtest approach defensible rather than
hand-wavy. Without it, "modelled prices are approximate" is an unfalsifiable
disclaimer; with it, the user gets a number — "median 8% error at 21-45 DTE and
0.15-0.35 delta" — that can be applied as a haircut, or used to conclude that a
strategy living in a badly-modelled bucket should not be trusted at all.

The fit is deliberately simple: a small number of parameters, closed-form or
coarse-grid estimation, no optimiser dependency, fully deterministic. An
elaborate fit on thin data would produce confident nonsense, which is the
failure this whole file exists to detect rather than commit.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..logging import get_logger
from .blackscholes import implied_vol
from .expiry import ms_to_expiry, year_fraction
from .spread import SpreadParams, tier_for
from .symbology import InvalidOccSymbol, is_occ, parse_occ
from .volsurface import (DEFAULT_VRP_MULT, SurfaceParams, VolSurface,
                         standardized_moneyness)

ET = ZoneInfo("America/New_York")

log = get_logger("options.calibrate")

#: Minimum contract-observations before a fit is written. A bad fit is worse
#: than the documented default, because it carries the authority of having been
#: "calibrated" while being pure noise.
MIN_OBS = 2_000

#: Buckets for the error report. Chosen to match how strategies actually
#: select contracts (plan/16 §7: dte_target and delta_target), so a user can
#: look up the bucket their strategy trades in.
DTE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0-7", 0, 7), ("7-21", 7, 21), ("21-45", 21, 45),
    ("45-90", 45, 90), ("90+", 90, 10_000),
)
DELTA_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0.00-0.15", 0.0, 0.15), ("0.15-0.35", 0.15, 0.35),
    ("0.35-0.65", 0.35, 0.65), ("0.65-1.00", 0.65, 1.01),
)


def _bucket(value: float, buckets) -> str:
    for name, lo, hi in buckets:
        if lo <= value < hi:
            return name
    return buckets[-1][0]


@dataclass
class FitResult:
    underlying: str
    surface: SurfaceParams
    spread: SpreadParams
    n_obs: int
    rmse: float | None
    written: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {"underlying": self.underlying, "n_obs": self.n_obs,
                "rmse": self.rmse, "written": self.written, "reason": self.reason,
                "surface": self.surface.to_dict(), "spread": self.spread.to_dict()}


def _rows_to_observations(rows, *, r: float, q: float) -> list[dict]:
    """Recorded rows -> (moneyness, iv, dte, spread_pct) observations.

    Rows whose IV is missing are re-derived from the mid price; rows where IV
    is unrecoverable (no extrinsic value) are DROPPED rather than defaulted,
    because `implied_vol` returning None means exactly that and substituting a
    number would corrupt the fit."""
    obs: list[dict] = []
    for row in rows:
        sym = row.get("symbol") or ""
        if not is_occ(sym):
            continue
        try:
            occ = parse_occ(sym)
        except InvalidOccSymbol:
            continue
        S = _f(row.get("underlying_px"))
        if not S or S <= 0:
            continue
        ts = int(row["ts"])
        T = year_fraction(occ.expiry, ts)
        dte = max((occ.expiry - ms_to_expiry(ts)).days, 0)
        K = float(occ.strike)

        iv = _f(row.get("iv"))
        if not iv or iv <= 0:
            bid, ask = _f(row.get("bid")), _f(row.get("ask"))
            mid = _f(row.get("mark")) or ((bid + ask) / 2 if bid and ask else None)
            if not mid or mid <= 0:
                continue
            iv = implied_vol(mid, S, K, T, r, q, occ.right)
            if iv is None:
                continue      # unidentifiable — see blackscholes.implied_vol
        if not (0.01 < iv < 5.0):
            continue

        bid, ask = _f(row.get("bid")), _f(row.get("ask"))
        mid = _f(row.get("mark")) or ((bid + ask) / 2 if bid and ask else 0.0)
        sp = ((ask - bid) / mid) if (mid and mid > 0 and ask and bid is not None) else None

        obs.append({"m_raw": math.log(K / (S * math.exp((r - q) * T))),
                    "iv": iv, "dte": dte, "T": T, "S": S, "K": K,
                    "right": occ.right, "spread_pct": sp,
                    "delta": abs(_f(row.get("delta")) or 0.0), "ts": ts})
    return obs


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def fit_surface(observations: list[dict], underlying: str, *,
                vrp_mult: float = DEFAULT_VRP_MULT) -> SurfaceParams:
    """Estimate (atm_iv_beta placeholder, term_slope, skew_b, skew_c).

    Method: take near-the-money observations to pin the level per tenor, fit
    the term slope in log-log space, then fit the skew quadratic in
    standardised moneyness by ordinary least squares. Deliberately closed-form
    — no optimiser, fully deterministic, and it cannot diverge on thin data."""
    defaults = SurfaceParams.default_for(underlying, vrp_mult=vrp_mult)
    if len(observations) < 50:
        return defaults

    # ---- level per tenor, from near-the-money contracts ----
    by_tenor: dict[int, list[float]] = {}
    for o in observations:
        if abs(o["m_raw"]) < 0.02:          # within 2% of the forward
            by_tenor.setdefault(o["dte"], []).append(o["iv"])
    tenor_atm = {d: sorted(v)[len(v) // 2] for d, v in by_tenor.items() if len(v) >= 3}

    term_slope = defaults.term_slope
    if len(tenor_atm) >= 3:
        xs = [math.log(max(d, 1) / 30.0) for d in tenor_atm]
        ys = [math.log(max(v, 1e-6)) for v in tenor_atm.values()]
        term_slope = _ols_slope(xs, ys)
        # a term slope outside this range is a fit artefact, not a market
        term_slope = min(max(term_slope, -0.35), 0.35)

    # reference 30-day ATM level for standardising moneyness
    atm30 = tenor_atm.get(30) or (sorted(tenor_atm.values())[len(tenor_atm) // 2]
                                  if tenor_atm else 0.20)

    # ---- skew: iv/atm_t - 1 ~ b*m + c*m^2 ----
    xs_b: list[float] = []
    xs_c: list[float] = []
    ys: list[float] = []
    for o in observations:
        atm_t = atm30 * (max(o["dte"], 1) / 30.0) ** term_slope
        if atm_t <= 0:
            continue
        m = standardized_moneyness(o["S"], o["K"], o["T"], 0.0, 0.0, atm_t)
        if abs(m) > 4.0:
            continue                      # wings are saturated anyway
        xs_b.append(m)
        xs_c.append(m * m)
        ys.append(o["iv"] / atm_t - 1.0)

    skew_b, skew_c = defaults.skew_b, defaults.skew_c
    if len(ys) >= 100:
        b, c = _ols2(xs_b, xs_c, ys)
        if b is not None:
            # sanity bounds: a positive skew_b on an equity underlying means the
            # fit found upside calls bid over downside puts, which happens in a
            # squeeze but is not a surface to extrapolate from
            skew_b = min(max(b, -0.60), 0.20)
            skew_c = min(max(c, -0.10), 0.40)

    rmse = None
    if ys:
        resid = [y - (skew_b * mb + skew_c * mc)
                 for y, mb, mc in zip(ys, xs_b, xs_c)]
        rmse = math.sqrt(sum(e * e for e in resid) / len(resid))

    return SurfaceParams(
        underlying=(underlying or "").strip().upper(),
        atm_iv_beta=defaults.atm_iv_beta, term_slope=term_slope,
        skew_b=skew_b, skew_c=skew_c, vrp_mult=vrp_mult,
        calibrated=True, n_obs=len(observations), rmse=rmse)


def fit_spread(observations: list[dict], underlying: str) -> SpreadParams:
    """Estimate (a, b, c) in spread_pct = a + b*|m| + c/sqrt(dte)."""
    defaults = SpreadParams.default_for(underlying)
    rows = [(abs(o["m_raw"]), o["dte"], o["spread_pct"]) for o in observations
            if o.get("spread_pct") is not None and 0 < o["spread_pct"] < 2.0]
    if len(rows) < 100:
        return defaults

    x1 = [r[0] for r in rows]
    x2 = [1.0 / math.sqrt(max(r[1], 1)) for r in rows]
    y = [r[2] for r in rows]
    a, b, c = _ols3(x1, x2, y)
    if a is None:
        return defaults
    return SpreadParams(
        underlying=(underlying or "").strip().upper(),
        a=min(max(a, 0.0005), 0.30), b=min(max(b, 0.0), 0.30),
        c=min(max(c, 0.0), 0.30), tier=tier_for(underlying),
        calibrated=True, n_obs=len(rows))


# ---- small deterministic least-squares helpers ----------------------------

def _ols_slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den > 1e-12 else 0.0


def _ols2(x1: list[float], x2: list[float], y: list[float]):
    """Two-regressor OLS through the origin (the intercept is pinned to 0 by
    construction: at m=0 the multiplier must be exactly 1)."""
    s11 = sum(a * a for a in x1)
    s22 = sum(b * b for b in x2)
    s12 = sum(a * b for a, b in zip(x1, x2))
    s1y = sum(a * v for a, v in zip(x1, y))
    s2y = sum(b * v for b, v in zip(x2, y))
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-12:
        return None, None
    return (s22 * s1y - s12 * s2y) / det, (s11 * s2y - s12 * s1y) / det


def _ols3(x1: list[float], x2: list[float], y: list[float]):
    """Two regressors WITH an intercept (the ATM long-dated floor)."""
    n = len(y)
    if n < 3:
        return None, None, None
    mx1, mx2, my = sum(x1) / n, sum(x2) / n, sum(y) / n
    c11 = sum((a - mx1) ** 2 for a in x1)
    c22 = sum((b - mx2) ** 2 for b in x2)
    c12 = sum((a - mx1) * (b - mx2) for a, b in zip(x1, x2))
    c1y = sum((a - mx1) * (v - my) for a, v in zip(x1, y))
    c2y = sum((b - mx2) * (v - my) for b, v in zip(x2, y))
    det = c11 * c22 - c12 * c12
    if abs(det) < 1e-12:
        return None, None, None
    b1 = (c22 * c1y - c12 * c2y) / det
    b2 = (c11 * c2y - c12 * c1y) / det
    return my - b1 * mx1 - b2 * mx2, b1, b2


# ---- the model-error report -----------------------------------------------

def error_report(observations: list[dict], surface: VolSurface) -> dict:
    """How wrong the synthetic pricer is against recorded reality.

    Errors are reported in VOL POINTS and as a percentage of the observed IV,
    bucketed by tenor and moneyness. The percentage is the number a user can
    apply as a haircut; the vol points are what a trader will recognise."""
    from .blackscholes import price as bs_price

    buckets: dict[str, list[float]] = {}
    px_buckets: dict[str, list[float]] = {}
    tod_buckets: dict[str, list[float]] = {}
    tod_px_buckets: dict[str, list[float]] = {}
    all_iv_err: list[float] = []
    all_px_err: list[float] = []

    for o in observations:
        model_iv = surface.sigma(o["S"], o["K"], o["T"], o["dte"])
        iv_err = model_iv - o["iv"]
        key = f"{_bucket(o['dte'], DTE_BUCKETS)} | {_bucket(o['delta'], DELTA_BUCKETS)}"
        buckets.setdefault(key, []).append(iv_err)
        all_iv_err.append(iv_err)
        tod = _et_slot(o.get("ts"))
        if tod:
            tod_buckets.setdefault(tod, []).append(iv_err)

        obs_px = bs_price(o["S"], o["K"], o["T"], surface.r, surface.q,
                          o["iv"], o["right"])
        model_px = bs_price(o["S"], o["K"], o["T"], surface.r, surface.q,
                            model_iv, o["right"])
        if obs_px > 0.01:
            pct = (model_px - obs_px) / obs_px * 100.0
            px_buckets.setdefault(key, []).append(pct)
            all_px_err.append(pct)
            if tod:
                tod_px_buckets.setdefault(tod, []).append(pct)

    rows = []
    for key in sorted(buckets):
        ivs = buckets[key]
        pxs = px_buckets.get(key, [])
        rows.append({
            "bucket": key, "n": len(ivs),
            "median_abs_iv_error_pts": round(_median([abs(e) for e in ivs]) * 100, 3),
            "bias_iv_pts": round(_median(ivs) * 100, 3),
            "median_abs_price_error_pct": round(_median([abs(e) for e in pxs]), 2)
            if pxs else None,
            "p90_abs_price_error_pct": round(_pct(sorted(abs(e) for e in pxs), 90), 2)
            if pxs else None,
            "bias_price_pct": round(_median(pxs), 2) if pxs else None,
        })

    return {
        "underlying": surface.underlying,
        "n_observations": len(observations),
        "overall": {
            "median_abs_iv_error_pts": round(_median([abs(e) for e in all_iv_err]) * 100, 3)
            if all_iv_err else None,
            "median_abs_price_error_pct": round(_median([abs(e) for e in all_px_err]), 2)
            if all_px_err else None,
            "bias_price_pct": round(_median(all_px_err), 2) if all_px_err else None,
        },
        "buckets": rows,
        "by_time_of_day": _time_of_day_rows(tod_buckets, tod_px_buckets),
        "surface": surface.to_dict(),
    }


def _et_slot(ts) -> str | None:
    """Snapshot timestamp -> "HH:MM" ET."""
    try:
        return dt.datetime.fromtimestamp(int(ts) / 1000, tz=ET).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return None


def _time_of_day_rows(tod: dict[str, list[float]],
                      tod_px: dict[str, list[float]]) -> list[dict]:
    """Model error broken out by the ET time the chain was recorded at.

    This is the row a user needs before standing behind an INTRADAY backtest.
    A single number for the whole day hides exactly the thing that matters: the
    pricer is fitted mostly to whatever hours were recorded most, so if the only
    chains are 15:50 chains, the 15:50 error looks great and the 09:45 error is
    unmeasured rather than small.

    An empty list here means one of two things, and they are NOT the same: no
    calibration data at all, or data from a single time of day. `n` per row is
    what distinguishes them, so it is always reported."""
    out = []
    for slot in sorted(tod):
        ivs, pxs = tod[slot], tod_px.get(slot, [])
        out.append({
            "et_time": slot, "n": len(ivs),
            "median_abs_iv_error_pts": round(_median([abs(e) for e in ivs]) * 100, 3),
            "median_abs_price_error_pct": round(_median([abs(e) for e in pxs]), 2)
            if pxs else None,
            "bias_price_pct": round(_median(pxs), 2) if pxs else None,
        })
    return out


def _median(xs) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _pct(sorted_xs, p: float) -> float:
    if not sorted_xs:
        return 0.0
    k = max(0, min(len(sorted_xs) - 1, int(round(p / 100 * (len(sorted_xs) - 1)))))
    return sorted_xs[k]


# ---- orchestration --------------------------------------------------------

class Calibrator:
    """Reads recorded snapshots, fits, stores, and reports."""

    def __init__(self, db, store, *, r: float = 0.04):
        self._db = db
        self._store = store
        self._r = r

    async def _observations(self, underlying: str, *, lookback_days: int = 180,
                            q: float = 0.0) -> list[dict]:
        now = int(time.time() * 1000)
        rows = await self._db.fetch_all(
            "SELECT symbol, ts, bid, ask, mark, iv, delta, underlying_px, dte "
            "FROM option_chain_snapshots WHERE underlying=? AND ts>=? ORDER BY ts",
            (underlying.strip().upper(), now - lookback_days * 86_400_000),
        )
        return _rows_to_observations([dict(r) for r in rows], r=self._r, q=q)

    async def fit(self, underlying: str, *, lookback_days: int = 180,
                  vrp_mult: float = DEFAULT_VRP_MULT,
                  min_obs: int = MIN_OBS) -> FitResult:
        u = underlying.strip().upper()
        obs = await self._observations(u, lookback_days=lookback_days)
        surface = fit_surface(obs, u, vrp_mult=vrp_mult)
        spread = fit_spread(obs, u)

        if len(obs) < min_obs:
            # A bad fit is WORSE than the documented default: it carries the
            # authority of having been calibrated while being pure noise.
            return FitResult(u, SurfaceParams.default_for(u, vrp_mult=vrp_mult),
                             SpreadParams.default_for(u), len(obs), None, False,
                             f"only {len(obs)} observations, need {min_obs}")

        await self._db.execute(
            "INSERT INTO option_surface_params (underlying, as_of, params_json) "
            "VALUES (?,?,?) ON CONFLICT(underlying, as_of) DO UPDATE SET "
            "params_json=excluded.params_json",
            (u, int(time.time() * 1000),
             json.dumps({"surface": surface.to_dict(), "spread": spread.to_dict()})),
        )
        log.info("surface_calibrated", underlying=u, n_obs=len(obs),
                 skew_b=surface.skew_b, skew_c=surface.skew_c,
                 term_slope=surface.term_slope)
        return FitResult(u, surface, spread, len(obs), surface.rmse, True)

    async def load_params(self, underlying: str, as_of_ms: int | None = None
                          ) -> tuple[SurfaceParams, SpreadParams]:
        """Newest fit at or before `as_of_ms`, or the documented defaults.

        The as-of constraint matters: applying today's fitted parameters to a
        2008 backtest would be a subtle form of look-ahead."""
        u = underlying.strip().upper()
        if as_of_ms is None:
            as_of_ms = int(time.time() * 1000)
        row = await self._db.fetch_one(
            "SELECT params_json FROM option_surface_params "
            "WHERE underlying=? AND as_of<=? ORDER BY as_of DESC LIMIT 1",
            (u, as_of_ms),
        )
        if not row:
            return SurfaceParams.default_for(u), SpreadParams.default_for(u)
        try:
            data = json.loads(row["params_json"])
            s = data.get("surface") or {}
            p = data.get("spread") or {}
            return (
                SurfaceParams(underlying=u, atm_iv_beta=s.get("atm_iv_beta", 1.0),
                              term_slope=s.get("term_slope", 0.03),
                              skew_b=s.get("skew_b", -0.12),
                              skew_c=s.get("skew_c", 0.06),
                              vrp_mult=s.get("vrp_mult", DEFAULT_VRP_MULT),
                              calibrated=True, n_obs=s.get("n_obs", 0),
                              rmse=s.get("rmse")),
                SpreadParams(underlying=u, a=p.get("a", 0.01), b=p.get("b", 0.02),
                             c=p.get("c", 0.02), tier=p.get("tier", tier_for(u)),
                             calibrated=True, n_obs=p.get("n_obs", 0)),
            )
        except (ValueError, TypeError) as e:
            log.warning("surface_params_unreadable", underlying=u, error=str(e))
            return SurfaceParams.default_for(u), SpreadParams.default_for(u)

    async def report(self, underlying: str, *, lookback_days: int = 180,
                     atm_30: float | None = None) -> dict:
        """The honesty report (see module docstring)."""
        u = underlying.strip().upper()
        obs = await self._observations(u, lookback_days=lookback_days)
        if not obs:
            return {"underlying": u, "n_observations": 0, "buckets": [],
                    "overall": {}, "note": "no recorded chain data for this underlying yet"}
        surface_params, _ = await self.load_params(u)
        if atm_30 is None:
            near = sorted(o["iv"] for o in obs if abs(o["m_raw"]) < 0.02)
            atm_30 = near[len(near) // 2] if near else 0.20
        surface = VolSurface(underlying=u, atm_30=atm_30, params=surface_params,
                             source="calibration_replay", r=self._r)
        return error_report(obs, surface)
