"""Expiration calendar + DTE arithmetic.

The off-by-one risk here is real and silent: `close_at_dte` decides when every
options position exits, so a DTE that ticks a day early changes every strategy's
holding period without any error surfacing."""

from __future__ import annotations

import datetime as dt

import pytest

from app.options.expiry import (EXP_QUARTERLY, EXP_STANDARD, EXP_WEEKLY, Expiration,
                                days_to_expiry, expiry_to_ms, generate_expirations,
                                has_weeklies, is_expired, ms_to_expiry, third_friday,
                                year_fraction)


def _ms(y, m, d, hh=10, mm=0):
    from zoneinfo import ZoneInfo
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)


# ---- third Friday ---------------------------------------------------------

@pytest.mark.parametrize("y,m,expected", [
    (2026, 1, dt.date(2026, 1, 16)),
    (2026, 2, dt.date(2026, 2, 20)),
    (2026, 3, dt.date(2026, 3, 20)),
    (2026, 6, dt.date(2026, 6, 19)),
    (2026, 9, dt.date(2026, 9, 18)),
    (2026, 12, dt.date(2026, 12, 18)),
    (2025, 12, dt.date(2025, 12, 19)),
    (2024, 3, dt.date(2024, 3, 15)),
    # month starting ON a Friday
    (2026, 5, dt.date(2026, 5, 15)),
])
def test_third_friday(y, m, expected):
    got = third_friday(y, m)
    assert got == expected
    assert got.weekday() == 4


@pytest.mark.parametrize("year,good_friday,expected", [
    # Years where Good Friday IS the 3rd Friday of April, so the standard
    # monthly expiration is pulled back to the Thursday. This is the real OCC
    # rule and the only holiday adjustment that recurs.
    (2019, dt.date(2019, 4, 19), dt.date(2019, 4, 18)),
    (2014, dt.date(2014, 4, 18), dt.date(2014, 4, 17)),
])
def test_good_friday_pulls_expiration_back_to_thursday(year, good_friday, expected):
    assert good_friday.weekday() == 4
    got = third_friday(year, 4)
    assert got == expected
    assert got.weekday() == 3


def test_good_friday_that_is_not_the_third_friday_changes_nothing():
    """Good Friday 2026 is 3 April — the FIRST Friday — so April 2026's
    expiration is the ordinary 17th."""
    assert third_friday(2026, 4) == dt.date(2026, 4, 17)


# ---- DTE ------------------------------------------------------------------

def test_dte_is_zero_on_expiration_day():
    """A contract is tradeable all day on its expiration date."""
    expiry = dt.date(2026, 9, 18)
    assert days_to_expiry(expiry, _ms(2026, 9, 18, 9, 45)) == 0
    assert days_to_expiry(expiry, _ms(2026, 9, 18, 15, 59)) == 0
    assert days_to_expiry(expiry, _ms(2026, 9, 17, 16, 0)) == 1
    assert days_to_expiry(expiry, _ms(2026, 9, 19, 10, 0)) == -1


def test_dte_uses_exchange_local_dates_not_utc():
    """A 20:00 UTC bar is the same ET trading day as a 14:00 UTC one. Using UTC
    dates would make DTE tick over mid-session every afternoon."""
    expiry = dt.date(2026, 9, 18)
    morning = _ms(2026, 9, 15, 9, 45)     # 13:45 UTC
    afternoon = _ms(2026, 9, 15, 15, 55)  # 19:55 UTC
    assert days_to_expiry(expiry, morning) == days_to_expiry(expiry, afternoon) == 3


def test_expiry_ms_round_trip():
    d = dt.date(2026, 6, 19)
    assert ms_to_expiry(expiry_to_ms(d)) == d


def test_expiry_anchored_at_the_close_not_midnight():
    """Anchoring at midnight would make a contract 'expired' for the whole of
    its own final trading session."""
    d = dt.date(2026, 6, 19)
    assert not is_expired(d, _ms(2026, 6, 19, 15, 59))
    assert is_expired(d, _ms(2026, 6, 19, 16, 1))


def test_year_fraction_shrinks_through_the_final_day():
    expiry = dt.date(2026, 6, 19)
    t_open = year_fraction(expiry, _ms(2026, 6, 19, 9, 30))
    t_late = year_fraction(expiry, _ms(2026, 6, 19, 15, 0))
    assert t_open > t_late > 0
    # ~30 days out should be ~30/365
    t30 = year_fraction(expiry, _ms(2026, 5, 20, 16, 0))
    assert 0.075 < t30 < 0.09


def test_year_fraction_is_floored_and_never_zero():
    """A finite, well-conditioned T is what keeps d1/d2 from dividing by zero
    at the very last moment of a contract's life."""
    t = year_fraction(dt.date(2026, 6, 19), _ms(2026, 6, 19, 16, 0))
    assert t > 0
    t_past = year_fraction(dt.date(2026, 6, 19), _ms(2026, 7, 1, 10, 0))
    assert t_past > 0


# ---- weeklies -------------------------------------------------------------

def test_weeklies_respect_their_introduction_year():
    """A 2008 backtest must not select a QQQ weekly: they did not list until
    2010. This is the anachronism guard."""
    assert has_weeklies("SPY", dt.date(2008, 5, 1))       # SPY weeklies from 2005
    assert not has_weeklies("QQQ", dt.date(2008, 5, 1))
    assert has_weeklies("QQQ", dt.date(2012, 5, 1))
    assert not has_weeklies("KO", dt.date(2026, 5, 1))    # not in the table at all


# ---- chain generation -----------------------------------------------------

def test_generate_expirations_are_all_real_fridays_in_the_future():
    as_of = dt.date(2026, 7, 30)
    exps = generate_expirations("SPY", as_of)
    assert exps
    for e in exps:
        assert e.expiry >= as_of
        # every generated date is a Friday, or a Thursday via holiday adjustment
        assert e.expiry.weekday() in (3, 4)
    assert exps == sorted(exps)


def test_generate_expirations_marks_quarterlies():
    exps = {e.expiry: e for e in generate_expirations("SPY", dt.date(2026, 7, 30))}
    sep = exps.get(dt.date(2026, 9, 18))
    assert sep is not None and sep.expiration_type == EXP_QUARTERLY
    aug = exps.get(dt.date(2026, 8, 21))
    assert aug is not None and aug.expiration_type == EXP_STANDARD


def test_generate_expirations_includes_weeklies_only_where_they_existed():
    spy = generate_expirations("SPY", dt.date(2026, 7, 30))
    assert any(e.expiration_type == EXP_WEEKLY for e in spy)
    ko = generate_expirations("KO", dt.date(2026, 7, 30))
    assert not any(e.expiration_type == EXP_WEEKLY for e in ko)
    old_qqq = generate_expirations("QQQ", dt.date(2008, 7, 30))
    assert not any(e.expiration_type == EXP_WEEKLY for e in old_qqq)


def test_generate_expirations_respects_max_dte():
    as_of = dt.date(2026, 7, 30)
    exps = generate_expirations("SPY", as_of, max_dte=90)
    assert exps
    for e in exps:
        assert (e.expiry - as_of).days <= 90


def test_expiration_ordering_is_by_date():
    a = Expiration(dt.date(2026, 1, 16), EXP_STANDARD)
    b = Expiration(dt.date(2026, 2, 20), EXP_STANDARD)
    assert a < b
