"""SchwabBroker option orders, checked against Schwab's OWN documented samples.

The golden payloads below are copied verbatim from
`schwab/trader-api--individual/README.md` — the single-leg option limit at line
813 and the NET_DEBIT vertical at line 840. Asserting equality against them
means a future edit that drifts from the documented request body fails here
rather than at 09:30 against a real account.

Also covers the instruction/asset-type matrix (README lines 373-382), which is
strict in both directions and where a mistake produces a position nobody
intended.
"""

from __future__ import annotations

import pytest

from app.broker.base import OrderLeg, OrderRequest
from app.broker.schwab import SchwabBroker


@pytest.fixture
def broker():
    return SchwabBroker(schwab=object(), account_hash="HASH")


def opt_req(**kw) -> OrderRequest:
    base = dict(intent_id="i1", symbol="XYZ   240315C00500000", side="buy_to_open",
                qty=10, order_type="limit", limit_px=6.45,
                asset_type="OPTION", multiplier=100.0, underlying="XYZ")
    base.update(kw)
    return OrderRequest(**base)


# ---- golden payloads ------------------------------------------------------

def test_single_leg_option_matches_the_documented_sample(broker):
    """schwab/trader-api--individual/README.md line 813:
    'Buy to open 10 contracts of XYZ March 15 2024 $500 Call at a limit of
    $6.45, good for the day.'"""
    expected = {
        "complexOrderStrategyType": "NONE",
        "orderType": "LIMIT",
        "session": "NORMAL",
        "price": 6.45,
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": "BUY_TO_OPEN",
                "quantity": 10,
                "instrument": {
                    "symbol": "XYZ   240315C00500000",
                    "assetType": "OPTION",
                },
            }
        ],
    }
    assert broker._build_order(opt_req()) == expected


def test_vertical_debit_spread_matches_the_documented_sample(broker):
    """README line 840: 'Buy to open 2 contracts of the XYZ March 15 2024 $45
    Put and sell to open 2 contracts of the $43 Put at a NET_DEBIT of $0.10.'"""
    req = OrderRequest(
        intent_id="i2", symbol="XYZ   240315P00045000", side="buy_to_open",
        qty=2, order_type="limit", limit_px=0.10, asset_type="OPTION",
        multiplier=100.0, underlying="XYZ", net_price_type="NET_DEBIT",
        complex_strategy="VERTICAL",
        legs=[
            OrderLeg("XYZ   240315P00045000", "buy_to_open", 2, "OPTION"),
            OrderLeg("XYZ   240315P00043000", "sell_to_open", 2, "OPTION"),
        ])
    order = broker._build_order(req)
    assert order["orderType"] == "NET_DEBIT"
    assert order["price"] == 0.10
    assert order["session"] == "NORMAL"
    assert order["duration"] == "DAY"
    assert order["orderStrategyType"] == "SINGLE"
    assert order["complexOrderStrategyType"] == "VERTICAL"
    assert order["orderLegCollection"] == [
        {"instruction": "BUY_TO_OPEN", "quantity": 2,
         "instrument": {"symbol": "XYZ   240315P00045000", "assetType": "OPTION"}},
        {"instruction": "SELL_TO_OPEN", "quantity": 2,
         "instrument": {"symbol": "XYZ   240315P00043000", "assetType": "OPTION"}},
    ]


def test_equity_orders_are_completely_unchanged(broker):
    """The other half of the guarantee: the equity payload is byte-identical to
    what the running strategies have always sent."""
    req = OrderRequest(intent_id="i3", symbol="SPY", side="buy", qty=15,
                       order_type="market")
    assert broker._build_order(req) == {
        "orderType": "MARKET",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {"instruction": "BUY", "quantity": 15,
             "instrument": {"symbol": "SPY", "assetType": "EQUITY"}},
        ],
    }


def test_no_tag_key_is_ever_sent(broker):
    """Schwab 400s every request from this app registration when `tag` is
    present at all — confirmed by testing, and documented in _build_order. The
    options path must not reintroduce it."""
    for req in (opt_req(),
                OrderRequest(intent_id="i", symbol="SPY", side="buy", qty=1,
                             order_type="market")):
        order = broker._build_order(req)
        assert "tag" not in order
        for leg in order["orderLegCollection"]:
            assert "tag" not in leg


# ---- instruction / asset-type matrix --------------------------------------

def test_sell_to_close_maps_correctly(broker):
    order = broker._build_order(opt_req(side="sell_to_close"))
    assert order["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE"


@pytest.mark.parametrize("side", ["buy", "sell", "sell_short", "buy_to_cover"])
def test_equity_instructions_are_refused_on_an_option_leg(broker, side):
    with pytest.raises(ValueError, match="not valid for assetType"):
        broker._build_order(opt_req(side=side))


@pytest.mark.parametrize("side", ["buy_to_open", "sell_to_close",
                                  "sell_to_open", "buy_to_close"])
def test_option_instructions_are_refused_on_an_equity_leg(broker, side):
    req = OrderRequest(intent_id="i", symbol="SPY", side=side, qty=1,
                       order_type="market", asset_type="EQUITY")
    with pytest.raises(ValueError, match="not valid for assetType"):
        broker._build_order(req)


def test_multileg_validates_each_leg(broker):
    req = OrderRequest(
        intent_id="i", symbol="X", side="buy_to_open", qty=1, order_type="limit",
        limit_px=1.0, asset_type="OPTION", net_price_type="NET_DEBIT",
        legs=[OrderLeg("XYZ   240315P00045000", "buy", 1, "OPTION")])
    with pytest.raises(ValueError, match="not valid for assetType"):
        broker._build_order(req)


def test_unsupported_net_price_type_is_refused(broker):
    req = OrderRequest(
        intent_id="i", symbol="X", side="buy_to_open", qty=1, order_type="limit",
        limit_px=1.0, asset_type="OPTION", net_price_type="NET_WHATEVER",
        legs=[OrderLeg("XYZ   240315P00045000", "buy_to_open", 1, "OPTION")])
    with pytest.raises(ValueError, match="net price type"):
        broker._build_order(req)


# ---- OrderRequest defaults ------------------------------------------------

def test_order_request_defaults_keep_equities_identical():
    req = OrderRequest(intent_id="i", symbol="SPY", side="buy", qty=1,
                       order_type="market")
    assert req.asset_type == "EQUITY"
    assert req.multiplier == 1.0
    assert req.underlying == "SPY"          # an equity is its own underlying
    assert req.legs is None
    assert not req.is_option
    assert req.notional_multiplier == 1.0


def test_option_request_carries_its_multiplier():
    req = opt_req()
    assert req.is_option
    assert req.notional_multiplier == 100.0
    assert req.underlying == "XYZ"


# ---- positions ------------------------------------------------------------

class FakeSchwabAccount:
    def __init__(self, positions):
        self._positions = positions

    async def get_account(self, account_hash, positions=False):
        return {"securitiesAccount": {"positions": self._positions}}


async def test_get_positions_detail_reads_option_identity():
    """Recovery needs the multiplier from the BROKER: adopting an option
    position at multiplier 1 would understate it by 100x, and mini/adjusted
    contracts genuinely differ (plan/16 D10)."""
    schwab = FakeSchwabAccount([
        {"instrument": {"symbol": "SPY", "assetType": "EQUITY"},
         "longQuantity": 100, "averagePrice": 500.0},
        {"instrument": {"symbol": "SPY   260918C00680000", "assetType": "OPTION",
                        "putCall": "CALL", "optionMultiplier": 100,
                        "underlyingSymbol": "SPY"},
         "longQuantity": 3, "averagePrice": 6.45, "marketValue": 1935.0},
    ])
    broker = SchwabBroker(schwab=schwab, account_hash="H")
    detail = await broker.get_positions_detail()

    assert detail["SPY"]["asset_type"] == "EQUITY"
    assert detail["SPY"]["multiplier"] == 1.0
    assert detail["SPY"]["underlying"] == "SPY"

    opt = detail["SPY   260918C00680000"]
    assert opt["asset_type"] == "OPTION"
    assert opt["multiplier"] == 100.0
    assert opt["put_call"] == "CALL"
    assert opt["underlying"] == "SPY"
    assert opt["qty"] == 3
    assert opt["avg_price"] == 6.45


async def test_option_underlying_falls_back_to_the_symbol_when_absent():
    schwab = FakeSchwabAccount([
        {"instrument": {"symbol": "SPY   260918C00680000", "assetType": "OPTION"},
         "longQuantity": 1, "averagePrice": 6.45},
    ])
    detail = await SchwabBroker(schwab=schwab, account_hash="H").get_positions_detail()
    assert detail["SPY   260918C00680000"]["underlying"] == "SPY"
    assert detail["SPY   260918C00680000"]["multiplier"] == 100.0


# ---- SimBroker (paper) ----------------------------------------------------

import asyncio  # noqa: E402

from app.broker.base import BrokerRejection  # noqa: E402
from app.broker.sim import SimBroker  # noqa: E402

OPT = "SPY   260918C00680000"


def sim(quotes=None, cash=100_000):
    q = quotes or {OPT: {"bid": 6.40, "ask": 6.50, "last": 6.45}}
    return SimBroker(lambda s: q.get(s), starting_cash=cash, sim_latency_ms=0)


def opt_order(side="buy_to_open", qty=3, intent="i"):
    return OrderRequest(intent_id=intent, symbol=OPT, side=side, qty=qty,
                        order_type="market", asset_type="OPTION",
                        multiplier=100.0, underlying="SPY")


async def _fill(broker, req):
    handle = await broker.place(req)
    await asyncio.sleep(0.03)
    return handle


async def test_sim_option_buy_debits_premium_times_multiplier():
    """The x100 that must never be missed: 3 contracts at ~$6.50 costs ~$1,950
    of cash, not $19.50."""
    b = sim()
    await _fill(b, opt_order(qty=3))
    assert b._positions[OPT] == 3
    spent = 100_000 - b.cash
    assert 1_940 < spent < 2_000, spent


async def test_sim_option_sell_credits_premium_times_multiplier():
    b = sim()
    await _fill(b, opt_order(qty=2))
    after_buy = b.cash
    await _fill(b, opt_order(side="sell_to_close", qty=2, intent="i2"))
    assert OPT not in b._positions
    assert b.cash > after_buy + 1_200


async def test_sim_refuses_to_sell_more_contracts_than_held():
    """Selling beyond the position would open a NAKED SHORT option, which
    plan/16 D3 forbids outright — the gateway checks this too, because the
    consequence is unbounded risk rather than a bad fill."""
    b = sim()
    await _fill(b, opt_order(qty=1))
    with pytest.raises(BrokerRejection, match="naked short"):
        await b.place(opt_order(side="sell_to_close", qty=5, intent="i2"))


async def test_sim_refuses_to_open_a_naked_short():
    b = sim()
    with pytest.raises(BrokerRejection, match="long premium only"):
        await b.place(opt_order(side="sell_to_open", qty=1))


async def test_sim_rejects_when_cash_is_short():
    b = sim(cash=500)
    with pytest.raises(BrokerRejection, match="insufficient cash"):
        await b.place(opt_order(qty=3))


async def test_sim_option_equity_applies_the_multiplier():
    """Equity feeds position sizing, so a missing multiplier here compounds
    into every subsequent order rather than staying a display bug."""
    b = sim()
    await _fill(b, opt_order(qty=3))
    acct = await b.get_account()
    assert acct["equity"] == pytest.approx(100_000, abs=120)


async def test_sim_equity_path_is_unchanged():
    b = sim(quotes={"SPY": {"bid": 99.0, "ask": 101.0, "last": 100.0}})
    await _fill(b, OrderRequest(intent_id="i", symbol="SPY", side="buy", qty=10,
                                order_type="market"))
    acct = await b.get_account()
    assert acct["equity"] == pytest.approx(100_000, abs=40)
    assert b._positions["SPY"] == 10
