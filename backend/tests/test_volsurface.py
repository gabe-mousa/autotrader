"""Volatility surface.

Two things matter here above all: (1) the variance risk premium is actually
applied to realized vol and NOT double-applied to an implied anchor, because
that is the difference between an honest backtest and a fictional one; (2) the
surface admits no arbitrage, because a backtest run on an arbitrageable surface
finds "edge" that is purely a pricing bug."""

from __future__ import annotations

import math

import pytest

from app.options.volsurface import (DEFAULT_VRP_MULT, MAX_VOL, MIN_VOL,
                                    REFERENCE_VOL_INDEX, SurfaceParams, VolSurface,
                                    anchor_for, atm_vol, check_butterfly_arbitrage,
                                    check_calendar_arbitrage, realized_vol_ewma,
                                    skew_multiplier, standardized_moneyness, term_vol)


def mk(underlying="SPY", atm=0.18, **kw) -> VolSurface:
    return VolSurface(underlying=underlying, atm_30=atm,
                      params=SurfaceParams.default_for(underlying, **kw),
                      source="test")


# ---- anchors --------------------------------------------------------------

def test_vol_index_anchors():
    assert anchor_for("SPY") == "$VIX"
    assert anchor_for("spy") == "$VIX"
    assert anchor_for("QQQ") == "$VXN"
    assert anchor_for("IWM") == "$RVX"
    assert anchor_for("KO") is None
    assert REFERENCE_VOL_INDEX == "$VIX"


def test_leveraged_etfs_inherit_their_parent_index_anchor():
    """TQQQ's own vol is ~3x QQQ's, which atm_iv_beta scales — but the REGIME
    shape is the parent index's, so the anchor is $VXN."""
    assert anchor_for("TQQQ") == "$VXN"
    assert anchor_for("SQQQ") == "$VXN"
    assert anchor_for("SPXL") == "$VIX"


# ---- level: the VRP, which is the whole ballgame --------------------------

def test_realized_vol_gets_the_variance_risk_premium():
    """Options trade at IMPLIED vol, persistently above realized. Without this
    multiplier every synthetic long-premium strategy prints fake profit."""
    v, source = atm_vol(realized=0.20, params=SurfaceParams.default_for("SPY"))
    assert source == "realized_ewma_x_vrp"
    assert v == pytest.approx(0.20 * DEFAULT_VRP_MULT)
    assert v > 0.20


def test_anchor_does_not_double_apply_the_vrp():
    """VIX is ALREADY an implied vol — the premium is inside it. Applying the
    VRP again would make every option systematically too expensive, which is
    the opposite error but just as wrong."""
    v, source = atm_vol(anchor_vol=0.18, params=SurfaceParams.default_for("SPY"))
    assert source == "vol_index_anchor"
    assert v == pytest.approx(0.18)      # NOT 0.18 * 1.15


def test_recorded_iv_wins_over_everything():
    v, source = atm_vol(recorded_iv=0.22, anchor_vol=0.18, realized=0.30,
                        params=SurfaceParams.default_for("SPY"))
    assert source == "recorded" and v == pytest.approx(0.22)


def test_level_priority_order():
    p = SurfaceParams.default_for("SPY")
    assert atm_vol(anchor_vol=0.18, realized=0.30, params=p)[1] == "vol_index_anchor"
    assert atm_vol(realized=0.30, params=p)[1] == "realized_ewma_x_vrp"
    assert atm_vol(params=p) == (None, "unavailable")


def test_level_is_clamped():
    p = SurfaceParams.default_for("SPY")
    assert atm_vol(recorded_iv=99.0, params=p)[0] == MAX_VOL
    assert atm_vol(recorded_iv=0.0001, params=p)[0] == MIN_VOL


def test_beta_scales_a_non_anchored_underlying():
    p = SurfaceParams(underlying="SOXL", atm_iv_beta=2.6)
    v, _ = atm_vol(anchor_vol=0.18, params=p)
    assert v == pytest.approx(0.18 * 2.6)


# ---- realized vol ---------------------------------------------------------

def test_realized_vol_ewma_on_a_known_series():
    """A constant 1%/day move annualizes to 1% * sqrt(252) ~ 15.9%."""
    closes = [100.0]
    for i in range(200):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    v = realized_vol_ewma(closes)
    assert v == pytest.approx(math.log(1.01) * math.sqrt(252), rel=0.05)


def test_realized_vol_needs_enough_data():
    assert realized_vol_ewma(None) is None
    assert realized_vol_ewma([]) is None
    assert realized_vol_ewma([100.0]) is None
    assert realized_vol_ewma([100.0, 100.0, 100.0]) is None   # zero variance


def test_realized_vol_ignores_bad_prices():
    v = realized_vol_ewma([100.0, 0.0, 101.0, -5.0, 102.0, 101.0, 103.0])
    assert v is not None and v > 0


def test_higher_vol_series_gives_higher_realized():
    calm = [100.0 * (1.001 ** (i % 2 * 2 - 1)) for i in range(100)]
    wild = [100.0 * (1.05 ** (i % 2 * 2 - 1)) for i in range(100)]
    assert realized_vol_ewma(wild) > realized_vol_ewma(calm)


# ---- term structure -------------------------------------------------------

def test_term_structure_is_mildly_upward_sloping_by_default():
    p = SurfaceParams.default_for("SPY")
    v30 = term_vol(0.18, 30, p)
    assert v30 == pytest.approx(0.18)
    assert term_vol(0.18, 7, p) < v30 < term_vol(0.18, 180, p)


def test_flat_term_slope_is_flat():
    p = SurfaceParams(underlying="SPY", term_slope=0.0)
    for dte in (1, 7, 30, 90, 365):
        assert term_vol(0.18, dte, p) == pytest.approx(0.18)


def test_term_vol_handles_zero_dte():
    p = SurfaceParams.default_for("SPY")
    assert term_vol(0.18, 0, p) > 0
    assert term_vol(0.18, -5, p) > 0


# ---- skew -----------------------------------------------------------------

def test_puts_are_bid_over_calls_the_way_the_real_market_prices_them():
    """A 25-delta SPY put trades several vol points ABOVE ATM. A flat surface
    makes protective puts cheap and OTM calls rich — and puts are half of what
    this feature is for."""
    s = mk("SPY", 0.18)
    S, T, dte = 672.0, 30 / 365, 30
    atm = s.sigma(S, 672.0, T, dte)
    otm_put = s.sigma(S, 620.0, T, dte)      # downside strike
    otm_call = s.sigma(S, 720.0, T, dte)     # upside strike
    assert otm_put > atm, "downside skew missing"
    assert otm_call < otm_put, "skew is not downward-sloping"


def test_index_skew_is_steeper_than_single_name_skew():
    idx = SurfaceParams.default_for("SPY")
    single = SurfaceParams.default_for("KO")
    assert abs(idx.skew_b) > abs(single.skew_b)


def test_skew_multiplier_is_one_at_the_money():
    assert skew_multiplier(0.0, SurfaceParams.default_for("SPY")) == pytest.approx(1.0)


def test_skew_multiplier_is_clamped_in_the_far_wings():
    """An unclamped quadratic eventually turns over and goes NEGATIVE, and a
    negative volatility is free money a backtest will happily 'harvest'."""
    p = SurfaceParams.default_for("SPY")
    for m in (-50.0, -20.0, 20.0, 50.0):
        mult = skew_multiplier(m, p)
        assert 0.0 < mult <= 3.0


def test_sigma_is_always_within_absolute_bounds():
    s = mk("SPY", 0.18)
    for K in (1.0, 100.0, 672.0, 5000.0):
        for dte in (1, 7, 30, 180, 400):
            v = s.sigma(672.0, K, dte / 365, dte)
            assert MIN_VOL <= v <= MAX_VOL, (K, dte, v)
            assert math.isfinite(v)


def test_standardized_moneyness_units():
    """Skew in standardised units is what lets ONE (b, c) pair describe a 7-day
    and a 180-day expiry at once."""
    S, sigma = 100.0, 0.20
    m_atm = standardized_moneyness(S, 100.0, 30 / 365, 0.0, 0.0, sigma)
    assert m_atm == pytest.approx(0.0, abs=1e-9)
    assert standardized_moneyness(S, 110.0, 30 / 365, 0.0, 0.0, sigma) > 0
    assert standardized_moneyness(S, 90.0, 30 / 365, 0.0, 0.0, sigma) < 0
    # the same dollar distance is MORE standard deviations on a short expiry
    near = abs(standardized_moneyness(S, 110.0, 7 / 365, 0.0, 0.0, sigma))
    far = abs(standardized_moneyness(S, 110.0, 180 / 365, 0.0, 0.0, sigma))
    assert near > far


def test_standardized_moneyness_is_safe_on_bad_inputs():
    assert standardized_moneyness(0.0, 100.0, 0.1, 0, 0, 0.2) == 0.0
    assert standardized_moneyness(100.0, 0.0, 0.1, 0, 0, 0.2) == 0.0
    assert math.isfinite(standardized_moneyness(100.0, 100.0, 0.0, 0, 0, 0.0))


# ---- no arbitrage: the check that keeps a backtest honest -----------------

@pytest.mark.parametrize("underlying,atm", [
    ("SPY", 0.12), ("SPY", 0.18), ("SPY", 0.45), ("SPY", 0.90),
    ("KO", 0.15), ("KO", 0.35), ("TQQQ", 0.60),
])
@pytest.mark.parametrize("dte", [7, 30, 90, 180])
def test_surface_admits_no_butterfly_arbitrage(underlying, atm, dte):
    s = mk(underlying, atm)
    S = 100.0
    strikes = [S * (1 + i * 0.02) for i in range(-20, 21)]
    for right in ("C", "P"):
        problems = check_butterfly_arbitrage(s, S, strikes, dte / 365, dte, right)
        assert problems == [], problems


@pytest.mark.parametrize("underlying,atm", [
    ("SPY", 0.12), ("SPY", 0.30), ("KO", 0.25), ("TQQQ", 0.70),
])
def test_surface_admits_no_calendar_arbitrage(underlying, atm):
    s = mk(underlying, atm)
    S = 100.0
    for K in (70.0, 90.0, 100.0, 110.0, 140.0):
        problems = check_calendar_arbitrage(s, S, K, [7, 14, 30, 60, 90, 180, 365])
        assert problems == [], problems


def test_surface_is_arbitrage_free_across_an_extreme_grid():
    """The guarantee the skew saturation exists to provide, locked in.

    Vol 5%-200%, tenors 1-730 DTE, strikes +/-60%, and skew parameters well
    outside anything calibration should ever produce. The UNSATURATED quadratic
    produced up to 164 violations on a much milder grid than this one; regressing
    that would silently hand every options backtest free money."""
    total = 0
    for atm in (0.05, 0.10, 0.18, 0.30, 0.60, 1.20, 2.00):
        for b, c in ((-0.12, 0.06), (-0.06, 0.04), (-0.25, 0.10), (0.0, 0.0)):
            params = SurfaceParams(underlying="X", skew_b=b, skew_c=c)
            s = VolSurface(underlying="X", atm_30=atm, params=params, source="test")
            strikes = [100.0 * (1 + i * 0.02) for i in range(-30, 31)]
            for dte in (1, 3, 7, 30, 90, 180, 365, 730):
                for right in ("C", "P"):
                    total += len(check_butterfly_arbitrage(
                        s, 100.0, strikes, dte / 365, dte, right))
                total += len(check_calendar_arbitrage(
                    s, 100.0, 100.0, [1, 7, 30, 90, 180, 365, 730]))
    assert total == 0, f"{total} arbitrage violations"


def test_skew_saturates_rather_than_growing_without_bound():
    p = SurfaceParams.default_for("SPY")
    far = skew_multiplier(30.0, p)
    further = skew_multiplier(300.0, p)
    assert far == pytest.approx(further, abs=1e-6), "wings must flatten, not diverge"


def test_near_the_money_skew_is_preserved_by_the_saturation():
    """The saturation must not neuter the effect it is protecting: within a
    standard deviation of the money it must still match the raw quadratic to
    within a couple of percent."""
    p = SurfaceParams.default_for("SPY")
    for m in (-1.0, -0.5, 0.5, 1.0):
        raw = 1.0 + p.skew_b * m + p.skew_c * m * m
        assert skew_multiplier(m, p) == pytest.approx(raw, rel=0.02), m
    assert skew_multiplier(-1.0, p) > 1.10     # downside strikes still bid up
    assert skew_multiplier(0.0, p) == pytest.approx(1.0)


def test_arbitrage_checkers_actually_detect_violations():
    """A checker that never fires is worthless. An absurd skew must trip it."""
    bad = VolSurface(underlying="SPY", atm_30=0.18, source="test",
                     params=SurfaceParams(underlying="SPY", skew_b=-3.0, skew_c=0.0))
    strikes = [100.0 * (1 + i * 0.02) for i in range(-20, 21)]
    problems = check_butterfly_arbitrage(bad, 100.0, strikes, 30 / 365, 30, "C")
    assert problems, "a wildly steep skew must be detected as arbitrageable"


def test_negative_term_slope_can_trip_the_calendar_check():
    steep = VolSurface(underlying="SPY", atm_30=0.18, source="test",
                       params=SurfaceParams(underlying="SPY", term_slope=-1.5))
    problems = check_calendar_arbitrage(steep, 100.0, 100.0, [7, 30, 90, 365])
    assert problems


# ---- plumbing -------------------------------------------------------------

def test_surface_to_dict_carries_provenance():
    """The assumptions block must be able to say whether the surface was FITTED
    or GUESSED — those deserve very different trust."""
    s = mk("SPY", 0.18)
    d = s.to_dict()
    assert d["underlying"] == "SPY"
    assert d["source"] == "test"
    assert d["calibrated"] is False
    assert d["vrp_mult"] == DEFAULT_VRP_MULT


def test_custom_vrp_is_respected():
    p = SurfaceParams.default_for("SPY", vrp_mult=1.0)
    v, _ = atm_vol(realized=0.20, params=p)
    assert v == pytest.approx(0.20)
