"""Spread model + the four pricers.

The properties that matter most: costs are never understated, the synthetic
bar's high/low are exact under monotonicity, the live pricer NEVER silently
falls back to a model, and a part-recorded backtest reports itself honestly as
mixed."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.options.contracts import Contract, ContractQuote, Greeks
from app.options.expiry import expiry_to_ms
from app.options.pricing import (ChainedPricer, LivePricer, RecordedPricer,
                                 SyntheticPricer, breakeven, implied_move,
                                 payoff_at_expiry, position_value, premium_at_risk,
                                 transact_price)
from app.options.spread import (DEFAULT_COMMISSION_PER_CONTRACT, MAX_SPREAD_PCT,
                                TIER_ILLIQUID, TIER_INDEX_ETF, TIER_LIQUID,
                                TIER_MEGA_CAP, SpreadParams, fill_price,
                                quote_from_mid, round_trip_cost, spread_pct,
                                tick_size, tier_for)
from app.options.volsurface import SurfaceParams, VolSurface

EXPIRY = dt.date(2026, 9, 18)
# 2026-08-19 16:00 ET -> 30 days to expiry
TS = expiry_to_ms(dt.date(2026, 8, 19))


def call(strike=680.0, expiry=EXPIRY) -> Contract:
    return Contract(underlying="SPY", expiry=expiry, right="C",
                    strike=Decimal(str(strike)))


def put(strike=660.0, expiry=EXPIRY) -> Contract:
    return Contract(underlying="SPY", expiry=expiry, right="P",
                    strike=Decimal(str(strike)))


def mk_pricer(underlying="SPY", atm=0.18, tier=None) -> SyntheticPricer:
    surface = VolSurface(underlying=underlying, atm_30=atm,
                         params=SurfaceParams.default_for(underlying), source="test")
    return SyntheticPricer(surface=surface,
                           spread=SpreadParams.default_for(underlying, tier))


# ---- tiers and spreads ----------------------------------------------------

def test_liquidity_tiers():
    assert tier_for("SPY") == TIER_INDEX_ETF
    assert tier_for("AAPL") == TIER_MEGA_CAP
    assert tier_for("KO") == TIER_LIQUID
    assert tier_for("SOMEJUNK") == TIER_ILLIQUID


def test_unknown_symbols_get_the_widest_tier():
    """Assuming a name is more liquid than it is understates costs — the
    dangerous direction."""
    assert tier_for("") == TIER_ILLIQUID
    assert tier_for("XYZQ") == TIER_ILLIQUID


def test_spread_widens_for_wings_and_short_tenors():
    p = SpreadParams.default_for("SPY")
    atm_30 = spread_pct(p, 0.0, 30)
    assert spread_pct(p, 2.0, 30) > atm_30       # wings wider
    assert spread_pct(p, 0.0, 3) > atm_30        # short-dated wider
    assert spread_pct(p, 0.0, 180) < atm_30      # long-dated tighter


def test_spread_ordering_across_tiers():
    for m, d in ((0.0, 30), (1.5, 7)):
        s1 = spread_pct(SpreadParams.default_for("SPY"), m, d)
        s2 = spread_pct(SpreadParams.default_for("AAPL"), m, d)
        s3 = spread_pct(SpreadParams.default_for("KO"), m, d)
        s4 = spread_pct(SpreadParams.default_for("JUNK"), m, d)
        assert s1 < s2 < s3 < s4


def test_spread_is_clamped():
    p = SpreadParams.default_for("JUNK")
    assert spread_pct(p, 100.0, 1) <= MAX_SPREAD_PCT


def test_index_etf_atm_spread_is_realistic():
    """SPY 30-day ATM options really do quote under ~1% of mid."""
    assert 0.002 < spread_pct(SpreadParams.default_for("SPY"), 0.0, 30) < 0.012


def test_spread_is_floored_at_one_tick():
    """A modelled 0.4% spread on a $0.30 option is a tenth of a cent, which no
    real market quotes. Cheap far-OTM options are exactly where a naive percent
    model understates costs most, and exactly where strategies like to trade."""
    p = SpreadParams.default_for("SPY")
    bid, ask = quote_from_mid(0.30, p, 0.0, 30)
    assert ask - bid >= 0.01 - 1e-12


def test_tick_size_follows_penny_pilot_rules():
    assert tick_size(1.50) == 0.01
    assert tick_size(5.00) == 0.05
    assert tick_size(1.50, penny_pilot=False) == 0.05


def test_quote_from_mid_is_symmetric_and_non_negative():
    p = SpreadParams.default_for("SPY")
    bid, ask = quote_from_mid(6.45, p, 0.0, 30)
    assert bid < 6.45 < ask
    assert (6.45 - bid) == pytest.approx(ask - 6.45)
    assert quote_from_mid(0.0, p, 0.0, 30) == (0.0, 0.0)
    bid2, _ = quote_from_mid(0.01, p, 0.0, 1)
    assert bid2 >= 0.0


# ---- fills ----------------------------------------------------------------

def test_full_aggression_crosses_the_spread_both_ways():
    """The default. Assuming a passive mid fill that may never happen is how a
    backtest quietly awards itself free money on every trade."""
    assert fill_price(6.40, 6.50, "open", 1.0) == pytest.approx(6.50)
    assert fill_price(6.40, 6.50, "close", 1.0) == pytest.approx(6.40)


def test_zero_aggression_fills_at_mid():
    assert fill_price(6.40, 6.50, "open", 0.0) == pytest.approx(6.45)
    assert fill_price(6.40, 6.50, "close", 0.0) == pytest.approx(6.45)


def test_opening_never_costs_less_than_closing_receives():
    """The spread must always work against the trader, at every aggression."""
    for agg in (0.0, 0.25, 0.5, 1.0):
        assert fill_price(6.40, 6.50, "open", agg) >= fill_price(6.40, 6.50, "close", agg)


def test_close_price_never_negative():
    assert fill_price(0.0, 0.02, "close", 1.0) >= 0.0


# ---- the cost arithmetic every strategy must clear ------------------------

def test_round_trip_cost_on_a_liquid_option():
    """~1-2% of premium round trip on a $3 SPY call. Any strategy whose edge is
    thinner than this is a cost-noise generator."""
    c = round_trip_cost(3.00, SpreadParams.default_for("SPY"), 0.0, 30)
    assert c["premium"] == pytest.approx(300.0)
    assert c["commission"] == pytest.approx(2 * DEFAULT_COMMISSION_PER_CONTRACT)
    assert 0.5 < c["pct_of_premium"] < 3.0


def test_round_trip_cost_on_an_illiquid_option_is_brutal():
    c = round_trip_cost(3.00, SpreadParams.default_for("JUNK"), 1.0, 7)
    assert c["pct_of_premium"] > 10.0


def test_round_trip_cost_scales_with_quantity():
    p = SpreadParams.default_for("SPY")
    one = round_trip_cost(3.00, p, 0.0, 30, qty=1)
    ten = round_trip_cost(3.00, p, 0.0, 30, qty=10)
    assert ten["total"] == pytest.approx(one["total"] * 10)
    assert ten["pct_of_premium"] == pytest.approx(one["pct_of_premium"])


# ---- synthetic pricer -----------------------------------------------------

def test_synthetic_quote_is_sane():
    p = mk_pricer()
    q = p.quote(call(680.0), 672.30, TS)
    assert q is not None
    assert q.source == "synthetic"
    assert q.bid < q.mid < q.ask
    assert q.mid > 0
    assert 0.0 < q.greeks.delta < 1.0
    assert q.greeks.theta < 0            # long options decay
    assert q.iv > 0


def test_synthetic_put_has_negative_delta():
    q = mk_pricer().quote(put(660.0), 672.30, TS)
    assert q is not None and -1.0 < q.greeks.delta < 0.0


def test_synthetic_quote_rejects_bad_underlying():
    p = mk_pricer()
    assert p.quote(call(), 0.0, TS) is None
    assert p.quote(call(), -5.0, TS) is None


def test_synthetic_bar_high_low_are_exact_under_monotonicity():
    """A call's high is its price at the underlying's HIGH; a put's high is its
    price at the underlying's LOW. Exact at fixed sigma/T."""
    p = mk_pricer()
    o, h, lo, c = 670.0, 678.0, 665.0, 674.0

    cb = p.bar(call(680.0), o, h, lo, c, TS)
    assert cb is not None
    assert cb.high == pytest.approx(p.quote(call(680.0), h, TS).mid)
    assert cb.low == pytest.approx(p.quote(call(680.0), lo, TS).mid)
    assert cb.low <= cb.open <= cb.high and cb.low <= cb.close <= cb.high

    pb = p.bar(put(660.0), o, h, lo, c, TS)
    assert pb is not None
    assert pb.high == pytest.approx(p.quote(put(660.0), lo, TS).mid)
    assert pb.low == pytest.approx(p.quote(put(660.0), h, TS).mid)
    assert pb.low <= pb.open <= pb.high and pb.low <= pb.close <= pb.high


def test_synthetic_bar_rejects_bad_input():
    assert mk_pricer().bar(call(), 0.0, 10.0, 5.0, 8.0, TS) is None


def test_synthetic_option_loses_value_as_expiry_approaches():
    """Theta made visible: the same contract, same spot, closer to expiry."""
    p = mk_pricer()
    far = p.quote(call(680.0), 672.30, expiry_to_ms(dt.date(2026, 7, 20)))
    near = p.quote(call(680.0), 672.30, expiry_to_ms(dt.date(2026, 9, 10)))
    assert far.mid > near.mid


def test_higher_vol_makes_options_more_expensive():
    cheap = mk_pricer(atm=0.10).quote(call(680.0), 672.30, TS)
    rich = mk_pricer(atm=0.40).quote(call(680.0), 672.30, TS)
    assert rich.mid > cheap.mid


# ---- recorded pricer ------------------------------------------------------

def _rec_quote(sym_contract: Contract, ts: int, mid: float) -> ContractQuote:
    return ContractQuote(contract=sym_contract, ts=ts, mid=mid, bid=mid - 0.05,
                         ask=mid + 0.05, underlying_px=672.30, iv=0.18,
                         greeks=Greeks(delta=0.35), source="recorded")


def test_recorded_pricer_returns_exact_quotes():
    r = RecordedPricer()
    c = call(680.0)
    r.add(_rec_quote(c, TS, 6.45))
    r.finalize()
    q = r.quote(c, 672.30, TS)
    assert q is not None and q.mid == 6.45 and q.source == "recorded"


def test_recorded_pricer_never_looks_forward():
    """Using a later snapshot to price an earlier bar is look-ahead, and in a
    backtest that is indistinguishable from cheating."""
    r = RecordedPricer()
    c = call(680.0)
    r.add(_rec_quote(c, TS + 3600_000, 9.99))
    r.finalize()
    assert r.quote(c, 672.30, TS) is None


def test_recorded_pricer_respects_tolerance():
    r = RecordedPricer(tolerance_ms=3600_000)
    c = call(680.0)
    r.add(_rec_quote(c, TS - 10 * 3600_000, 6.45))
    r.finalize()
    assert r.quote(c, 672.30, TS) is None


def test_recorded_pricer_returns_none_for_unknown_contract():
    r = RecordedPricer()
    r.finalize()
    assert r.quote(call(680.0), 672.30, TS) is None


# ---- live pricer: the safety property -------------------------------------

class FakeRecorder:
    def __init__(self, q=None):
        self.q = q
        self.calls = []

    def latest_quote(self, symbol, max_age_s=15.0):
        self.calls.append((symbol, max_age_s))
        return self.q


def test_live_pricer_prefers_the_stream():
    c = call(680.0)
    streamed = _rec_quote(c, TS, 6.45)
    lp = LivePricer(recorder=FakeRecorder(streamed))
    assert lp.quote(c, 672.30, TS) is streamed


def test_live_pricer_falls_back_to_rest_but_never_to_a_model():
    """A live order priced off Black-Scholes rather than the actual book could
    be sent at a limit nowhere near where the contract trades. None means 'no
    usable market' and the runner must BLOCK on it."""
    c = call(680.0)
    lp = LivePricer(recorder=FakeRecorder(None))
    assert lp.quote(c, 672.30, TS) is None       # no synthetic fallback

    rest = _rec_quote(c, TS, 6.45)
    lp.set_rest_quote(rest)
    assert lp.quote(c, 672.30, TS) is rest


def test_live_pricer_works_without_a_recorder():
    assert LivePricer().quote(call(), 672.30, TS) is None


# ---- chained pricer: honest reporting -------------------------------------

def test_chained_prefers_recorded_and_reports_the_mix():
    rec = RecordedPricer()
    c1, c2 = call(680.0), call(690.0)
    rec.add(_rec_quote(c1, TS, 6.45))
    rec.finalize()
    ch = ChainedPricer(recorded=rec, synthetic=mk_pricer())

    q1 = ch.quote(c1, 672.30, TS)
    q2 = ch.quote(c2, 672.30, TS)
    assert q1.source == "recorded"
    assert q2.source == "synthetic"

    mix = ch.mix()
    assert mix["pricing_source"] == "mixed"
    assert mix["pct_bars_recorded"] == pytest.approx(50.0)
    assert mix["quotes_recorded"] == 1 and mix["quotes_synthetic"] == 1


def test_chained_labels_a_fully_synthetic_run_honestly():
    ch = ChainedPricer(recorded=RecordedPricer(), synthetic=mk_pricer())
    ch.quote(call(680.0), 672.30, TS)
    assert ch.mix()["pricing_source"] == "synthetic"
    assert ch.mix()["pct_bars_recorded"] == 0.0


def test_chained_labels_a_fully_recorded_run_honestly():
    rec = RecordedPricer()
    c = call(680.0)
    rec.add(_rec_quote(c, TS, 6.45))
    rec.finalize()
    ch = ChainedPricer(recorded=rec, synthetic=mk_pricer())
    ch.quote(c, 672.30, TS)
    assert ch.mix()["pricing_source"] == "recorded"
    assert ch.mix()["pct_bars_recorded"] == 100.0


def test_chained_mix_with_no_quotes():
    ch = ChainedPricer(recorded=RecordedPricer(), synthetic=mk_pricer())
    assert ch.mix()["pricing_source"] == "none"


# ---- shared helpers -------------------------------------------------------

def test_position_value_applies_the_multiplier():
    """Omitting the multiplier understates every options position by 100x —
    the single easiest and most expensive mistake in this codebase."""
    q = _rec_quote(call(680.0), TS, 6.45)
    assert position_value(q, 3, 100.0) == pytest.approx(1935.0)


def test_premium_at_risk_is_the_max_loss():
    """The property the whole safety argument rests on: a long option cannot
    lose more than the premium, known exactly at entry."""
    assert premium_at_risk(6.45, 3, 100.0) == pytest.approx(1935.0)


def test_transact_price_uses_the_adverse_side():
    q = _rec_quote(call(680.0), TS, 6.45)
    assert transact_price(q, "open") == pytest.approx(q.ask)
    assert transact_price(q, "close") == pytest.approx(q.bid)


def test_breakeven():
    assert breakeven(call(680.0), 6.45) == pytest.approx(686.45)
    assert breakeven(put(660.0), 6.45) == pytest.approx(653.55)


def test_payoff_at_expiry():
    c = call(680.0)
    assert payoff_at_expiry(c, 700.0, 6.45, 1) == pytest.approx((20.0 - 6.45) * 100)
    assert payoff_at_expiry(c, 650.0, 6.45, 1) == pytest.approx(-645.0)
    p = put(660.0)
    assert payoff_at_expiry(p, 640.0, 6.45, 1) == pytest.approx((20.0 - 6.45) * 100)
    assert payoff_at_expiry(p, 700.0, 6.45, 1) == pytest.approx(-645.0)


def test_payoff_loss_is_capped_at_the_premium():
    c = call(680.0)
    for spot in (0.01, 100.0, 500.0, 679.0):
        assert payoff_at_expiry(c, spot, 6.45, 1) >= -645.0 - 1e-9


def test_implied_move():
    q = _rec_quote(call(680.0), TS, 6.45)
    m = implied_move(q, 30)
    assert m is not None and 4.0 < m < 6.5
    assert implied_move(ContractQuote(contract=call(), ts=TS, mid=1, bid=1, ask=1,
                                      underlying_px=672.3, iv=0.0), 30) is None
