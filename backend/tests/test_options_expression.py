"""Contract selection, option sizing, and the options lint.

Selection is shared by the engine and the runner for the same reason sizing and
stops are: if it were implemented twice, a strategy would eventually pick a
different contract in backtest than in life and the backtest would stop meaning
anything."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.options.contracts import Contract
from app.options.expiry import expiry_to_ms, generate_expirations
from app.options.pricing import SyntheticPricer
from app.options.spread import SpreadParams
from app.options.volsurface import SurfaceParams, VolSurface
from app.strategy.expression import (REASON_NO_EXPIRY, REASON_NO_QUOTE,
                                     REASON_NO_STRIKE, REASON_OI_TOO_LOW,
                                     REASON_SPREAD_TOO_WIDE, Rejection, RealChainView,
                                     Selection, SyntheticChainView, right_for_side,
                                     select_contract, select_expiry, target_strike)
from app.strategy.lint import lint
from app.strategy.schema import (Entry, ExpirySelector, Expression, Liquidity,
                                 OptionExit, OptionRisk, PricingCfg, RuleBlock,
                                 SideExpression, Sizing, StrategyDocument,
                                 StrikeSelector)
from app.strategy.sizing import (BOUND_CASH, BOUND_MAX_CONTRACTS,
                                 BOUND_MAX_POSITION_NOTIONAL, BOUND_PREMIUM_AT_RISK,
                                 BOUND_PREMIUM_PER_TRADE, compute_contract_qty)

TODAY = dt.date(2026, 7, 30)
TS = expiry_to_ms(TODAY)
SPOT = 672.0


def pricer(atm=0.18, underlying="SPY") -> SyntheticPricer:
    return SyntheticPricer(
        surface=VolSurface(underlying=underlying, atm_30=atm, source="test",
                           params=SurfaceParams.default_for(underlying)),
        spread=SpreadParams.default_for(underlying))


def synth_view(underlying="SPY", atm=0.18) -> SyntheticChainView:
    return SyntheticChainView(underlying=underlying, pricer=pricer(atm, underlying))


def side(right="call", **kw) -> SideExpression:
    kw.setdefault("expiry", ExpirySelector())
    kw.setdefault("strike", StrikeSelector())
    return SideExpression(right=right, **kw)


# ---- expiry selection -----------------------------------------------------

def test_selects_the_expiry_nearest_the_target():
    v = synth_view()
    exp = select_expiry(v, side(expiry=ExpirySelector(dte_min=25, dte_target=35,
                                                      dte_max=55)), TS)
    assert not isinstance(exp, Rejection)
    from app.options.expiry import days_to_expiry
    assert 25 <= days_to_expiry(exp.expiry, TS) <= 55


def test_rejects_when_no_expiry_is_in_the_window():
    v = synth_view()
    exp = select_expiry(v, side(expiry=ExpirySelector(dte_min=700, dte_target=710,
                                                      dte_max=720)), TS)
    assert isinstance(exp, Rejection) and exp.reason == REASON_NO_EXPIRY


def test_expiry_allow_list_is_respected():
    """Weeklies are thin on most names; a document restricting to monthlies
    must not silently get one."""
    v = synth_view()
    exp = select_expiry(
        v, side(expiry=ExpirySelector(dte_min=0, dte_target=7, dte_max=14,
                                      allow=["S", "M", "Q"])), TS)
    if not isinstance(exp, Rejection):
        assert exp.expiration_type in ("S", "M", "Q")


def test_weeklies_are_available_when_allowed():
    v = synth_view()
    exp = select_expiry(
        v, side(expiry=ExpirySelector(dte_min=0, dte_target=7, dte_max=14,
                                      allow=["W", "S", "M", "Q"])), TS)
    assert not isinstance(exp, Rejection)


def test_recorded_expirations_win_over_generated():
    real = [e for e in generate_expirations("SPY", TODAY) if e.expiry.month == 9]
    v = SyntheticChainView(underlying="SPY", pricer=pricer(),
                           recorded_expirations=real)
    got = v.expirations(TS)
    assert got == real


# ---- strike selection -----------------------------------------------------

def test_delta_target_inverts_to_a_sensible_strike():
    """A 35-delta call sits above spot; a 35-delta put below."""
    exp = dt.date(2026, 9, 18)
    k_call = target_strike(StrikeSelector(by="delta", target=0.35), "C", SPOT, exp, TS)
    k_put = target_strike(StrikeSelector(by="delta", target=0.35), "P", SPOT, exp, TS)
    assert k_call > SPOT
    assert k_put < SPOT


def test_lower_delta_means_further_out_of_the_money():
    exp = dt.date(2026, 9, 18)
    k30 = target_strike(StrikeSelector(by="delta", target=0.30), "C", SPOT, exp, TS)
    k50 = target_strike(StrikeSelector(by="delta", target=0.50), "C", SPOT, exp, TS)
    assert k30 > k50


def test_moneyness_pct_is_direction_relative():
    exp = dt.date(2026, 9, 18)
    c = target_strike(StrikeSelector(by="moneyness_pct", target=5.0), "C", SPOT, exp, TS)
    p = target_strike(StrikeSelector(by="moneyness_pct", target=5.0), "P", SPOT, exp, TS)
    assert c == pytest.approx(SPOT * 1.05)
    assert p == pytest.approx(SPOT * 0.95)


def test_offset_dollars_and_atm():
    exp = dt.date(2026, 9, 18)
    assert target_strike(StrikeSelector(by="offset_dollars", target=10.0), "C",
                         SPOT, exp, TS) == pytest.approx(SPOT + 10)
    assert target_strike(StrikeSelector(by="atm", target=1.0), "C",
                         SPOT, exp, TS) == pytest.approx(SPOT)


def test_atr_multiple_requires_an_atr():
    exp = dt.date(2026, 9, 18)
    sel = StrikeSelector(by="atr_multiple", target=1.5, atr_indicator="atr14")
    assert isinstance(target_strike(sel, "C", SPOT, exp, TS, atr=None), Rejection)
    got = target_strike(sel, "C", SPOT, exp, TS, atr=8.0)
    assert got == pytest.approx(SPOT + 12.0)


# ---- full selection against a synthetic chain -----------------------------

def test_select_contract_end_to_end():
    got = select_contract(side("call"), Liquidity(min_open_interest=0),
                          synth_view(), "SPY", SPOT, TS)
    assert isinstance(got, Selection), got
    assert got.contract.underlying == "SPY"
    assert got.contract.is_call
    assert got.contract.strike > 0
    assert got.quote is not None and got.quote.source == "synthetic"
    assert 25 <= got.expiry_dte <= 55


def test_short_signal_buys_puts():
    """plan/16 D2: a 'short' signal is long premium — no margin, no borrow, and
    a loss capped at the debit. Wholly different from shorting shares."""
    got = select_contract(side("put"), Liquidity(min_open_interest=0),
                          synth_view(), "SPY", SPOT, TS)
    assert isinstance(got, Selection)
    assert got.contract.right == "P"
    assert got.quote.greeks.delta < 0


def test_selected_strike_is_always_encodable_as_occ():
    """Snapping exists so that a computed strike survives OCC encoding."""
    for spot in (7.3, 12.35, 61.9, 180.55, 321.21, 672.44, 1203.9):
        for underlying in ("SPY", "KO", "NVDA"):
            got = select_contract(side("call"), Liquidity(min_open_interest=0),
                                  synth_view(underlying), underlying, spot, TS)
            if isinstance(got, Selection):
                assert len(got.contract.symbol) == 21


def test_delta_selection_lands_near_the_requested_delta():
    got = select_contract(
        side("call", strike=StrikeSelector(by="delta", target=0.35, tolerance=0.10)),
        Liquidity(min_open_interest=0), synth_view(), "SPY", SPOT, TS)
    assert isinstance(got, Selection), got
    assert abs(abs(got.quote.greeks.delta) - 0.35) <= 0.10


def test_impossible_delta_tolerance_is_rejected_with_a_reason():
    """Silence is the enemy: a strategy taking 3 trades instead of 300 must be
    able to say why."""
    got = select_contract(
        side("call", strike=StrikeSelector(by="delta", target=0.35, tolerance=0.0001)),
        Liquidity(min_open_interest=0), synth_view(), "SPY", SPOT, TS)
    assert isinstance(got, Rejection)
    assert got.reason == REASON_NO_STRIKE
    assert "delta" in got.detail


def test_wide_spread_is_rejected():
    got = select_contract(
        side("call"), Liquidity(min_open_interest=0, max_spread_pct=0.001),
        synth_view("JUNK"), "JUNK", 20.0, TS)
    assert isinstance(got, Rejection)
    assert got.reason == REASON_SPREAD_TOO_WIDE


def test_no_underlying_price_is_rejected():
    got = select_contract(side("call"), Liquidity(), synth_view(), "SPY", 0.0, TS)
    assert isinstance(got, Rejection)


def test_rejection_is_falsy_and_selection_is_truthy():
    """Lets call sites read as `if got: ... else: log(got.reason)`."""
    assert not Rejection("x")
    assert Selection(contract=Contract(underlying="SPY", expiry=dt.date(2026, 9, 18),
                                       right="C", strike=Decimal("680")))


# ---- selection against a REAL chain ---------------------------------------

class Row:
    def __init__(self, symbol, expiry, strike, right, oi=5000, vol=100,
                 non_standard=0):
        self.symbol = symbol
        self.expiry = expiry
        self.strike = strike
        self.opt_right = right
        self.open_interest = oi
        self.volume = vol
        self.is_non_standard = non_standard


def real_view(oi=5000) -> RealChainView:
    exp = dt.date(2026, 9, 18)
    exp_ms = expiry_to_ms(exp)
    rows = []
    quotes = {}
    p = pricer()
    for k in range(650, 701, 5):
        for right in ("C", "P"):
            c = Contract(underlying="SPY", expiry=exp, right=right,
                         strike=Decimal(str(k)))
            rows.append(Row(c.symbol, exp_ms, float(k), right, oi=oi))
            quotes[c.symbol] = p.quote(c, SPOT, TS)
    return RealChainView(underlying="SPY", rows=rows, quotes=quotes)


def test_real_chain_selection_uses_listed_strikes_only():
    v = real_view()
    got = select_contract(
        side("call", expiry=ExpirySelector(dte_min=30, dte_target=50, dte_max=60),
             strike=StrikeSelector(by="moneyness_pct", target=1.0)),
        Liquidity(min_open_interest=0), v, "SPY", SPOT, TS)
    assert isinstance(got, Selection), got
    assert float(got.contract.strike) % 5 == 0


def test_open_interest_filter_applies_to_a_real_chain():
    v = real_view(oi=5)
    got = select_contract(
        side("call", expiry=ExpirySelector(dte_min=30, dte_target=50, dte_max=60)),
        Liquidity(min_open_interest=1000), v, "SPY", SPOT, TS)
    assert isinstance(got, Rejection)
    assert got.reason == REASON_OI_TOO_LOW


def test_real_chain_without_quotes_is_rejected_not_guessed():
    v = real_view()
    v.quotes = {}
    got = select_contract(
        side("call", expiry=ExpirySelector(dte_min=30, dte_target=50, dte_max=60)),
        Liquidity(min_open_interest=0), v, "SPY", SPOT, TS)
    assert isinstance(got, Rejection) and got.reason == REASON_NO_QUOTE


# ---- sizing ---------------------------------------------------------------

def sz(**kw) -> Sizing:
    kw.setdefault("type", "fixed_dollars")
    kw.setdefault("value", 5000)
    kw.setdefault("max_position_notional", 1_000_000)
    return Sizing(**kw)


def test_sizing_by_premium_dollars():
    r = compute_contract_qty(sz(type="fixed_dollars", value=3000),
                             OptionRisk(max_premium_at_risk=1e9,
                                        max_premium_per_trade_pct=100),
                             premium_per_contract=6.0, multiplier=100.0,
                             delta=0.35, underlying_px=SPOT,
                             equity=100_000, available_cash=100_000)
    assert r.qty == 5              # $3000 / ($6 x 100)
    assert r.premium == pytest.approx(3000.0)


def test_risk_based_sizing_needs_no_stop_distance():
    """The max loss IS the premium, known exactly at entry — simpler and
    stricter than the share path, where a stop can gap through."""
    r = compute_contract_qty(sz(type="risk_based", value=2.0),
                             OptionRisk(max_premium_at_risk=1e9,
                                        max_premium_per_trade_pct=100),
                             premium_per_contract=4.0, multiplier=100.0,
                             delta=0.35, underlying_px=SPOT,
                             equity=100_000, available_cash=100_000)
    assert r.qty == 5              # 2% of 100k = $2000 / $400
    assert r.premium == pytest.approx(2000.0)


def test_premium_per_trade_cap_binds_and_is_named():
    r = compute_contract_qty(sz(type="fixed_dollars", value=50_000),
                             OptionRisk(max_premium_per_trade_pct=2.0,
                                        max_premium_at_risk=1e9),
                             premium_per_contract=6.0, multiplier=100.0,
                             delta=0.35, underlying_px=SPOT,
                             equity=100_000, available_cash=1e9)
    assert r.qty == 3              # 2% of 100k = $2000 / $600
    assert r.bound_by == BOUND_PREMIUM_PER_TRADE


def test_portfolio_premium_at_risk_cap_binds():
    r = compute_contract_qty(sz(type="fixed_dollars", value=50_000),
                             OptionRisk(max_premium_at_risk=5_000,
                                        max_premium_per_trade_pct=100),
                             premium_per_contract=6.0, multiplier=100.0,
                             delta=0.35, underlying_px=SPOT,
                             equity=1e9, available_cash=1e9,
                             premium_at_risk_open=4_400)
    assert r.qty == 1              # only $600 of budget left
    assert r.bound_by == BOUND_PREMIUM_AT_RISK


def test_delta_adjusted_exposure_cap_binds():
    """The cap that catches what premium caps miss: 10 SPY 35-delta calls cost
    ~$3k of premium but carry ~$210k of underlying exposure."""
    r = compute_contract_qty(sz(type="fixed_dollars", value=50_000,
                                max_position_notional=50_000),
                             OptionRisk(max_premium_at_risk=1e9,
                                        max_premium_per_trade_pct=100),
                             premium_per_contract=6.0, multiplier=100.0,
                             delta=0.35, underlying_px=672.0,
                             equity=1e9, available_cash=1e9)
    # exposure per contract = 0.35 * 100 * 672 = $23,520
    assert r.qty == 2
    assert r.bound_by == BOUND_MAX_POSITION_NOTIONAL
    assert r.exposure == pytest.approx(2 * 23_520.0)


def test_max_contracts_backstop():
    r = compute_contract_qty(sz(type="fixed_dollars", value=1e9),
                             OptionRisk(max_premium_at_risk=1e12,
                                        max_premium_per_trade_pct=100,
                                        max_contracts_per_position=7),
                             premium_per_contract=1.0, multiplier=100.0,
                             delta=0.05, underlying_px=10.0,
                             equity=1e12, available_cash=1e12)
    assert r.qty == 7 and r.bound_by == BOUND_MAX_CONTRACTS


def test_cash_including_commission_binds():
    r = compute_contract_qty(sz(type="fixed_dollars", value=1e6),
                             OptionRisk(max_premium_at_risk=1e9,
                                        max_premium_per_trade_pct=100),
                             premium_per_contract=6.0, multiplier=100.0,
                             delta=0.35, underlying_px=SPOT,
                             equity=1e9, available_cash=1_000)
    assert r.qty == 1 and r.bound_by == BOUND_CASH


def test_zero_quantity_still_names_the_binding_cap():
    r = compute_contract_qty(sz(type="fixed_dollars", value=10),
                             OptionRisk(), premium_per_contract=6.0,
                             multiplier=100.0, delta=0.35, underlying_px=SPOT,
                             equity=1_000, available_cash=1_000)
    assert r.qty == 0
    assert r.bound_by      # never empty
    assert not r


def test_invalid_premium_returns_zero():
    for prem in (0.0, -1.0):
        r = compute_contract_qty(sz(), OptionRisk(), premium_per_contract=prem,
                                 multiplier=100.0, delta=0.35, underlying_px=SPOT,
                                 equity=1e6, available_cash=1e6)
        assert r.qty == 0


# ---- lint -----------------------------------------------------------------

def opts_doc(**expr_kw) -> StrategyDocument:
    expr_kw.setdefault("instrument", "options")
    expr_kw.setdefault("long", side("call"))
    return StrategyDocument(
        meta={"name": "Opt Test"}, universe={"symbols": ["SPY"]},
        entry=Entry(long=RuleBlock(when={"op": "gt", "left": {"price": "close"},
                                         "right": {"value": 1}})),
        sizing=sz(), expression=Expression(**expr_kw))


def test_shares_documents_get_no_options_lint():
    doc = StrategyDocument(
        meta={"name": "Shares"}, universe={"symbols": ["SPY"]},
        entry=Entry(long=RuleBlock(when={"op": "gt", "left": {"price": "close"},
                                         "right": {"value": 1}})),
        sizing=sz())
    assert not [p for p in lint(doc) if "option" in p.lower() or "expression" in p]


def test_options_without_any_expression_is_an_error():
    doc = opts_doc(long=None)
    problems = lint(doc)
    assert any("neither expression.long nor expression.short" in p for p in problems)


def test_signal_without_an_expression_is_an_error():
    """The most confusing possible failure: configured and silently inert."""
    doc = opts_doc()
    doc.entry.short = RuleBlock(when={"op": "lt", "left": {"price": "close"},
                                      "right": {"value": 1}})
    assert any("expression.short" in p and "dropped silently" in p for p in lint(doc))


def test_dte_window_is_validated():
    doc = opts_doc(long=side("call", expiry=ExpirySelector(dte_min=40, dte_target=10,
                                                           dte_max=60)))
    assert any("dte_target" in p for p in lint(doc))


def test_close_at_dte_that_would_close_on_entry_is_an_error():
    doc = opts_doc(long=side("call", expiry=ExpirySelector(dte_min=5, dte_target=7,
                                                           dte_max=10)),
                   exit=OptionExit(close_at_dte=10))
    assert any("close_at_dte" in p for p in lint(doc))


def test_vertical_without_wing_width_is_an_error():
    doc = opts_doc(long=side("call", structure="vertical_debit"))
    assert any("wing_width" in p for p in lint(doc))


def test_atr_strike_requires_a_declared_indicator():
    doc = opts_doc(long=side("call", strike=StrikeSelector(by="atr_multiple",
                                                           target=1.5,
                                                           atr_indicator="nope")))
    assert any("not a declared indicator" in p for p in lint(doc))


def test_atr_stop_with_premium_basis_is_an_error():
    """ATR measures the underlying's range; it is meaningless against premium."""
    from app.strategy.schema import StopSpec
    doc = opts_doc(exit=OptionExit(stop_basis="premium"))
    doc.exit.stop_loss = StopSpec(type="atr_multiple", value=2.0, atr_indicator="a")
    assert any("atr_multiple" in p and "premium" in p for p in lint(doc))


def test_low_vrp_warns_loudly():
    """The single most dangerous knob in the whole feature."""
    doc = opts_doc(pricing=PricingCfg(vrp_mult=0.8))
    warn = [p for p in lint(doc) if "vrp_mult" in p]
    assert warn and "manufacture profit that does not exist" in warn[0]


def test_low_fill_aggression_warns():
    doc = opts_doc(pricing=PricingCfg(fill_aggression=0.2))
    assert any("fill_aggression" in p for p in lint(doc))


def test_zero_open_interest_warns():
    doc = opts_doc(liquidity=Liquidity(min_open_interest=0))
    assert any("min_open_interest" in p for p in lint(doc))


def test_no_broker_side_protection_warns():
    doc = opts_doc(exit=OptionExit(stop_basis="underlying",
                                   max_loss_pct_of_premium=None))
    assert any("rest at the broker" in p for p in lint(doc))


def test_short_selling_flag_on_an_options_doc_warns():
    """A 'short' options signal BUYS PUTS — it must never be routed through the
    equity short path, which would demand a margin account and a borrow."""
    doc = opts_doc(short=side("put"))
    doc.risk.short.enabled = True
    assert any("BUYS PUTS" in p for p in lint(doc))


def test_right_for_side_helper():
    expr = Expression(instrument="options", long=side("call"), short=side("put"))
    assert right_for_side(expr, "long").right == "call"
    assert right_for_side(expr, "short").right == "put"
    assert right_for_side(None, "long") is None
    assert right_for_side(Expression(instrument="shares"), "long") is None


# ---- live-support gaps ----------------------------------------------------

def test_options_document_registers_the_pricing_gap():
    from app.strategy.live_support import live_support_gaps
    fields = {g.field for g in live_support_gaps(opts_doc())}
    assert "expression.pricing" in fields


def test_roll_is_a_blocking_gap():
    from app.strategy.live_support import blocking_gaps
    doc = opts_doc(exit=OptionExit(roll=True))
    assert any(g.field == "expression.exit.roll" for g in blocking_gaps(doc))


def test_underlying_stop_without_a_backstop_blocks_promotion():
    from app.strategy.live_support import blocking_gaps
    doc = opts_doc(exit=OptionExit(stop_basis="underlying",
                                   max_loss_pct_of_premium=None))
    fields = {g.field for g in blocking_gaps(doc)}
    assert "expression.exit.stop_basis" in fields


def test_underlying_stop_with_a_backstop_is_only_advisory():
    from app.strategy.live_support import blocking_gaps, live_support_gaps
    doc = opts_doc(exit=OptionExit(stop_basis="underlying",
                                   max_loss_pct_of_premium=60.0))
    assert "expression.exit.stop_basis" in {g.field for g in live_support_gaps(doc)}
    assert "expression.exit.stop_basis" not in {g.field for g in blocking_gaps(doc)}
