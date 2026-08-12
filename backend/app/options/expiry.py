"""Expiration calendar and DTE arithmetic.

Two jobs:

  * generate the expirations that PLAUSIBLY existed on a given historical date,
    for the synthetic chain a backtest trades against. This matters more than it
    sounds: a synthetic backtest that invents a 37-day expiry on an arbitrary
    Wednesday is trading contracts that never listed, and its results are
    fiction. Generating only real expiration DATES (3rd-Friday monthlies,
    quarterlies, and weeklies for the handful of products that had them) keeps
    the simulation honest about WHICH contracts were available even though it
    cannot know their prices.

  * DTE arithmetic that both the engine and the runner share, so
    `close_at_dte` means the same thing in a backtest and in a live run.

EXPIRATION TIME. Standard equity options stop trading at the close (16:00 ET)
on the expiration date and settle that evening. So a contract expiring
2026-09-18 is tradeable all day on the 18th: DTE is 0, not -1, and a position
with `close_at_dte: 1` must be closed on the 17th. Off-by-one here silently
changes every options strategy's holding period, so it has its own tests.

WEEKLIES. Only a small set of very liquid products had weekly options, and they
were introduced progressively (SPY 2005, QQQ/IWM 2010, mega-caps ~2011-2016).
`WEEKLY_UNDERLYINGS` encodes that with start dates rather than pretending
weeklies always existed — a 2008 backtest selecting a 7-DTE SPY weekly that
would not list until 2010 is exactly the kind of quiet anachronism that makes a
synthetic backtest untrustworthy.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

#: Schwab's `expirationType` codes (market-data README §ExpirationType).
EXP_STANDARD = "S"    # 3rd Friday of the month
EXP_MONTHLY = "M"     # end-of-month
EXP_QUARTERLY = "Q"   # last business day of Mar/Jun/Sep/Dec
EXP_WEEKLY = "W"      # Friday short-term

ALL_EXPIRY_TYPES = (EXP_STANDARD, EXP_MONTHLY, EXP_QUARTERLY, EXP_WEEKLY)

#: Underlyings with weekly options, and the year weeklies became broadly
#: available on them. Anything not listed is treated as monthly-only, which is
#: the conservative assumption: it makes the synthetic chain SPARSER than
#: reality rather than richer, so a strategy cannot depend on a contract that
#: may not have existed.
WEEKLY_UNDERLYINGS: dict[str, int] = {
    "SPY": 2005, "QQQ": 2010, "IWM": 2010, "DIA": 2010,
    "AAPL": 2011, "MSFT": 2012, "NVDA": 2013, "AMZN": 2011, "GOOGL": 2012,
    "META": 2013, "TSLA": 2012, "NFLX": 2012, "AMD": 2014, "SMH": 2016,
    "TQQQ": 2012, "SQQQ": 2012, "SOXL": 2014, "UVXY": 2013, "GLD": 2010,
    "TLT": 2010, "XLF": 2011, "EEM": 2010, "SPXL": 2014,
}

#: US market holidays that land on a Friday and therefore pull an expiration
#: back to Thursday. Good Friday is the one that actually recurs; the others are
#: rare. Kept as a small explicit set rather than a full holiday calendar
#: because only FRIDAY holidays can move an expiration.
_GOOD_FRIDAYS = {
    dt.date(2006, 4, 14), dt.date(2007, 4, 6), dt.date(2008, 3, 21),
    dt.date(2009, 4, 10), dt.date(2010, 4, 2), dt.date(2011, 4, 22),
    dt.date(2012, 4, 6), dt.date(2013, 3, 29), dt.date(2014, 4, 18),
    dt.date(2015, 4, 3), dt.date(2016, 3, 25), dt.date(2017, 4, 14),
    dt.date(2018, 3, 30), dt.date(2019, 4, 19), dt.date(2020, 4, 10),
    dt.date(2021, 4, 2), dt.date(2022, 4, 15), dt.date(2023, 4, 7),
    dt.date(2024, 3, 29), dt.date(2025, 4, 18), dt.date(2026, 4, 3),
    dt.date(2027, 3, 26), dt.date(2028, 4, 14), dt.date(2029, 3, 30),
    dt.date(2030, 4, 19),
}
_FIXED_FRIDAY_HOLIDAYS = {
    dt.date(2020, 7, 3), dt.date(2021, 12, 24), dt.date(2026, 7, 3),
    dt.date(2027, 12, 24), dt.date(2032, 12, 24),
}
FRIDAY_HOLIDAYS = _GOOD_FRIDAYS | _FIXED_FRIDAY_HOLIDAYS


@dataclass(frozen=True, order=True)
class Expiration:
    """One tradeable expiration date for an underlying."""
    expiry: dt.date
    expiration_type: str            # S | M | Q | W
    standard: bool = True
    settlement_type: str = "P"      # A (AM) | P (PM); equity options are PM

    @property
    def is_weekly(self) -> bool:
        return self.expiration_type == EXP_WEEKLY


# ---- date helpers ---------------------------------------------------------

def third_friday(year: int, month: int) -> dt.date:
    """The standard monthly expiration date: the 3rd Friday, pulled back to
    Thursday when that Friday is a holiday (the real OCC rule)."""
    d = dt.date(year, month, 1)
    # weekday(): Mon=0 .. Fri=4
    first_friday = 1 + (4 - d.weekday()) % 7
    third = dt.date(year, month, first_friday + 14)
    return _adjust_for_holiday(third)


def _adjust_for_holiday(d: dt.date) -> dt.date:
    while d in FRIDAY_HOLIDAYS:
        d -= dt.timedelta(days=1)
    return d


def fridays_between(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    """Every Friday in [start, end], holiday-adjusted."""
    d = start + dt.timedelta(days=(4 - start.weekday()) % 7)
    while d <= end:
        yield _adjust_for_holiday(d)
        d += dt.timedelta(days=7)


def is_quarterly_month(month: int) -> bool:
    return month in (3, 6, 9, 12)


def has_weeklies(underlying: str, as_of: dt.date) -> bool:
    year = WEEKLY_UNDERLYINGS.get((underlying or "").strip().upper())
    return year is not None and as_of.year >= year


# ---- DTE arithmetic -------------------------------------------------------

def expiry_to_ms(expiry: dt.date) -> int:
    """Epoch ms for an expiration date, anchored at 16:00 ET on that date —
    the moment the contract stops trading. Storing midnight instead would make
    every same-day comparison off by one session."""
    return int(dt.datetime.combine(expiry, dt.time(16, 0), ET).timestamp() * 1000)


def ms_to_expiry(ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(ms / 1000, tz=ET).date()


def days_to_expiry(expiry: dt.date, ts_ms: int) -> int:
    """Calendar days from the bar at `ts_ms` to `expiry`, in EXCHANGE-LOCAL
    time. 0 on expiration day (the contract is still tradeable), negative
    afterwards. Converting to ET first matters: a 20:00 UTC bar is the same
    trading day as a 14:00 UTC one, and using UTC dates would make DTE tick
    over mid-session for every afternoon bar."""
    bar_date = dt.datetime.fromtimestamp(ts_ms / 1000, tz=ET).date()
    return (expiry - bar_date).days


def year_fraction(expiry: dt.date, ts_ms: int, *, min_years: float = 1.0 / (365.0 * 24)) -> float:
    """Time to expiry in YEARS for Black-Scholes, from the exact bar timestamp
    to 16:00 ET on the expiration date.

    Uses the real elapsed time rather than whole days, because at 1 DTE the
    difference between "1 day" and "0.3 days" is a large fraction of the
    remaining extrinsic value. Floored at one hour so a contract being priced
    at its final moment yields a finite, well-conditioned d1/d2 rather than a
    division by zero."""
    end = expiry_to_ms(expiry)
    years = (end - ts_ms) / (365.0 * 24 * 3600 * 1000)
    return max(years, min_years)


def is_expired(expiry: dt.date, ts_ms: int) -> bool:
    return ts_ms > expiry_to_ms(expiry)


# ---- synthetic chain generation ------------------------------------------

def generate_expirations(underlying: str, as_of: dt.date, *,
                         max_dte: int = 400,
                         include_weeklies: bool = True) -> list[Expiration]:
    """The expirations plausibly listed for `underlying` on `as_of`.

    Used ONLY when no recorded `option_expirations` row exists for the date
    (i.e. historical backtests before the chain recorder started). Recorded
    data always wins — see `options/store.py::read_expirations`.

    Listing conventions modelled:
      * the next ~8 monthly (3rd Friday) expirations,
      * quarterlies out to `max_dte` (Mar/Jun/Sep/Dec, which coincide with the
        monthly 3rd Friday for equity options),
      * weeklies for the next ~6 weeks, only for products that had them on this
        date (see WEEKLY_UNDERLYINGS).

    LEAPS beyond `max_dte` are omitted: v1 trades 0-180 DTE and a synthetic
    2-year contract would be priced with a flat-extrapolated surface nobody
    should trust."""
    horizon = as_of + dt.timedelta(days=max_dte)
    out: dict[dt.date, Expiration] = {}

    # monthlies / quarterlies
    y, m = as_of.year, as_of.month
    for _ in range(24):
        d = third_friday(y, m)
        if d >= as_of and d <= horizon:
            out[d] = Expiration(
                expiry=d,
                expiration_type=EXP_QUARTERLY if is_quarterly_month(m) else EXP_STANDARD,
                standard=True)
        m += 1
        if m > 12:
            m, y = 1, y + 1
        if dt.date(y, m, 1) > horizon:
            break

    # weeklies
    if include_weeklies and has_weeklies(underlying, as_of):
        weekly_end = min(horizon, as_of + dt.timedelta(days=45))
        for d in fridays_between(as_of, weekly_end):
            if d not in out:
                out[d] = Expiration(expiry=d, expiration_type=EXP_WEEKLY, standard=False)

    return sorted(out.values())
