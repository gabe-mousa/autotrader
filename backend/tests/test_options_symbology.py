"""OCC symbology — the lowest level of the options stack, so it gets the
hardest tests. A bug here produces a VALID-LOOKING symbol for a contract that
does not exist, which is far worse than an exception."""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

import pytest

from app.options.symbology import (OCC_LEN, InvalidOccSymbol, OccSymbol, describe,
                                   format_occ, is_occ, parse_occ, underlying_of)


# ---- documented examples (the ground truth from the vendored Schwab docs) ----

@pytest.mark.parametrize("underlying,expiry,right,strike,expected", [
    # schwab/market-data-production/README.md §Symbol Formats
    ("AAPL", dt.date(2025, 12, 19), "C", Decimal("200.00"), "AAPL  251219C00200000"),
    # schwab/trader-api--individual/README.md §Options Symbology table
    ("XYZ", dt.date(2021, 1, 15), "C", Decimal("50.00"), "XYZ   210115C00050000"),
    ("XYZ", dt.date(2021, 1, 15), "C", Decimal("55.00"), "XYZ   210115C00055000"),
    ("XYZ", dt.date(2021, 1, 15), "C", Decimal("62.50"), "XYZ   210115C00062500"),
    ("XYZ", dt.date(2024, 3, 15), "C", Decimal("500.00"), "XYZ   240315C00500000"),
    ("XYZ", dt.date(2024, 3, 15), "P", Decimal("45.00"), "XYZ   240315P00045000"),
])
def test_documented_examples(underlying, expiry, right, strike, expected):
    assert format_occ(underlying, expiry, right, strike) == expected
    assert len(expected) == OCC_LEN
    occ = parse_occ(expected)
    assert occ.underlying == underlying
    assert occ.expiry == expiry
    assert occ.right == right
    assert occ.strike == strike


def test_root_is_space_padded_to_six():
    """The padding is real wire format, not cosmetic."""
    assert format_occ("A", dt.date(2026, 1, 16), "C", 5) == "A     260116C00005000"
    assert format_occ("SPXW", dt.date(2026, 1, 16), "C", 5)[:6] == "SPXW  "
    assert format_occ("ABCDEF", dt.date(2026, 1, 16), "C", 5)[:6] == "ABCDEF"


# ---- round-trip fuzz -------------------------------------------------------

def test_round_trip_fuzz():
    """10k random contracts survive format -> parse unchanged. This is the test
    that catches float contamination in the strike encoding."""
    rng = random.Random(20260730)
    roots = ["A", "GE", "SPY", "AAPL", "GOOGL", "ABCDEF", "BRK.B", "AAPL1"]
    for _ in range(10_000):
        root = rng.choice(roots)
        expiry = dt.date(2000 + rng.randint(1, 99), rng.randint(1, 12), rng.randint(1, 28))
        right = rng.choice(["C", "P"])
        # strikes on real ladders: whole, half, quarter and thousandth dollars
        strike = Decimal(rng.randint(1, 9_999_999)) / 1000
        sym = format_occ(root, expiry, right, strike)
        assert len(sym) == OCC_LEN
        assert is_occ(sym)
        back = parse_occ(sym)
        assert back == OccSymbol(root, expiry, right, strike), sym


def test_float_strikes_land_exactly():
    """`62.5` must encode 00062500, not 00062499 — the classic float trap."""
    for f, expect in [(62.5, "00062500"), (0.5, "00000500"), (1.0, "00001000"),
                      (200.0, "00200000"), (7.25, "00007250"), (12.375, "00012375"),
                      (0.1 * 3, "00000300"), (1234.567, "01234567")]:
        sym = format_occ("SPY", dt.date(2026, 6, 19), "C", f)
        assert sym[-8:] == expect, f"{f} -> {sym[-8:]}, wanted {expect}"


def test_strike_finer_than_a_tenth_of_a_cent_is_rejected():
    with pytest.raises(InvalidOccSymbol):
        format_occ("SPY", dt.date(2026, 6, 19), "C", Decimal("100.00005"))


def test_arithmetic_float_noise_is_absorbed_not_rejected():
    """`price * 1.05` style inputs are the normal way a strike gets computed,
    and they carry float noise. Noise must round; genuine sub-thousandth
    precision must still raise (test above)."""
    assert format_occ("SPY", dt.date(2026, 6, 19), "C", 0.1 * 3)[-8:] == "00000300"
    assert format_occ("SPY", dt.date(2026, 6, 19), "C", 600 * 1.05)[-8:] == "00630000"
    assert format_occ("SPY", dt.date(2026, 6, 19), "C", 2.675 * 100)[-8:] == "00267500"


def test_is_occ_agrees_with_parse_occ():
    """The contract that makes `is_occ` safe to branch on: anything it accepts,
    `parse_occ` parses. A structural-only regex would break this."""
    candidates = [
        "AAPL  251219C00200000", "AAPL  251332C00200000", "AAPL  251200C00200000",
        "      251219C00200000", "AAPL  251219C00000000", "SPY   260619P00062500",
        "AAPL  250229C00200000",   # 2025 is not a leap year
        "SPY", "", "garbage", "AAPL  251219X00200000",
    ]
    for sym in candidates:
        if is_occ(sym):
            parse_occ(sym)   # must not raise
        else:
            with pytest.raises(InvalidOccSymbol):
                parse_occ(sym)


# ---- rejection paths -------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "", "AAPL", "AAPL  251219C0020000",      # 20 chars
    "AAPL  251219C002000000",                 # 22 chars
    "AAPL  251219X00200000",                  # bad right
    "AAPL  251319C00200000",                  # month 13
    "AAPL  251232C00200000",                  # day 32
    "AAPL  2512X9C00200000",                  # non-numeric date
    "      251219C00200000",                  # empty root
    "AAPL  251219C0020000A",                  # non-numeric strike
])
def test_malformed_symbols_rejected(bad):
    assert not is_occ(bad)
    with pytest.raises(InvalidOccSymbol):
        parse_occ(bad)


def test_zero_strike_rejected():
    assert not is_occ("AAPL  251219C00000000")
    with pytest.raises(InvalidOccSymbol):
        parse_occ("AAPL  251219C00000000")
    with pytest.raises(InvalidOccSymbol):
        format_occ("AAPL", dt.date(2025, 12, 19), "C", 0)


def test_root_too_long_rejected_not_truncated():
    """Truncation would silently produce a symbol for a DIFFERENT contract."""
    with pytest.raises(InvalidOccSymbol):
        format_occ("TOOLONG", dt.date(2026, 1, 16), "C", 10)


def test_strike_too_large_rejected():
    with pytest.raises(InvalidOccSymbol):
        format_occ("SPY", dt.date(2026, 1, 16), "C", 100_000)


def test_call_put_words_accepted():
    a = format_occ("SPY", dt.date(2026, 1, 16), "call", 500)
    b = format_occ("SPY", dt.date(2026, 1, 16), "C", 500)
    assert a == b
    assert format_occ("SPY", dt.date(2026, 1, 16), "put", 500)[12] == "P"


# ---- is_occ is total and cheap --------------------------------------------

@pytest.mark.parametrize("v", [None, 123, "", "SPY", "AAPL", b"x" * 21, [], {},
                               "                     "])
def test_is_occ_never_raises(v):
    assert is_occ(v) is False


def test_underlying_of_handles_both_kinds():
    assert underlying_of("AAPL  251219C00200000") == "AAPL"
    assert underlying_of("spy") == "SPY"
    assert underlying_of("  qqq ") == "QQQ"
    assert underlying_of("") == ""


def test_describe_is_human_readable_and_safe():
    assert describe("AAPL  251219C00200000") == "AAPL 19Dec25 200C"
    assert describe("SPY   260619P00062500") == "SPY 19Jun26 62.5P"
    assert describe("SPY") == "SPY"            # equity passes through
    assert describe("garbage") == "garbage"    # never raises


def test_occ_symbol_helpers():
    occ = parse_occ("AAPL  251219C00200000")
    assert occ.is_call and not occ.is_put
    assert occ.to_symbol() == "AAPL  251219C00200000"
    put = parse_occ("AAPL  251219P00200000")
    assert put.is_put and not put.is_call


def test_occ_symbol_rejects_bad_right_in_constructor():
    with pytest.raises(InvalidOccSymbol):
        OccSymbol("AAPL", dt.date(2025, 12, 19), "X", Decimal("200"))


def test_strike_equality_is_exact_across_construction_paths():
    """Decimal identity: $62.50 built three ways must compare and hash equal."""
    d = dt.date(2026, 3, 20)
    a = parse_occ(format_occ("SPY", d, "C", 62.5))
    b = parse_occ(format_occ("SPY", d, "C", Decimal("62.5")))
    c = parse_occ(format_occ("SPY", d, "C", "62.500"))
    assert a == b == c
    assert len({a, b, c}) == 1
