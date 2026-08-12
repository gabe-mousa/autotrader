"""Calibration + the model-error report.

The report is the deliverable that makes synthetic backtesting defensible: it
turns "modelled prices are approximate" from an unfalsifiable disclaimer into a
number a user can apply as a haircut. So the tests check that it actually
recovers a KNOWN surface from synthetic-but-realistic data, and that a thin or
absent dataset refuses to produce a confident-looking fit."""

from __future__ import annotations

import datetime as dt

import pytest

from app.db import Database
from app.options.blackscholes import price as bs_price
from app.options.calibrate import (MIN_OBS, Calibrator, error_report, fit_spread,
                                   fit_surface, _rows_to_observations)
from app.options.expiry import expiry_to_ms
from app.options.spread import SpreadParams, quote_from_mid, spread_pct
from app.options.store import OptionChainStore, SnapshotRow
from app.options.symbology import format_occ
from app.options.calibrate import ET
from app.options.volsurface import SurfaceParams, VolSurface, standardized_moneyness

R = 0.04
UNDERLYING = "SPY"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


def build_chain_rows(*, as_of: dt.date, S: float = 672.0, atm30: float = 0.18,
                     skew_b: float = -0.12, skew_c: float = 0.06,
                     term_slope: float = 0.03, spread_a: float = 0.004,
                     n_strikes: int = 25) -> list[SnapshotRow]:
    """Generate a realistic chain from a KNOWN surface, so the fit has a truth
    to be measured against."""
    truth = VolSurface(
        underlying=UNDERLYING, atm_30=atm30, r=R, source="truth",
        params=SurfaceParams(underlying=UNDERLYING, skew_b=skew_b, skew_c=skew_c,
                             term_slope=term_slope))
    spread_params = SpreadParams(underlying=UNDERLYING, a=spread_a, b=0.010,
                                 c=0.010, tier=1)
    ts = expiry_to_ms(as_of)
    rows: list[SnapshotRow] = []
    for dte in (7, 14, 30, 45, 60, 90):
        expiry = as_of + dt.timedelta(days=dte)
        T = dte / 365.0
        for i in range(-n_strikes, n_strikes + 1):
            K = round(S + i * 5.0)
            if K <= 0:
                continue
            sigma = truth.sigma(S, float(K), T, dte)
            for right in ("C", "P"):
                mid = bs_price(S, float(K), T, R, 0.0, sigma, right)
                if mid < 0.02:
                    continue
                m = standardized_moneyness(S, float(K), T, R, 0.0, sigma)
                bid, ask = quote_from_mid(mid, spread_params, m, dte)
                rows.append(SnapshotRow(
                    underlying=UNDERLYING, ts=ts,
                    symbol=format_occ(UNDERLYING, expiry, right, K),
                    expiry=expiry_to_ms(expiry), strike=float(K), opt_right=right,
                    bid=bid, ask=ask, mark=mid, iv=sigma, underlying_px=S,
                    dte=dte, multiplier=100.0,
                    delta=0.35, quote_ts=ts))
    return rows


def observations_from(rows: list[SnapshotRow]) -> list[dict]:
    return _rows_to_observations(
        [{"symbol": r.symbol, "ts": r.ts, "bid": r.bid, "ask": r.ask, "mark": r.mark,
          "iv": r.iv, "delta": r.delta, "underlying_px": r.underlying_px,
          "dte": r.dte} for r in rows], r=R, q=0.0)


# ---- observation extraction ----------------------------------------------

def test_observations_are_extracted_from_rows():
    rows = build_chain_rows(as_of=dt.date(2026, 7, 30))
    obs = observations_from(rows)
    assert len(obs) > 500
    for o in obs:
        assert o["iv"] > 0 and o["S"] > 0 and o["dte"] >= 0
        assert o["right"] in ("C", "P")


def test_observations_drop_unusable_rows():
    bad = [
        {"symbol": "garbage", "ts": 1, "iv": 0.2, "underlying_px": 100.0},
        {"symbol": format_occ("SPY", dt.date(2026, 9, 18), "C", 680), "ts": 1,
         "iv": 0.2, "underlying_px": 0.0},                       # no spot
        {"symbol": format_occ("SPY", dt.date(2026, 9, 18), "C", 680), "ts": 1,
         "iv": None, "mark": None, "bid": None, "ask": None,
         "underlying_px": 672.0},                                # nothing to infer from
        {"symbol": format_occ("SPY", dt.date(2026, 9, 18), "C", 680), "ts": 1,
         "iv": 99.0, "underlying_px": 672.0},                    # absurd iv
    ]
    assert _rows_to_observations(bad, r=R, q=0.0) == []


def test_iv_is_recovered_from_price_when_missing():
    """Some rows arrive without Schwab's own IV; it must be inverted from the
    mid rather than defaulted."""
    expiry = dt.date(2026, 9, 18)
    ts = expiry_to_ms(dt.date(2026, 8, 19))
    T = 30 / 365
    mid = bs_price(672.0, 680.0, T, R, 0.0, 0.22, "C")
    obs = _rows_to_observations([{
        "symbol": format_occ("SPY", expiry, "C", 680), "ts": ts, "iv": None,
        "mark": mid, "bid": mid - 0.05, "ask": mid + 0.05,
        "underlying_px": 672.0, "delta": 0.35}], r=R, q=0.0)
    assert len(obs) == 1
    assert obs[0]["iv"] == pytest.approx(0.22, abs=1e-3)


# ---- fitting recovers a known surface ------------------------------------

def test_fit_recovers_the_true_skew():
    rows = build_chain_rows(as_of=dt.date(2026, 7, 30), skew_b=-0.15, skew_c=0.05)
    fitted = fit_surface(observations_from(rows), UNDERLYING)
    assert fitted.calibrated
    assert fitted.skew_b == pytest.approx(-0.15, abs=0.04)
    assert fitted.skew_c == pytest.approx(0.05, abs=0.04)


def test_fit_recovers_the_true_term_slope():
    rows = build_chain_rows(as_of=dt.date(2026, 7, 30), term_slope=0.10)
    fitted = fit_surface(observations_from(rows), UNDERLYING)
    assert fitted.term_slope == pytest.approx(0.10, abs=0.05)


def test_fit_falls_back_to_defaults_on_thin_data():
    """An elaborate fit on thin data produces confident nonsense — exactly the
    failure this module exists to detect rather than commit."""
    fitted = fit_surface([], UNDERLYING)
    assert not fitted.calibrated
    assert fitted.skew_b == SurfaceParams.default_for(UNDERLYING).skew_b


def test_fit_clamps_absurd_parameters():
    """Bounds keep a pathological fit from producing an arbitrageable surface."""
    obs = [{"m_raw": 0.0, "iv": 0.2, "dte": 30, "T": 30 / 365, "S": 100.0,
            "K": 100.0, "right": "C", "spread_pct": 0.01, "delta": 0.5, "ts": 0}
           for _ in range(200)]
    for i, o in enumerate(obs):
        o["iv"] = 0.2 + (i % 7) * 3.0        # nonsense
    fitted = fit_surface(obs, UNDERLYING)
    assert -0.60 <= fitted.skew_b <= 0.20
    assert -0.10 <= fitted.skew_c <= 0.40
    assert -0.35 <= fitted.term_slope <= 0.35


def test_spread_fit_recovers_the_true_floor():
    rows = build_chain_rows(as_of=dt.date(2026, 7, 30), spread_a=0.012)
    fitted = fit_spread(observations_from(rows), UNDERLYING)
    assert fitted.calibrated
    assert spread_pct(fitted, 0.0, 180) == pytest.approx(0.012, abs=0.008)


def test_spread_fit_falls_back_on_thin_data():
    assert not fit_spread([], UNDERLYING).calibrated


# ---- the honesty report ---------------------------------------------------

def test_error_report_shows_near_zero_error_for_a_correct_surface():
    """Sanity floor: if the model IS the truth, the report must say so.
    A report that shows error where there is none is useless as a haircut."""
    rows = build_chain_rows(as_of=dt.date(2026, 7, 30))
    obs = observations_from(rows)
    truth = VolSurface(underlying=UNDERLYING, atm_30=0.18, r=R, source="truth",
                       params=SurfaceParams(underlying=UNDERLYING, skew_b=-0.12,
                                            skew_c=0.06, term_slope=0.03))
    rep = error_report(obs, truth)
    assert rep["n_observations"] == len(obs)
    assert rep["overall"]["median_abs_iv_error_pts"] < 0.5      # < half a vol point
    assert abs(rep["overall"]["bias_price_pct"]) < 2.0


def test_error_report_detects_a_wrong_surface():
    """A report that never shows error is worthless."""
    rows = build_chain_rows(as_of=dt.date(2026, 7, 30), atm30=0.18)
    obs = observations_from(rows)
    wrong = VolSurface(underlying=UNDERLYING, atm_30=0.35, r=R, source="wrong",
                       params=SurfaceParams.default_for(UNDERLYING))
    rep = error_report(obs, wrong)
    assert rep["overall"]["median_abs_iv_error_pts"] > 5.0
    assert rep["overall"]["median_abs_price_error_pct"] > 20.0


def test_error_report_buckets_by_tenor_and_delta():
    rows = build_chain_rows(as_of=dt.date(2026, 7, 30))
    rep = error_report(observations_from(rows),
                       VolSurface(underlying=UNDERLYING, atm_30=0.18, r=R,
                                  params=SurfaceParams.default_for(UNDERLYING)))
    assert rep["buckets"]
    for b in rep["buckets"]:
        assert "|" in b["bucket"]
        assert b["n"] > 0


# ---- Calibrator against the real store ------------------------------------

async def test_calibrator_refuses_to_write_a_fit_on_thin_data(db):
    store = OptionChainStore(db)
    await store.upsert_snapshot(build_chain_rows(as_of=dt.date.today())[:50])
    result = await Calibrator(db, store).fit(UNDERLYING)
    assert not result.written
    assert "observations" in result.reason
    # and the stored params must still be the documented defaults
    surface, _ = await Calibrator(db, store).load_params(UNDERLYING)
    assert not surface.calibrated


async def test_calibrator_writes_and_reloads_a_fit(db):
    store = OptionChainStore(db)
    # several days of chains to clear MIN_OBS
    today = dt.date.today()
    for i in range(12):
        await store.upsert_snapshot(
            build_chain_rows(as_of=today - dt.timedelta(days=i), n_strikes=30))
    cal = Calibrator(db, store)
    result = await cal.fit(UNDERLYING, min_obs=MIN_OBS)
    assert result.written, result.reason
    assert result.n_obs >= MIN_OBS

    surface, spread = await cal.load_params(UNDERLYING)
    assert surface.calibrated and spread.calibrated
    assert surface.skew_b == pytest.approx(result.surface.skew_b, abs=1e-9)


async def test_load_params_never_looks_ahead(db):
    """Applying today's fitted parameters to a 2008 backtest would be a subtle
    form of look-ahead."""
    store = OptionChainStore(db)
    today = dt.date.today()
    for i in range(12):
        await store.upsert_snapshot(
            build_chain_rows(as_of=today - dt.timedelta(days=i), n_strikes=30))
    cal = Calibrator(db, store)
    await cal.fit(UNDERLYING)

    old = expiry_to_ms(dt.date(2008, 1, 1))
    surface, _ = await cal.load_params(UNDERLYING, as_of_ms=old)
    assert not surface.calibrated, "a 2008 bar must not see a 2026 fit"


async def test_report_on_an_underlying_with_no_data(db):
    rep = await Calibrator(db, OptionChainStore(db)).report("NOPE")
    assert rep["n_observations"] == 0
    assert "note" in rep


async def test_report_end_to_end(db):
    store = OptionChainStore(db)
    today = dt.date.today()
    for i in range(5):
        await store.upsert_snapshot(build_chain_rows(as_of=today - dt.timedelta(days=i)))
    rep = await Calibrator(db, store).report(UNDERLYING)
    assert rep["n_observations"] > 0
    assert rep["buckets"]
    assert rep["overall"]["median_abs_iv_error_pts"] is not None


async def test_unreadable_stored_params_fall_back_to_defaults(db):
    await db.execute(
        "INSERT INTO option_surface_params (underlying, as_of, params_json) "
        "VALUES (?,?,?)", (UNDERLYING, 1, "{not json"))
    surface, spread = await Calibrator(db, OptionChainStore(db)).load_params(
        UNDERLYING, as_of_ms=10)
    assert not surface.calibrated and not spread.calibrated


def test_error_report_breaks_error_out_by_time_of_day():
    """The row an intraday backtest has to be defended with.

    A single whole-day error number hides the thing that matters: the pricer is
    fitted mostly to whatever hours were recorded most, so with EOD-only data
    the close looks accurate and the open is UNMEASURED rather than accurate."""
    rows = build_chain_rows(as_of=dt.date(2026, 7, 30))
    obs = observations_from(rows)
    # Same contracts observed at two different times of day, with the morning
    # chain genuinely 5 vol points higher than the model expects.
    morning = dt.datetime(2026, 7, 30, 9, 45, tzinfo=ET)
    close = dt.datetime(2026, 7, 30, 15, 50, tzinfo=ET)
    for o in obs:
        o["ts"] = int(morning.timestamp() * 1000)
        o["iv"] += 0.05
    late = []
    for o in obs:
        c = dict(o)
        c["ts"] = int(close.timestamp() * 1000)
        c["iv"] -= 0.05
        late.append(c)

    surface = VolSurface(underlying=UNDERLYING, atm_30=0.18, r=R, source="truth",
                         params=SurfaceParams(underlying=UNDERLYING, skew_b=-0.12,
                                              skew_c=0.06, term_slope=0.03))
    rep = error_report(obs + late, surface)
    by_tod = {r["et_time"]: r for r in rep["by_time_of_day"]}
    assert set(by_tod) == {"09:45", "15:50"}
    assert all(r["n"] > 0 for r in by_tod.values())
    # The mispriced hour must be visibly worse — that is the entire point.
    assert by_tod["09:45"]["median_abs_iv_error_pts"] > \
        by_tod["15:50"]["median_abs_iv_error_pts"] + 2.0


def test_time_of_day_report_is_empty_without_timestamps():
    """Absent, not zero: a missing breakdown must never read as 'no error'."""
    rows = build_chain_rows(as_of=dt.date(2026, 7, 30))
    obs = observations_from(rows)
    for o in obs:
        o.pop("ts", None)
    rep = error_report(obs, VolSurface(underlying=UNDERLYING, atm_30=0.18, r=R,
                                       params=SurfaceParams.default_for(UNDERLYING)))
    assert rep["by_time_of_day"] == []
