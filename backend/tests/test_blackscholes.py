"""Black-Scholes correctness.

Options pricing is easy to get subtly wrong in ways that produce plausible
numbers, so this leans on IDENTITIES that must hold exactly (put-call parity,
delta bounds, IV round-trip) rather than on hand-checked values alone. Where
literature values are used they are stated with their source."""

from __future__ import annotations

import math

import pytest

from app.options.blackscholes import (DAYS_PER_YEAR, delta, d1_d2, greeks,
                                      implied_vol, intrinsic, norm_cdf, norm_pdf,
                                      price, price_bounds)

R, Q = 0.04, 0.0


# ---- the normal distribution ---------------------------------------------

def test_norm_cdf_known_values():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.0) == pytest.approx(0.8413447461, abs=1e-9)
    assert norm_cdf(-1.0) == pytest.approx(0.1586552539, abs=1e-9)
    assert norm_cdf(1.96) == pytest.approx(0.9750021049, abs=1e-9)
    assert norm_cdf(-8.0) == pytest.approx(0.0, abs=1e-14)
    assert norm_cdf(8.0) == pytest.approx(1.0, abs=1e-14)


def test_norm_pdf_known_values():
    assert norm_pdf(0.0) == pytest.approx(0.3989422804, abs=1e-9)
    assert norm_pdf(1.0) == pytest.approx(0.2419707245, abs=1e-9)


def test_norm_cdf_is_monotone_and_symmetric():
    prev = -1.0
    for i in range(-500, 501):
        x = i / 100.0
        v = norm_cdf(x)
        assert v >= prev
        prev = v
        assert norm_cdf(x) + norm_cdf(-x) == pytest.approx(1.0, abs=1e-12)


# ---- put-call parity: the single strongest correctness check --------------

def test_put_call_parity():
    """C - P == S*e^-qT - K*e^-rT, exactly, everywhere. If parity holds across
    a wide grid, the call and put branches are almost certainly both right."""
    for S in (50.0, 100.0, 250.0, 672.30):
        for K in (0.5 * S, 0.9 * S, S, 1.1 * S, 2.0 * S):
            for T in (1 / 365, 7 / 365, 0.25, 1.0, 2.0):
                for sigma in (0.05, 0.18, 0.60, 1.5):
                    for q in (0.0, 0.013, 0.05):
                        c = price(S, K, T, R, q, sigma, "C")
                        p = price(S, K, T, R, q, sigma, "P")
                        lhs = c - p
                        rhs = S * math.exp(-q * T) - K * math.exp(-R * T)
                        assert lhs == pytest.approx(rhs, abs=1e-9), (S, K, T, sigma, q)


def test_prices_are_within_no_arbitrage_bounds():
    for S in (100.0, 500.0):
        for K in (50.0, 100.0, 200.0, 900.0):
            for T in (1 / 365, 0.5, 2.0):
                for sigma in (0.05, 0.3, 2.0):
                    for right in ("C", "P"):
                        lo, hi = price_bounds(S, K, T, R, Q, right)
                        px = price(S, K, T, R, Q, sigma, right)
                        assert lo - 1e-9 <= px <= hi + 1e-9, (S, K, T, sigma, right)


# ---- limits and boundaries ------------------------------------------------

def test_price_converges_to_intrinsic_as_time_runs_out():
    S, K = 105.0, 100.0
    prev = price(S, K, 1.0, R, Q, 0.2, "C")
    for T in (0.5, 0.1, 0.01, 1 / 365, 1 / (365 * 24)):
        px = price(S, K, T, R, Q, 0.2, "C")
        assert px <= prev + 1e-9
        prev = px
    assert price(S, K, 0.0, R, Q, 0.2, "C") == pytest.approx(5.0)
    assert price(S, K, 0.0, R, Q, 0.2, "P") == pytest.approx(0.0)


def test_expired_options_are_worth_intrinsic():
    assert price(120.0, 100.0, 0.0, R, Q, 0.3, "C") == 20.0
    assert price(80.0, 100.0, 0.0, R, Q, 0.3, "P") == 20.0
    assert price(80.0, 100.0, -1.0, R, Q, 0.3, "C") == 0.0


def test_zero_vol_is_the_deterministic_forward_payoff():
    S, K, T = 100.0, 90.0, 1.0
    fwd = S * math.exp((R - Q) * T)
    assert price(S, K, T, R, Q, 0.0, "C") == pytest.approx(
        math.exp(-R * T) * (fwd - K))
    assert price(S, K, T, R, Q, 0.0, "P") == pytest.approx(0.0)


def test_deep_itm_and_otm_never_produce_nan():
    """The normal case at expiry, not an edge case."""
    for S, K in ((1000.0, 1.0), (1.0, 1000.0)):
        for T in (2.0, 1 / 365, 1 / (365 * 24)):
            for right in ("C", "P"):
                px = price(S, K, T, R, Q, 0.3, right)
                assert math.isfinite(px) and px >= 0
                g = greeks(S, K, T, R, Q, 0.3, right)
                for v in (g.delta, g.gamma, g.theta, g.vega, g.rho):
                    assert math.isfinite(v)


def test_non_positive_inputs_raise():
    for bad in ((0.0, 100.0), (100.0, 0.0), (-1.0, 100.0)):
        with pytest.raises(ValueError):
            price(bad[0], bad[1], 1.0, R, Q, 0.2, "C")
        with pytest.raises(ValueError):
            greeks(bad[0], bad[1], 1.0, R, Q, 0.2, "C")


# ---- monotonicity ---------------------------------------------------------

def test_price_is_increasing_in_vol():
    S, K, T = 100.0, 100.0, 0.5
    prev_c = prev_p = -1.0
    for sigma in (0.01, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.0):
        c = price(S, K, T, R, Q, sigma, "C")
        p = price(S, K, T, R, Q, sigma, "P")
        assert c > prev_c and p > prev_p
        prev_c, prev_p = c, p


def test_call_rises_and_put_falls_with_spot():
    """The monotonicity that makes the synthetic bar's high/low exact:
    call price is strictly increasing in S, put strictly decreasing."""
    K, T, sigma = 100.0, 0.25, 0.25
    prev_c, prev_p = -1.0, 1e9
    for S in range(50, 151, 5):
        c = price(float(S), K, T, R, Q, sigma, "C")
        p = price(float(S), K, T, R, Q, sigma, "P")
        assert c > prev_c
        assert p < prev_p
        prev_c, prev_p = c, p


def test_call_falls_with_strike():
    S, T, sigma = 100.0, 0.25, 0.25
    prev = 1e9
    for K in range(50, 151, 5):
        c = price(S, float(K), T, R, Q, sigma, "C")
        assert c < prev
        prev = c


# ---- greeks ---------------------------------------------------------------

def test_delta_bounds_by_right():
    for S in (50.0, 100.0, 200.0):
        for K in (50.0, 100.0, 200.0):
            for T in (0.01, 1.0):
                gc = greeks(S, K, T, R, Q, 0.25, "C")
                gp = greeks(S, K, T, R, Q, 0.25, "P")
                assert 0.0 <= gc.delta <= 1.0
                assert -1.0 <= gp.delta <= 0.0


def test_atm_delta_is_near_half():
    g = greeks(100.0, 100.0, 0.25, 0.0, 0.0, 0.2, "C")
    assert 0.5 < g.delta < 0.56


def test_put_call_delta_relationship():
    """delta_call - delta_put == e^-qT for every contract."""
    for q in (0.0, 0.02):
        for S in (80.0, 100.0, 130.0):
            dc = greeks(S, 100.0, 0.5, R, q, 0.3, "C").delta
            dp = greeks(S, 100.0, 0.5, R, q, 0.3, "P").delta
            assert dc - dp == pytest.approx(math.exp(-q * 0.5), abs=1e-9)


def test_gamma_and_vega_are_identical_for_calls_and_puts():
    gc = greeks(105.0, 100.0, 0.4, R, 0.01, 0.3, "C")
    gp = greeks(105.0, 100.0, 0.4, R, 0.01, 0.3, "P")
    assert gc.gamma == pytest.approx(gp.gamma, abs=1e-12)
    assert gc.vega == pytest.approx(gp.vega, abs=1e-12)


def test_gamma_and_vega_peak_at_the_money():
    atm = greeks(100.0, 100.0, 0.25, R, Q, 0.25, "C")
    for S in (70.0, 85.0, 115.0, 140.0):
        away = greeks(S, 100.0, 0.25, R, Q, 0.25, "C")
        assert atm.gamma > away.gamma
        assert atm.vega > away.vega


def test_long_option_theta_is_negative():
    """Time decay works against the holder — the defining cost of being long
    premium, and a sign error here would invert every options strategy."""
    for right in ("C", "P"):
        for S in (90.0, 100.0, 110.0):
            assert greeks(S, 100.0, 0.25, 0.0, 0.0, 0.25, right).theta < 0


def test_theta_is_per_calendar_day_not_per_year():
    """Per-year theta is ~365x larger; mixing the two silently corrupts P&L
    attribution. An ATM 90-day option loses cents/day, not dollars/day."""
    g = greeks(100.0, 100.0, 0.25, R, Q, 0.25, "C")
    assert -0.10 < g.theta < -0.01
    # and it must be exactly 1/365 of the annualized figure
    S, K, T, sigma = 100.0, 100.0, 0.25, 0.25
    d1, d2 = d1_d2(S, K, T, R, Q, sigma)
    theta_year = (-(S * math.exp(-Q * T) * norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
                  - R * K * math.exp(-R * T) * norm_cdf(d2)
                  + Q * S * math.exp(-Q * T) * norm_cdf(d1))
    assert g.theta == pytest.approx(theta_year / DAYS_PER_YEAR, abs=1e-12)


def test_vega_is_per_vol_point():
    """Trading screens quote vega per 1 vol point (0.01 of sigma). A 1-point
    vol move should change the price by approximately vega."""
    S, K, T, sigma = 100.0, 100.0, 1.0, 0.25
    v = greeks(S, K, T, R, Q, sigma, "C").vega
    actual = price(S, K, T, R, Q, sigma + 0.01, "C") - price(S, K, T, R, Q, sigma, "C")
    assert v == pytest.approx(actual, rel=0.02)
    assert 0.1 < v < 1.0


def test_greeks_match_numerical_derivatives():
    """The definitive check: every greek against a finite difference."""
    S, K, T, sigma, q = 105.0, 100.0, 0.5, 0.28, 0.01
    g = greeks(S, K, T, R, q, sigma, "C")
    h = 1e-5

    num_delta = (price(S + h, K, T, R, q, sigma, "C")
                 - price(S - h, K, T, R, q, sigma, "C")) / (2 * h)
    assert g.delta == pytest.approx(num_delta, rel=1e-5)

    # A SECOND difference needs a much larger step than a first difference:
    # at h=1e-5 the numerator is the difference of near-identical O(10) prices
    # divided by 1e-10, which is pure floating-point cancellation noise. The
    # optimal step for a second derivative is ~eps^(1/4)*S, i.e. ~1e-2 here.
    hg = 1e-2
    num_gamma = (price(S + hg, K, T, R, q, sigma, "C")
                 - 2 * price(S, K, T, R, q, sigma, "C")
                 + price(S - hg, K, T, R, q, sigma, "C")) / (hg * hg)
    assert g.gamma == pytest.approx(num_gamma, rel=1e-5)

    num_vega = (price(S, K, T, R, q, sigma + h, "C")
                - price(S, K, T, R, q, sigma - h, "C")) / (2 * h) / 100.0
    assert g.vega == pytest.approx(num_vega, rel=1e-5)

    num_rho = (price(S, K, T, R + h, q, sigma, "C")
               - price(S, K, T, R - h, q, sigma, "C")) / (2 * h) / 100.0
    assert g.rho == pytest.approx(num_rho, rel=1e-4)

    dt = 1e-6
    num_theta = ((price(S, K, T - dt, R, q, sigma, "C")
                  - price(S, K, T, R, q, sigma, "C")) / dt) / DAYS_PER_YEAR
    assert g.theta == pytest.approx(num_theta, rel=1e-3)


def test_standalone_delta_matches_full_greeks():
    for S in (80.0, 100.0, 120.0):
        for right in ("C", "P"):
            assert delta(S, 100.0, 0.3, R, 0.01, 0.25, right) == pytest.approx(
                greeks(S, 100.0, 0.3, R, 0.01, 0.25, right).delta, abs=1e-12)


def test_expired_delta_is_binary():
    assert delta(120.0, 100.0, 0.0, R, Q, 0.2, "C") == 1.0
    assert delta(80.0, 100.0, 0.0, R, Q, 0.2, "C") == 0.0
    assert delta(80.0, 100.0, 0.0, R, Q, 0.2, "P") == -1.0


# ---- implied volatility ---------------------------------------------------

def test_implied_vol_round_trips():
    """price(implied_vol(price(sigma))) == sigma across the whole grid v1
    trades. This is the test that matters for calibration correctness."""
    for S in (25.0, 100.0, 672.30):
        for moneyness in (0.7, 0.9, 1.0, 1.1, 1.4):
            K = S * moneyness
            for T in (7 / 365, 30 / 365, 90 / 365, 1.0):
                for sigma in (0.08, 0.18, 0.35, 0.75, 1.5):
                    for right in ("C", "P"):
                        px = price(S, K, T, R, 0.01, sigma, right)
                        # Skip contracts with no extrinsic value left: their
                        # price is identical at every vol, so no IV exists to
                        # recover and `implied_vol` returns None by design.
                        lo_bound, _ = price_bounds(S, K, T, R, 0.01, right)
                        if px < 1e-6 or px - lo_bound < 1e-6:
                            continue
                        iv = implied_vol(px, S, K, T, R, 0.01, right)
                        assert iv is not None, (S, K, T, sigma, right)
                        assert iv == pytest.approx(sigma, abs=1e-4), (S, K, T, sigma, right)


def test_implied_vol_returns_none_outside_no_arbitrage_bounds():
    """None, never 0.0 and never an exception — a stale or crossed quote is
    not consistent with any volatility, and callers must be able to tell."""
    S, K, T = 100.0, 100.0, 0.25
    lo, hi = price_bounds(S, K, T, R, Q, "C")
    assert implied_vol(hi * 1.5, S, K, T, R, Q, "C") is None
    assert implied_vol(max(lo - 1.0, 0.0) if lo > 1 else 1e-12, S, K, T, R, Q, "C") is None
    assert implied_vol(0.0, S, K, T, R, Q, "C") is None
    assert implied_vol(-5.0, S, K, T, R, Q, "C") is None


def test_implied_vol_handles_degenerate_inputs():
    assert implied_vol(5.0, 100.0, 100.0, 0.0, R, Q, "C") is None
    assert implied_vol(5.0, 0.0, 100.0, 1.0, R, Q, "C") is None
    assert implied_vol(5.0, 100.0, 0.0, 1.0, R, Q, "C") is None


def test_implied_vol_survives_wings_where_vega_collapses():
    """Newton alone diverges here because vega -> 0; the bisection fallback is
    what makes convergence unconditional. These strikes still carry real
    extrinsic value, so an IV genuinely exists and must be found."""
    S, T = 100.0, 30 / 365
    for K in (70.0, 140.0):
        for right in ("C", "P"):
            px = price(S, K, T, R, Q, 0.5, right)
            lo_bound, _ = price_bounds(S, K, T, R, Q, right)
            if px - lo_bound < 1e-6:
                continue
            iv = implied_vol(px, S, K, T, R, Q, right)
            assert iv is not None, (K, right)
            assert iv == pytest.approx(0.5, abs=1e-3)


def test_implied_vol_is_none_when_unidentifiable():
    """A deep-ITM contract with no extrinsic value prices identically at 0.01%
    vol and at 500%, so no IV exists. Returning `lo` would look like a real
    0.01% reading and would drag any calibration averaging it toward zero —
    the model must say 'unknown', not 'nearly zero'."""
    S, T = 100.0, 30 / 365
    for K in (20.0, 40.0):
        px = price(S, K, T, R, Q, 0.5, "C")
        lo, _ = price_bounds(S, K, T, R, Q, "C")
        assert px - lo < 1e-6, "test premise: this contract has no extrinsic value"
        assert implied_vol(px, S, K, T, R, Q, "C") is None


def test_implied_vol_of_a_quote_at_intrinsic_is_none_or_near_zero():
    S, K, T = 110.0, 100.0, 0.25
    lo, _ = price_bounds(S, K, T, R, Q, "C")
    iv = implied_vol(lo + 1e-8, S, K, T, R, Q, "C")
    assert iv is None or iv < 0.02


def test_intrinsic_helper():
    assert intrinsic(110.0, 100.0, "C") == 10.0
    assert intrinsic(90.0, 100.0, "C") == 0.0
    assert intrinsic(90.0, 100.0, "P") == 10.0
    assert intrinsic(110.0, 100.0, "P") == 0.0
