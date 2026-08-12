"""Chain flattening + validation.

Built around a payload shaped exactly like Schwab's documented `OptionChain`
(schwab/market-data-production/README.md §OptionChain / §OptionContract). The
validation here is the same class of guard that caught the CHART_EQUITY field
shift and the crossed-market quotes in streamer/recorder.py — so each rejection
path gets its own test rather than being assumed to work.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.options.chain_service import (MAX_ABS_DELTA, _normalize_iv, flatten_chain,
                                       parse_expiration_chain, quote_from_schwab_quote)
from app.options.expiry import expiry_to_ms

NOW = 1_785_000_000_000  # arbitrary fixed epoch ms


def _contract(symbol, *, put_call="CALL", strike=680.0, bid=6.40, ask=6.50,
              delta=0.35, quote_ts=None, **extra):
    d = {
        "putCall": put_call, "symbol": symbol, "description": "SPY Sep 18 2026 680 Call",
        # `bid`/`ask`/`last`, NOT bidPrice/askPrice — this fixture is shaped
        # from a captured live /chains response. The original fixture used the
        # /quotes names, which is why the whole suite stayed green while every
        # real contract was being dropped as `missing_quote`.
        "exchangeName": "OPR", "bid": bid, "ask": ask, "last": 6.45,
        "mark": (bid + ask) / 2, "bidSize": 40, "askSize": 55, "lastSize": 3,
        "highPrice": 7.1, "lowPrice": 6.1, "openPrice": 6.7, "closePrice": 6.6,
        "totalVolume": 2400, "openInterest": 15800, "volatility": 18.42,
        "delta": delta, "gamma": 0.0121, "theta": -0.0850, "vega": 0.6120, "rho": 0.2100,
        "timeValue": 6.45, "theoreticalOptionValue": 6.44, "intrinsicValue": 0.0,
        "strikePrice": strike, "expirationDate": "2026-09-18T20:00:00.000+00:00",
        "daysToExpiration": 50, "expirationType": "S", "multiplier": 100.0,
        "settlementType": "P", "isPennyPilot": True, "isInTheMoney": False,
        "isMini": False, "isNonStandard": False, "isIndexOption": False,
        "quoteTimeInLong": quote_ts if quote_ts is not None else NOW - 5_000,
        "tradeTimeInLong": NOW - 30_000, "optionRoot": "SPY",
    }
    d.update(extra)
    return d


def _payload(calls=None, puts=None, underlying_px=672.30):
    return {
        "symbol": "SPY", "status": "SUCCESS", "strategy": "SINGLE",
        "underlyingPrice": underlying_px, "isDelayed": False, "isIndex": False,
        "underlying": {"symbol": "SPY", "mark": underlying_px, "last": underlying_px},
        "callExpDateMap": {"2026-09-18:50": {"680.0": calls or []}},
        "putExpDateMap": {"2026-09-18:50": {"660.0": puts or []}},
    }


# ---- happy path -----------------------------------------------------------

def test_flatten_extracts_calls_and_puts():
    call = _contract("SPY   260918C00680000")
    put = _contract("SPY   260918P00660000", put_call="PUT", strike=660.0, delta=-0.30)
    res = flatten_chain(_payload([call], [put]), underlying="SPY", ts=NOW)

    assert res.error is None
    assert res.requested == 2
    assert res.rejected == 0
    assert len(res.rows) == 2
    by_right = {r.opt_right: r for r in res.rows}
    assert by_right["C"].strike == 680.0
    assert by_right["P"].strike == 660.0
    assert by_right["C"].symbol == "SPY   260918C00680000"
    assert by_right["C"].expiry == expiry_to_ms(dt.date(2026, 9, 18))
    assert by_right["C"].underlying_px == pytest.approx(672.30)
    assert by_right["C"].open_interest == 15800
    assert by_right["C"].ts == NOW


def test_iv_is_converted_from_percent_to_decimal():
    """Schwab reports chain volatility in PERCENTAGE POINTS. Storing 18.42
    instead of 0.1842 would make every calibration wrong by 100x — and it looks
    'nearly right' on a low-vol instrument, which is the worst kind of wrong."""
    res = flatten_chain(_payload([_contract("SPY   260918C00680000", volatility=18.42)]),
                        underlying="SPY", ts=NOW)
    assert res.rows[0].iv == pytest.approx(0.1842)


@pytest.mark.parametrize("raw,expected", [
    (18.42, 0.1842), (95.0, 0.95), (0.185, 0.185), (2.5, 2.5),
    (None, None), (0, None), (-1, None),
])
def test_normalize_iv(raw, expected):
    got = _normalize_iv(raw)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_greeks_sentinel_minus_999_becomes_none():
    """Schwab sends -999.0 for 'not available'. Storing it as a delta would put
    a contract at -999 delta, which passes every range check downstream."""
    res = flatten_chain(_payload([_contract("SPY   260918C00680000", delta=-999.0,
                                            gamma=-999.0)]),
                        underlying="SPY", ts=NOW)
    assert res.rows[0].delta is None
    assert res.rows[0].gamma is None


# ---- rejection paths ------------------------------------------------------

def _reject_reason(contract, **kw):
    res = flatten_chain(_payload([contract]), underlying="SPY", ts=NOW, **kw)
    assert res.rejected == 1, res.reject_reasons
    assert not res.rows
    return next(iter(res.reject_reasons))


def test_rejects_crossed_market():
    assert _reject_reason(_contract("SPY   260918C00680000", bid=7.0, ask=6.0)) \
        == "crossed_market"


def test_rejects_non_positive_ask():
    assert _reject_reason(_contract("SPY   260918C00680000", bid=0.0, ask=0.0)) \
        == "non_positive_quote"


def test_rejects_stale_quote():
    """An illiquid strike that has not quoted in hours has a price that is a
    memory, not a market."""
    stale = _contract("SPY   260918C00680000", quote_ts=NOW - 2 * 3600 * 1000)
    assert _reject_reason(stale) == "stale_quote"


def test_rejects_root_mismatch():
    """`AAPL1`-style adjusted roots leaking into an AAPL chain would be stored
    under the wrong underlying, and an adjusted contract does not deliver 100
    shares."""
    res = flatten_chain(_payload([_contract("SPY1  260918C00680000")]),
                        underlying="SPY", ts=NOW)
    assert res.reject_reasons["root_mismatch"] == 1


def test_rejects_strike_disagreeing_with_symbol():
    bad = _contract("SPY   260918C00680000", strikePrice=681.0)
    assert _reject_reason(bad) == "strike_disagrees_with_symbol"


def test_rejects_right_disagreeing_with_symbol():
    bad = _contract("SPY   260918P00680000", put_call="CALL")
    reasons = flatten_chain(_payload([bad]), underlying="SPY", ts=NOW).reject_reasons
    assert reasons  # either right_disagrees_with_symbol or right_in_wrong_map
    assert "right_disagrees_with_symbol" in reasons or "right_in_wrong_map" in reasons


def test_rejects_malformed_occ_symbol():
    assert _reject_reason(_contract("NOTASYMBOL")) == "bad_occ_symbol"


def test_rejects_non_standard_and_mini_contracts():
    """v1 excludes them rather than mispricing them (plan/16 D10)."""
    assert _reject_reason(_contract("SPY   260918C00680000", isNonStandard=True)) \
        == "non_standard"
    assert _reject_reason(_contract("SPY   260918C00680000", isMini=True)) == "mini"


def test_rejects_deep_wings():
    assert _reject_reason(_contract("SPY   260918C00680000", delta=0.001)) == "deep_wing"
    assert _reject_reason(_contract("SPY   260918C00680000",
                                    delta=MAX_ABS_DELTA + 0.005)) == "deep_wing"


def test_deep_wings_kept_when_filter_disabled():
    res = flatten_chain(_payload([_contract("SPY   260918C00680000", delta=0.001)]),
                        underlying="SPY", ts=NOW, drop_deep_wings=False)
    assert res.rejected == 0 and len(res.rows) == 1


def test_rejection_reasons_are_counted_not_swallowed():
    """A silent 40% rejection rate must be visible. Counts are what the Data
    page surfaces."""
    rows = [
        _contract("SPY   260918C00680000"),
        _contract("SPY   260918C00681000", bid=9.0, ask=8.0, strike=681.0,
                  strikePrice=681.0),
        _contract("SPY   260918C00682000", strike=682.0, strikePrice=682.0,
                  quote_ts=NOW - 3600 * 1000 * 3),
    ]
    res = flatten_chain(_payload(rows), underlying="SPY", ts=NOW)
    assert res.requested == 3
    assert res.written == 1
    assert res.rejected == 2
    assert res.reject_reasons["crossed_market"] == 1
    assert res.reject_reasons["stale_quote"] == 1


# ---- robustness -----------------------------------------------------------

def test_garbage_payloads_do_not_raise():
    for bad in [None, [], "nope", 42, {}, {"callExpDateMap": "not-a-dict"},
                {"callExpDateMap": {"k": "not-a-dict"}},
                {"callExpDateMap": {"k": {"680.0": "not-a-list"}}},
                {"callExpDateMap": {"k": {"680.0": [None, 5, "x"]}}}]:
        res = flatten_chain(bad, underlying="SPY", ts=NOW)
        assert res.rows == []


def test_non_success_status_is_reported_but_rows_still_parsed():
    payload = _payload([_contract("SPY   260918C00680000")])
    payload["status"] = "PARTIAL"
    res = flatten_chain(payload, underlying="SPY", ts=NOW)
    assert res.error and "PARTIAL" in res.error
    assert len(res.rows) == 1


def test_underlying_price_falls_back_through_the_underlying_node():
    payload = _payload([_contract("SPY   260918C00680000")])
    del payload["underlyingPrice"]
    res = flatten_chain(payload, underlying="SPY", ts=NOW)
    assert res.underlying_px == pytest.approx(672.30)


# ---- expiration chain -----------------------------------------------------

def test_parse_expiration_chain():
    # `expirationDate` is the key the wire actually sends. This fixture used the
    # spec's `expiration` instead, so the parser matched the fixture and
    # returned [] for every real symbol — and an empty list looks exactly like
    # "this underlying has no options", which is why nothing caught it.
    payload = {"status": "SUCCESS", "expirationList": [
        {"expirationDate": "2026-08-21", "daysToExpiration": 22, "expirationType": "S",
         "standard": True, "settlementType": "P", "optionRoots": "SPY"},
        {"expirationDate": "2026-09-18", "daysToExpiration": 50, "expirationType": "Q",
         "standard": True, "settlementType": "P", "optionRoots": "SPY"},
        {"expirationDate": "bogus", "expirationType": "W"},
        {"expirationType": "W"},
    ]}
    rows = parse_expiration_chain(payload)
    assert len(rows) == 2
    assert rows[0]["expiry"] == expiry_to_ms(dt.date(2026, 8, 21))
    assert rows[1]["expiration_type"] == "Q"


def test_parse_expiration_chain_handles_empty():
    assert parse_expiration_chain({}) == []
    assert parse_expiration_chain(None) == []


# ---- live single quote ----------------------------------------------------

def test_quote_from_schwab_quote():
    payload = {
        "assetMainType": "OPTION",
        "quote": {"bidPrice": 6.40, "askPrice": 6.50, "mark": 6.45, "volatility": 18.42,
                  "delta": 0.35, "gamma": 0.012, "theta": -0.085, "vega": 0.61,
                  "rho": 0.21, "openInterest": 15800, "totalVolume": 2400,
                  "underlyingPrice": 672.30, "quoteTime": NOW},
        "reference": {"contractType": "C", "strikePrice": 680.0, "multiplier": 100.0},
    }
    q = quote_from_schwab_quote("SPY   260918C00680000", payload)
    assert q is not None
    assert q.source == "live"
    assert q.bid == 6.40 and q.ask == 6.50
    assert q.iv == pytest.approx(0.1842)
    assert q.greeks.delta == pytest.approx(0.35)
    assert q.spread == pytest.approx(0.10)
    assert q.spread_pct == pytest.approx(0.10 / 6.45 * 100)
    # A LONG position buys at the ask and sells at the bid — the adverse side
    # in both directions, matching the equity engine's slippage convention.
    assert q.price_for("open") == 6.50
    assert q.price_for("close") == 6.40


@pytest.mark.parametrize("quote", [
    {}, {"bidPrice": 6.4}, {"askPrice": 6.5},
    {"bidPrice": 7.0, "askPrice": 6.0},        # crossed
    {"bidPrice": 0.0, "askPrice": 0.0},        # no market
    {"bidPrice": -1.0, "askPrice": 6.5},
])
def test_unusable_live_quotes_return_none(quote):
    """None is a MEANINGFUL answer: the runner blocks the trade rather than
    falling back to a modelled price (plan/16 §9.2)."""
    assert quote_from_schwab_quote("SPY   260918C00680000", {"quote": quote}) is None


def test_live_quote_rejects_non_option_symbol():
    assert quote_from_schwab_quote("SPY", {"quote": {"bidPrice": 1, "askPrice": 2}}) is None


# ---- cash-settled index products ------------------------------------------
#
# SPX broke both of the assumptions the ETF path is built on: the request
# symbol is `$SPX` (plain `SPX` is a 400 from Schwab), and the contracts carry
# the roots SPX/SPXW rather than the underlying's own name.

def _spx_payload(contracts, underlying_px=7587.0):
    return {
        "symbol": "$SPX", "status": "SUCCESS", "strategy": "SINGLE",
        "underlyingPrice": underlying_px, "isDelayed": False, "isIndex": True,
        "underlying": {"symbol": "$SPX", "mark": underlying_px},
        "callExpDateMap": {"2026-09-18:50": {"7585.0": contracts}},
        "putExpDateMap": {},
    }


def test_spxw_weeklies_are_accepted_under_spx():
    """The whole SPX chain is SPXW on a normal day. Rejecting it as a root
    mismatch is what left the chain empty."""
    c = _contract("SPXW  260918C07585000", strike=7585.0, optionRoot="SPXW")
    res = flatten_chain(_spx_payload([c]), underlying="SPX", ts=NOW)

    assert res.written == 1, dict(res.reject_reasons)
    row = res.rows[0]
    assert row.symbol == "SPXW  260918C07585000"   # symbol kept verbatim
    assert row.underlying == "SPX"                 # but filed under the index


def test_spx_am_settled_monthlies_also_accepted():
    c = _contract("SPX   260918C07585000", strike=7585.0, optionRoot="SPX")
    assert flatten_chain(_spx_payload([c]), underlying="SPX", ts=NOW).written == 1


def test_request_symbol_form_is_normalised_away():
    """Passing the `$SPX` request symbol must file rows under `SPX`, not
    `$SPX` — otherwise the store grows two keys for one index."""
    c = _contract("SPXW  260918C07585000", strike=7585.0, optionRoot="SPXW")
    res = flatten_chain(_spx_payload([c]), underlying="$SPX", ts=NOW)
    assert res.underlying == "SPX"
    assert res.rows[0].underlying == "SPX"


def test_unrelated_index_root_still_rejected():
    """The root check must stay a real check: NDXP does not settle against SPX,
    and accepting it would mix two indices in one chain."""
    c = _contract("NDXP  260918C07585000", strike=7585.0, optionRoot="NDXP")
    res = flatten_chain(_spx_payload([c]), underlying="SPX", ts=NOW)
    assert res.written == 0
    assert res.reject_reasons["root_mismatch"] == 1


def test_adjusted_equity_root_still_rejected():
    """SPXW-for-SPX must not have loosened the AAPL1-for-AAPL guard: an
    adjusted contract does not deliver 100 shares."""
    c = _contract("AAPL1 260918C00680000")
    payload = _payload([c])
    payload["symbol"] = "AAPL"
    res = flatten_chain(payload, underlying="AAPL", ts=NOW)
    assert res.written == 0
    assert res.reject_reasons["root_mismatch"] == 1


def test_parse_expiration_chain_accepts_the_documented_key_too():
    """The spec's field table says `expiration`; the wire says `expirationDate`.
    Both parse, so a fixture captured from either source works."""
    rows = parse_expiration_chain({"expirationList": [
        {"expiration": "2026-08-21", "expirationType": "S", "standard": True}]})
    assert len(rows) == 1


def test_parse_expiration_chain_real_wire_shape():
    """Regression: a captured live /expirationchain response must not parse to
    an empty list. The chain browser hid its entire expiry selector because
    this returned [] — the UI gated on a non-empty list."""
    captured = {"status": "SUCCESS", "expirationList": [
        {"expirationDate": "2026-08-03", "daysToExpiration": 0,
         "expirationType": "W", "settlementType": "P",
         "optionRoots": "SPY", "standard": True},
        {"expirationDate": "2026-08-04", "daysToExpiration": 1,
         "expirationType": "W", "settlementType": "P",
         "optionRoots": "SPY", "standard": True},
    ]}
    rows = parse_expiration_chain(captured)
    assert len(rows) == 2
    assert rows[0]["expiry"] == expiry_to_ms(dt.date(2026, 8, 3))
    assert rows[0]["settlement_type"] == "P"
