"""Plan 15 §B4 — the short-selling EXECUTION path.

Safety-critical. The properties pinned here are the ones whose failure costs
real money: a cash account must never short, a cover must never reverse into a
long, and the broker instruction must be exactly right (SELL_SHORT is a
different order from SELL).
"""

from __future__ import annotations

import pytest

from app.broker.base import (CLOSING_SIDES, OPENING_SIDES, BrokerRejection, OrderRequest,
                             side_direction)
from app.broker.schwab import _INSTRUCTION_MAP
from app.broker.sim import SimBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.execution.types import OrderIntentRequest

from test_gateway import FakeBroker, FakeRisk


class ShortRisk(FakeRisk):
    """RiskContext with the short-selling attributes populated."""

    def __init__(self, *, short_enabled=True, account_type="MARGIN", hard_to_borrow=frozenset(),
                 max_short_notional=100_000.0, short_positions=None, **kw):
        super().__init__(**kw)
        self.short_enabled = short_enabled
        self.account_type = account_type
        self.hard_to_borrow = hard_to_borrow
        self.max_short_notional = max_short_notional
        self._short_positions = short_positions or {}

    def short_qty(self, symbol):
        return self._short_positions.get(symbol, 0.0)


def short_req(**kw) -> OrderIntentRequest:
    d = dict(run_id="run-1", symbol="SPY", side="sell_short", qty=10, order_type="market",
             purpose="entry", reference_price=500.0, reference_price_age_s=1.0)
    d.update(kw)
    return OrderIntentRequest(**d)


def cover_req(**kw) -> OrderIntentRequest:
    d = dict(run_id="run-1", symbol="SPY", side="buy_to_cover", qty=10, order_type="market",
             purpose="exit", reference_price=500.0, reference_price_age_s=1.0)
    d.update(kw)
    return OrderIntentRequest(**d)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def gw(db):
    return OrderGateway(db, orders_per_minute=600)


# ------------------------------------------------------------- side semantics

def test_order_side_direction_and_classification():
    """The EQUITY sides classify exactly as they always have.

    These sets were widened when options landed (plan/16 §10.1) — they answer
    "does this side open or close exposure", which is a question option sides
    have an answer to as well. The assertion is therefore on membership rather
    than on exact set contents: what must never change is how the four equity
    sides behave, and adding an option side must not perturb them."""
    assert side_direction("buy") == 1 and side_direction("buy_to_cover") == 1
    assert side_direction("sell") == -1 and side_direction("sell_short") == -1
    assert {"buy", "sell_short"} <= OPENING_SIDES
    assert {"sell", "buy_to_cover"} <= CLOSING_SIDES
    assert not (OPENING_SIDES & CLOSING_SIDES), "a side cannot both open and close"


def test_option_sides_classify_and_never_mix_with_equity_ones():
    """Schwab's instruction/asset-type matrix is strict: an equity instruction
    on an option leg is rejected, and vice versa."""
    from app.broker.base import (EQUITY_SIDES, OPTION_SIDES, is_option_side,
                                 valid_side_for)
    assert side_direction("buy_to_open") == 1
    assert side_direction("sell_to_close") == -1
    assert "buy_to_open" in OPENING_SIDES
    assert "sell_to_close" in CLOSING_SIDES
    assert not (OPTION_SIDES & EQUITY_SIDES)
    assert is_option_side("buy_to_open") and not is_option_side("buy")

    assert valid_side_for("buy", "EQUITY")
    assert not valid_side_for("buy", "OPTION")
    assert valid_side_for("buy_to_open", "OPTION")
    assert not valid_side_for("buy_to_open", "EQUITY")


def test_schwab_instruction_mapping_is_exact():
    """Sending SELL where SELL_SHORT is meant produces the wrong position."""
    assert _INSTRUCTION_MAP["buy"] == "BUY"
    assert _INSTRUCTION_MAP["sell"] == "SELL"
    assert _INSTRUCTION_MAP["sell_short"] == "SELL_SHORT"
    assert _INSTRUCTION_MAP["buy_to_cover"] == "BUY_TO_COVER"


# ------------------------------------------------------------- gateway gates

async def test_cash_account_can_never_short(gw):
    """THE critical check: a cash account cannot short, and we reject locally
    rather than letting the intent reach the broker."""
    res = await gw.submit(short_req(), FakeBroker(), ShortRisk(account_type="CASH"))
    assert not res.accepted
    assert "cash_account_cannot_short" in res.reason


async def test_unknown_account_type_denies_by_default(gw):
    """A RiskContext that predates shorting must not be able to short."""
    res = await gw.submit(short_req(), FakeBroker(), FakeRisk())
    assert not res.accepted
    assert "short_not_enabled" in res.reason


async def test_short_requires_the_strategy_opt_in(gw):
    res = await gw.submit(short_req(), FakeBroker(), ShortRisk(short_enabled=False))
    assert not res.accepted
    assert "short_not_enabled" in res.reason


async def test_hard_to_borrow_symbol_is_rejected(gw):
    res = await gw.submit(short_req(),
                          FakeBroker(), ShortRisk(hard_to_borrow=frozenset({"SPY"})))
    assert not res.accepted
    assert "hard_to_borrow" in res.reason


async def test_max_short_notional_is_enforced(gw):
    res = await gw.submit(short_req(qty=100),      # 100 * 500 = 50,000
                          FakeBroker(), ShortRisk(max_short_notional=1_000.0))
    assert not res.accepted
    assert "max_short_notional" in res.reason


async def test_a_valid_short_passes_the_stack(gw):
    broker = FakeBroker()
    res = await gw.submit(short_req(), broker, ShortRisk())
    assert res.accepted and res.status == "SUBMITTED"
    assert broker.placed[0].side == "sell_short"


async def test_cover_cannot_exceed_the_open_short(gw):
    """Covering more than is short would buy through zero into a LONG."""
    risk = ShortRisk(short_positions={"SPY": 5.0})
    res = await gw.submit(cover_req(qty=10), FakeBroker(), risk)
    assert not res.accepted
    assert "cover_exceeds_short" in res.reason


async def test_cover_within_the_open_short_is_allowed(gw):
    risk = ShortRisk(short_positions={"SPY": 10.0})
    res = await gw.submit(cover_req(qty=10), FakeBroker(), risk)
    assert res.accepted


async def test_cover_is_protective_and_survives_a_daily_loss_breach(gw):
    """An exit must never be blocked by the very condition it fixes."""
    risk = ShortRisk(pnl=-10_000, max_daily_loss=500, short_positions={"SPY": 10.0})
    assert (await gw.submit(cover_req(), FakeBroker(), risk)).accepted


# ---------------------------------------------------------------- SimBroker

def quotes(px):
    return lambda sym: {"bid": px, "ask": px, "last": px}


async def test_sim_short_round_trip_moves_cash_and_position_correctly():
    b = SimBroker(quotes(100.0), starting_cash=100_000, sim_latency_ms=0)
    await b.place(OrderRequest("i1", "X", "sell_short", 10, "market"))
    await _settle()
    assert (await b.get_positions())["X"] == pytest.approx(-10)   # signed short
    assert b.cash == pytest.approx(100_000 + 10 * 100, rel=1e-3)

    b._quotes = quotes(90.0)
    await b.place(OrderRequest("i2", "X", "buy_to_cover", 10, "market"))
    await _settle()
    assert (await b.get_positions()).get("X") is None
    # sold 10 @100, covered 10 @90 -> +100 profit
    assert b.cash == pytest.approx(100_100, rel=1e-3)


async def test_sim_rejects_covering_more_than_the_open_short():
    b = SimBroker(quotes(100.0), starting_cash=100_000, sim_latency_ms=0)
    await b.place(OrderRequest("i1", "X", "sell_short", 5, "market"))
    await _settle()
    with pytest.raises(BrokerRejection):
        await b.place(OrderRequest("i2", "X", "buy_to_cover", 10, "market"))


async def test_sim_rejects_a_short_without_enough_margin():
    b = SimBroker(quotes(100.0), starting_cash=100.0, sim_latency_ms=0,
                  short_margin_pct=50.0)
    with pytest.raises(BrokerRejection):
        await b.place(OrderRequest("i1", "X", "sell_short", 100, "market"))  # needs 5,000


async def test_sim_equity_subtracts_the_short_liability():
    b = SimBroker(quotes(100.0), starting_cash=100_000, sim_latency_ms=0)
    await b.place(OrderRequest("i1", "X", "sell_short", 10, "market"))
    await _settle()
    acct = await b.get_account()
    # cash 101,000 minus the 1,000 liability == unchanged equity
    assert acct["equity"] == pytest.approx(100_000, rel=1e-4)


async def test_sim_short_stop_triggers_on_strength():
    """A short's protective stop is a buy_to_cover firing when price RISES."""
    b = SimBroker(quotes(100.0), starting_cash=100_000, sim_latency_ms=0)
    await b.place(OrderRequest("i1", "X", "sell_short", 10, "market"))
    await _settle()
    await b.place(OrderRequest("i2", "X", "buy_to_cover", 10, "stop", stop_px=105.0))
    await b.process_tick("X", bid=101, ask=101, last=101)     # below stop: no fill
    assert (await b.get_positions())["X"] == pytest.approx(-10)
    await b.process_tick("X", bid=106, ask=106, last=106)     # through the stop
    assert (await b.get_positions()).get("X") is None


async def _settle():
    import asyncio
    for _ in range(5):
        await asyncio.sleep(0)


# ------------------------------------------- account type + borrow failures

def test_borrow_failure_detection_matches_real_wordings():
    from app.broker.schwab import is_borrow_failure
    for msg in ("Symbol is not shortable", "No shares available to borrow",
                "HARD TO BORROW", "locate required for short sale",
                "Unable to short this security"):
        assert is_borrow_failure(msg), msg
    for msg in ("Internal server error", "Insufficient buying power", "rate limited"):
        assert not is_borrow_failure(msg), msg


def test_schwab_account_type_is_surfaced_for_the_gateway():
    """The gateway needs CASH/MARGIN; the runtime must not have to dig into
    Schwab's raw payload shape to find it."""
    import inspect

    from app.broker import schwab
    src = inspect.getsource(schwab.SchwabBroker.get_account)
    assert '"account_type"' in src


async def test_paper_run_reports_margin_and_live_denies_without_an_account_type():
    """Safe default: an unknown account type cannot short."""
    from app.runtime.risk import RunRiskTracker

    class FakeRunner:
        run_id = "r"
        _positions: dict = {}
        _pending_entries: dict = {}
        _last_price: dict = {}
        _orders_today = 0
        _realized_pnl_today = 0.0

        def __init__(self, mode, account_type=""):
            self.mode = mode
            self._account_type = account_type

    paper = RunRiskTracker(FakeRunner("paper"))
    assert paper.account_type == "MARGIN"          # simulated margin

    live_unknown = RunRiskTracker(FakeRunner("live", ""))
    assert live_unknown.account_type != "MARGIN"   # denies

    live_cash = RunRiskTracker(FakeRunner("live", "CASH"))
    assert live_cash.account_type == "CASH"

    live_margin = RunRiskTracker(FakeRunner("live", "margin"))
    assert live_margin.account_type == "MARGIN"    # normalized
