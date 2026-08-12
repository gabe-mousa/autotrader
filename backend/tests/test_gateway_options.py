"""Gateway option gates (docs/plan/16 §11.2).

The discipline being tested is the one that made the shorting gates safe: EVERY
option value is read with a DENYING default, so a RiskContext written before
options existed cannot accidentally permit an option order. `test_pre_options_
risk_context_denies_everything` is the load-bearing test in this file.

The other one that matters most is `close_exceeds_position`: selling more
contracts than are held does not merely oversell, it opens a NAKED SHORT
OPTION — the one position whose loss is not bounded by the premium and cannot
be capped by a resting order, and the thing plan/16 D3 forbids outright.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.broker.base import OrderLeg
from app.db import Database
from app.execution.gateway import OrderGateway
from app.execution.types import OrderIntentRequest
from app.options.symbology import format_occ

CALL = format_occ("SPY", dt.date.today() + dt.timedelta(days=45), "C", 680)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


class FakeBroker:
    mode = "paper"

    def __init__(self):
        self.placed = []

    async def place(self, req):
        self.placed.append(req)
        from app.broker.base import BrokerOrderHandle
        return BrokerOrderHandle("bid-1", "WORKING")

    async def cancel(self, oid): ...
    async def replace(self, oid, req): ...
    async def get_positions(self): return {}
    async def get_account(self): return {}
    def subscribe_events(self, h): ...


class OptionRisk:
    """A fully options-aware RiskContext."""
    run_id = "run-1"
    mode = "paper"
    max_open_positions = 5
    max_daily_orders = 100
    max_daily_loss = 10_000

    options_enabled = True
    max_premium_at_risk_remaining = 10_000.0
    max_contracts_per_position = 50

    def __init__(self, held=0.0):
        self._held = held

    def open_position_count(self): return 0
    def orders_today_count(self): return 0
    def daily_pnl(self): return 0.0
    def has_open_entry_order(self, symbol): return False
    def contract_qty(self, symbol): return self._held


class PreOptionsRisk:
    """A RiskContext exactly as it was written BEFORE options existed."""
    run_id = "run-1"
    mode = "paper"
    max_open_positions = 5
    max_daily_orders = 100
    max_daily_loss = 10_000

    def open_position_count(self): return 0
    def orders_today_count(self): return 0
    def daily_pnl(self): return 0.0
    def has_open_entry_order(self, symbol): return False


def opt_intent(**kw) -> OrderIntentRequest:
    base = dict(run_id="run-1", symbol=CALL, side="buy_to_open", qty=3,
                order_type="limit", purpose="entry", limit_px=6.50,
                reference_price=6.45, reference_price_age_s=1.0,
                asset_type="OPTION", multiplier=100.0, underlying="SPY",
                quote_bid=6.40, quote_ask=6.50, delta=0.35,
                underlying_price=672.0)
    base.update(kw)
    return OrderIntentRequest(**base)


async def submit(db, req, risk=None, **kw):
    gw = OrderGateway(db, max_single_order_notional=1_000_000)
    return await gw.submit(req, FakeBroker(), risk or OptionRisk(), **kw)


# ---- the load-bearing guarantee -------------------------------------------

async def test_pre_options_risk_context_denies_everything(db):
    """A RiskContext that predates options must be unable to trade them —
    the same denying-default discipline that made the shorting gates safe."""
    res = await submit(db, opt_intent(), risk=PreOptionsRisk())
    assert not res.accepted
    assert "options_not_enabled" in res.reason


async def test_options_enabled_false_is_refused(db):
    risk = OptionRisk()
    risk.options_enabled = False
    res = await submit(db, opt_intent(), risk=risk)
    assert not res.accepted and "options_not_enabled" in res.reason


# ---- naked shorts: the thing v1 must never do -----------------------------

async def test_closing_more_than_held_is_refused(db):
    """Not merely an oversell — it opens a NAKED SHORT OPTION."""
    res = await submit(db, opt_intent(side="sell_to_close", qty=5, purpose="exit"),
                       risk=OptionRisk(held=2))
    assert not res.accepted
    assert "close_exceeds_position" in res.reason
    assert "naked short" in res.reason


async def test_closing_exactly_what_is_held_is_allowed(db):
    res = await submit(db, opt_intent(side="sell_to_close", qty=2, purpose="exit"),
                       risk=OptionRisk(held=2))
    assert res.accepted, res.reason


async def test_bare_sell_to_open_is_refused(db):
    res = await submit(db, opt_intent(side="sell_to_open"))
    assert not res.accepted
    assert "naked_short_option" in res.reason


async def test_sell_to_open_covered_by_a_long_leg_is_permitted(db):
    """A debit spread's short leg is covered by its long leg, so max loss is
    still the debit paid. This is the v1.1 path; the gate is written against
    the real condition so enabling spreads later cannot open it wider."""
    put_long = format_occ("SPY", dt.date.today() + dt.timedelta(days=45), "P", 660)
    put_short = format_occ("SPY", dt.date.today() + dt.timedelta(days=45), "P", 650)
    res = await submit(db, opt_intent(
        symbol=put_long, side="sell_to_open", qty=2,
        legs=[OrderLeg(put_long, "buy_to_open", 2, "OPTION"),
              OrderLeg(put_short, "sell_to_open", 2, "OPTION")]))
    assert res.accepted, res.reason


async def test_short_leg_larger_than_the_long_leg_is_still_naked(db):
    put_long = format_occ("SPY", dt.date.today() + dt.timedelta(days=45), "P", 660)
    put_short = format_occ("SPY", dt.date.today() + dt.timedelta(days=45), "P", 650)
    res = await submit(db, opt_intent(
        symbol=put_long, side="sell_to_open", qty=5,
        legs=[OrderLeg(put_long, "buy_to_open", 1, "OPTION"),
              OrderLeg(put_short, "sell_to_open", 5, "OPTION")]))
    assert not res.accepted and "naked_short_option" in res.reason


# ---- market orders --------------------------------------------------------

async def test_option_market_orders_are_refused(db):
    """An option market order on a wide book can fill catastrophically far
    from mid."""
    res = await submit(db, opt_intent(order_type="market", limit_px=None))
    assert not res.accepted
    assert "option_market_order" in res.reason


async def test_equity_market_orders_still_work(db):
    req = OrderIntentRequest(run_id="run-1", symbol="SPY", side="buy", qty=10,
                             order_type="market", purpose="entry",
                             reference_price=670.0, reference_price_age_s=1.0)
    res = await submit(db, req)
    assert res.accepted, res.reason


# ---- symbol / expiry ------------------------------------------------------

async def test_malformed_occ_symbol_is_refused(db):
    res = await submit(db, opt_intent(symbol="NOT-AN-OPTION"))
    assert not res.accepted and "bad_occ_symbol" in res.reason


async def test_expired_contract_is_refused(db):
    expired = format_occ("SPY", dt.date.today() - dt.timedelta(days=1), "C", 680)
    res = await submit(db, opt_intent(symbol=expired))
    assert not res.accepted and "expired_contract" in res.reason


async def test_a_contract_expiring_today_is_still_tradeable(db):
    """It trades all day on its expiration date; rejecting it as expired would
    strand a position that needs closing."""
    today = format_occ("SPY", dt.date.today(), "C", 680)
    res = await submit(db, opt_intent(symbol=today, side="sell_to_close",
                                      purpose="exit", qty=1),
                       risk=OptionRisk(held=1))
    assert res.accepted, res.reason


# ---- premium / contract caps ----------------------------------------------

async def test_premium_at_risk_headroom_binds(db):
    risk = OptionRisk()
    risk.max_premium_at_risk_remaining = 500.0
    # 3 x 6.45 x 100 = 1935 premium
    res = await submit(db, opt_intent(), risk=risk)
    assert not res.accepted and "max_premium_at_risk" in res.reason


async def test_max_contracts_per_position_binds(db):
    risk = OptionRisk()
    risk.max_contracts_per_position = 2
    res = await submit(db, opt_intent(qty=3), risk=risk)
    assert not res.accepted and "max_contracts_per_position" in res.reason


async def test_premium_notional_uses_the_multiplier(db):
    """The single-order cap must see $1,935, not $19.35."""
    gw = OrderGateway(db, max_single_order_notional=1_000)
    res = await gw.submit(opt_intent(), FakeBroker(), OptionRisk())
    assert not res.accepted
    assert "max_single_order_premium" in res.reason


async def test_delta_adjusted_exposure_is_capped_separately(db):
    """Premium and exposure differ by ~100x: 100 contracts at $0.50 is $5,000
    of premium but ~$1.7M of underlying exposure. Capping only premium would
    be blind to that."""
    gw = OrderGateway(db, max_single_order_notional=50_000)
    req = opt_intent(qty=100, limit_px=0.50, reference_price=0.50,
                     quote_bid=0.48, quote_ask=0.52, delta=0.25,
                     underlying_price=672.0)
    risk = OptionRisk()
    risk.max_premium_at_risk_remaining = 1e9
    risk.max_contracts_per_position = 1000
    res = await gw.submit(req, FakeBroker(), risk)
    assert not res.accepted
    assert "max_single_order_exposure" in res.reason


# ---- price sanity ---------------------------------------------------------

async def test_option_price_sanity_is_spread_relative(db):
    """A marketable limit on a $1.20 option is routinely 5-15% from mid, which
    the 5% equity band would reject. The band is measured against the SPREAD
    instead — the actual measure of 'far from market' for this instrument."""
    # 12% above mid, but well inside 2x the spread -> allowed
    res = await submit(db, opt_intent(limit_px=6.60, reference_price=6.45,
                                      quote_bid=6.40, quote_ask=6.50))
    assert res.accepted, res.reason


async def test_wildly_off_option_price_is_still_rejected(db):
    res = await submit(db, opt_intent(limit_px=60.0, reference_price=6.45,
                                      quote_bid=6.40, quote_ask=6.50))
    assert not res.accepted and "price_sanity" in res.reason


async def test_without_a_quote_a_wider_percentage_band_applies(db):
    ok = await submit(db, opt_intent(limit_px=7.40, reference_price=6.45,
                                     quote_bid=None, quote_ask=None))
    assert ok.accepted, ok.reason
    bad = await submit(db, opt_intent(limit_px=20.0, reference_price=6.45,
                                      quote_bid=None, quote_ask=None))
    assert not bad.accepted and "price_sanity" in bad.reason


async def test_equity_band_is_unchanged(db):
    """The equity path must keep its tight 5% band."""
    req = OrderIntentRequest(run_id="run-1", symbol="SPY", side="buy", qty=10,
                             order_type="limit", purpose="entry", limit_px=740.0,
                             reference_price=670.0, reference_price_age_s=1.0)
    res = await submit(db, req)
    assert not res.accepted and "price_sanity" in res.reason


# ---- journaling -----------------------------------------------------------

async def test_option_intents_journal_their_asset_type(db):
    res = await submit(db, opt_intent())
    assert res.accepted, res.reason
    row = await db.fetch_one(
        "SELECT asset_type, underlying, multiplier, symbol FROM order_intents "
        "WHERE id=?", (res.intent_id,))
    assert row["asset_type"] == "OPTION"
    assert row["underlying"] == "SPY"        # what a user groups by
    assert row["multiplier"] == 100.0
    assert row["symbol"] == CALL             # the exact contract


async def test_equity_intents_journal_as_equity(db):
    req = OrderIntentRequest(run_id="run-1", symbol="SPY", side="buy", qty=10,
                             order_type="market", purpose="entry",
                             reference_price=670.0, reference_price_age_s=1.0)
    res = await submit(db, req)
    row = await db.fetch_one(
        "SELECT asset_type, underlying, multiplier FROM order_intents WHERE id=?",
        (res.intent_id,))
    assert row["asset_type"] == "EQUITY"
    assert row["underlying"] == "SPY"
    assert row["multiplier"] == 1.0


async def test_broker_receives_the_option_fields(db):
    gw = OrderGateway(db, max_single_order_notional=1_000_000)
    broker = FakeBroker()
    res = await gw.submit(opt_intent(), broker, OptionRisk())
    assert res.accepted
    placed = broker.placed[0]
    assert placed.asset_type == "OPTION"
    assert placed.multiplier == 100.0
    assert placed.underlying == "SPY"
    assert placed.symbol == CALL


# ---- the kill switch still comes first ------------------------------------

async def test_kill_switch_blocks_options_too(db):
    gw = OrderGateway(db)
    await gw.activate_kill_switch(reason="test")
    res = await gw.submit(opt_intent(), FakeBroker(), OptionRisk())
    assert not res.accepted and "kill_switch" in res.reason
