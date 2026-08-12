"""SimBroker tests: market/limit/stop fill semantics against controllable
quotes, cash/position bookkeeping, cancellation, and rejection paths."""

from __future__ import annotations

import asyncio

import pytest

from app.broker.base import BrokerRejection, OrderRequest
from app.broker.sim import SimBroker


class QuoteBook:
    """Mutable in-memory quote store standing in for MarketDataService's
    latest_cached_quote — tests mutate .quotes directly to simulate ticks."""

    def __init__(self):
        self.quotes: dict[str, dict] = {}

    def get(self, symbol: str) -> dict | None:
        return self.quotes.get(symbol)

    def set(self, symbol: str, bid: float, ask: float, last: float | None = None) -> None:
        self.quotes[symbol] = {"bid": bid, "ask": ask, "last": last if last is not None else bid}


class EventCollector:
    def __init__(self):
        self.events: list = []

    async def __call__(self, event) -> None:
        self.events.append(event)


async def wait_settled():
    """Let background fill tasks (asyncio.create_task) run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


def buy(symbol="SPY", qty=10, order_type="market", limit_px=None, stop_px=None, intent="i-1"):
    return OrderRequest(intent_id=intent, symbol=symbol, side="buy", qty=qty,
                        order_type=order_type, limit_px=limit_px, stop_px=stop_px)


def sell(symbol="SPY", qty=10, order_type="market", limit_px=None, stop_px=None, intent="i-2"):
    return OrderRequest(intent_id=intent, symbol=symbol, side="sell", qty=qty,
                        order_type=order_type, limit_px=limit_px, stop_px=stop_px)


@pytest.fixture
def book():
    return QuoteBook()


@pytest.fixture
def broker(book):
    return SimBroker(book.get, starting_cash=100_000, sim_latency_ms=0, sim_slippage_bps=10)


# ---- market orders ----------------------------------------------------------

async def test_market_buy_fills_at_ask_plus_slippage(book, broker):
    book.set("SPY", bid=499.9, ask=500.1)
    events = EventCollector()
    broker.subscribe_events(events)
    handle = await broker.place(buy(qty=10))
    assert handle.status == "WORKING"
    await wait_settled()

    kinds = [e.kind for e in events.events]
    assert kinds == ["accepted", "fill"]
    fill = events.events[1].fill
    expected_px = 500.1 * 1.001  # 10 bps slippage
    assert fill.price == pytest.approx(expected_px)
    assert fill.qty == 10
    positions = await broker.get_positions()
    assert positions["SPY"] == 10
    assert broker.cash == pytest.approx(100_000 - 10 * expected_px)


async def test_market_buy_insufficient_cash_rejects_synchronously(book, broker):
    book.set("SPY", bid=499.9, ask=500.1)
    with pytest.raises(BrokerRejection):
        await broker.place(buy(qty=100_000))  # far beyond $100k cash


async def test_concurrent_buys_rejected_at_placement_not_at_fill(book):
    """A strategy signalling on several symbols at one bar close places them
    back-to-back before any fills. Cash is reserved at placement, so the ones
    that no longer fit are declined up front instead of surviving to be
    rejected at fill time."""
    slow = SimBroker(book.get, starting_cash=1_100, sim_latency_ms=50, sim_slippage_bps=0)
    events = EventCollector()
    slow.subscribe_events(events)
    for sym in ("AAA", "BBB", "CCC"):
        book.set(sym, bid=499.9, ask=500.0)

    await slow.place(buy(symbol="AAA", qty=1, intent="i-a"))
    await slow.place(buy(symbol="BBB", qty=1, intent="i-b"))
    assert slow.available_cash == pytest.approx(100.0)
    with pytest.raises(BrokerRejection, match="insufficient cash"):
        await slow.place(buy(symbol="CCC", qty=1, intent="i-c"))

    await asyncio.sleep(0.15)
    assert [e.kind for e in events.events].count("rejected") == 0
    assert [e.kind for e in events.events].count("fill") == 2
    assert slow.cash == pytest.approx(100.0)
    assert slow.reserved_cash == 0.0


async def test_market_buy_no_quote_rejects(broker):
    with pytest.raises(BrokerRejection):
        await broker.place(buy(symbol="ZZZZ"))


async def test_market_sell_more_than_position_rejects(book, broker):
    book.set("SPY", bid=499.9, ask=500.1)
    with pytest.raises(BrokerRejection):
        await broker.place(sell(qty=5))  # no position at all yet


async def test_market_sell_fills_at_bid_minus_slippage(book, broker):
    book.set("SPY", bid=500.0, ask=500.2)
    await broker.place(buy(qty=10, intent="i-buy"))
    await wait_settled()

    book.set("SPY", bid=510.0, ask=510.2)
    events = EventCollector()
    broker.subscribe_events(events)
    await broker.place(sell(qty=10, intent="i-sell"))
    await wait_settled()

    fill = [e for e in events.events if e.kind == "fill"][0].fill
    assert fill.price == pytest.approx(510.0 * 0.999)
    positions = await broker.get_positions()
    assert "SPY" not in positions  # fully closed


# ---- limit orders (passive, driven by process_tick) --------------------------

async def test_limit_buy_does_not_fill_until_cross(book, broker):
    book.set("SPY", bid=499.0, ask=501.0)
    events = EventCollector()
    broker.subscribe_events(events)
    await broker.place(buy(order_type="limit", limit_px=500.0, qty=10))
    await wait_settled()
    assert not any(e.kind == "fill" for e in events.events)

    # ask still above limit -> no fill
    await broker.process_tick("SPY", bid=499.5, ask=500.5, last=500.0)
    assert not any(e.kind == "fill" for e in events.events)

    # ask crosses the limit -> fills AT THE LIMIT PRICE (not the crossing ask)
    await broker.process_tick("SPY", bid=499.8, ask=499.9, last=499.9)
    fills = [e for e in events.events if e.kind == "fill"]
    assert len(fills) == 1 and fills[0].fill.price == 500.0


async def test_limit_sell_fills_on_bid_cross():
    book = QuoteBook()
    broker = SimBroker(book.get, starting_cash=100_000, sim_latency_ms=0)
    book.set("SPY", bid=500, ask=500.5)
    await broker.place(buy(qty=10, intent="i-buy"))
    await wait_settled()

    events = EventCollector()
    broker.subscribe_events(events)
    await broker.place(sell(order_type="limit", limit_px=505.0, qty=10))
    await broker.process_tick("SPY", bid=504.0, ask=504.5, last=504.0)  # not yet
    assert not any(e.kind == "fill" for e in events.events)
    await broker.process_tick("SPY", bid=505.5, ask=506.0, last=505.5)  # crosses
    fills = [e for e in events.events if e.kind == "fill"]
    assert len(fills) == 1 and fills[0].fill.price == 505.0


# ---- stop orders --------------------------------------------------------------

async def test_stop_sell_triggers_on_last_trade():
    book = QuoteBook()
    broker = SimBroker(book.get, starting_cash=100_000, sim_latency_ms=0, sim_slippage_bps=10)
    book.set("SPY", bid=500, ask=500.5)
    await broker.place(buy(qty=10, intent="i-buy"))
    await wait_settled()

    events = EventCollector()
    broker.subscribe_events(events)
    await broker.place(sell(order_type="stop", stop_px=490.0, qty=10))
    await broker.process_tick("SPY", bid=495, ask=495.5, last=495)  # above stop, no trigger
    assert not any(e.kind == "fill" for e in events.events)
    await broker.process_tick("SPY", bid=489, ask=489.5, last=489.5)  # last <= stop
    fills = [e for e in events.events if e.kind == "fill"]
    assert len(fills) == 1
    assert fills[0].fill.price == pytest.approx(489 * 0.999)  # bid - slippage, market-style


# ---- cancellation --------------------------------------------------------------

async def test_cancel_prevents_pending_market_fill(book):
    book.set("SPY", bid=499.9, ask=500.1)
    broker = SimBroker(book.get, starting_cash=100_000, sim_latency_ms=1000)  # slow on purpose
    events = EventCollector()
    broker.subscribe_events(events)
    handle = await broker.place(buy(qty=10))
    await broker.cancel(handle.broker_order_id)
    await asyncio.sleep(0.05)  # well under the 1s latency
    kinds = [e.kind for e in events.events]
    assert kinds == ["accepted", "canceled"]
    assert (await broker.get_positions()) == {}


async def test_cancel_working_limit_order_removes_it(book, broker):
    book.set("SPY", bid=499, ask=501)
    handle = await broker.place(buy(order_type="limit", limit_px=500, qty=10))
    await broker.cancel(handle.broker_order_id)
    # a subsequent crossing tick must NOT fill the canceled order
    events = EventCollector()
    broker.subscribe_events(events)
    await broker.process_tick("SPY", bid=499, ask=499.5, last=499.5)
    assert not any(e.kind == "fill" for e in events.events)


async def test_cancel_unknown_order_is_a_safe_noop(broker):
    await broker.cancel("does-not-exist")  # must not raise


# ---- accounting -----------------------------------------------------------------

async def test_average_price_across_multiple_buys(book, broker):
    book.set("SPY", bid=99.9, ask=100.0)
    await broker.place(buy(qty=10, intent="i-1"))
    await wait_settled()
    book.set("SPY", bid=119.9, ask=120.0)
    await broker.place(buy(qty=10, intent="i-2"))
    await wait_settled()
    # 10bps slippage: 10@100.1 + 10@120.12 -> avg 110.11
    assert broker._avg_price["SPY"] == pytest.approx(110.11, abs=0.01)
    assert broker._positions["SPY"] == 20


async def test_get_account_marks_to_market(book, broker):
    book.set("SPY", bid=99.9, ask=100.0)
    await broker.place(buy(qty=10, intent="i-1"))
    await wait_settled()
    book.set("SPY", bid=150.0, ask=150.2, last=150.0)
    account = await broker.get_account()
    assert account["equity"] == pytest.approx(account["cash"] + 10 * 150.0)


# ---- option quote routing ---------------------------------------------------
#
# REGRESSION (paper SPY options run, 2026-08-04): options entries were rejected
# all morning with "no quote available for SPY   2608...". The sim's single
# quote provider is the LEVELONE_EQUITIES cache, which has never heard of an
# OCC symbol and returns None for every one, so `_check_option_order` rejected
# at PLACEMENT — the option quotes were being streamed and cached the whole
# time, just never consulted. These tests pin the routing: an option symbol is
# answered by the option provider, and an equity provider that cannot answer
# for OCC symbols (which is the real one's behaviour) must not break options.

OCC = "SPY   260807C00769000"


def buy_to_open(symbol=OCC, qty=1, limit_px=None, intent="i-opt"):
    return OrderRequest(intent_id=intent, symbol=symbol, side="buy_to_open", qty=qty,
                        order_type="limit" if limit_px else "market", limit_px=limit_px,
                        asset_type="OPTION", multiplier=100.0)


async def test_option_order_is_priced_by_the_option_provider_not_the_equity_cache():
    """The equity book answers None for the OCC symbol, exactly as the real
    LEVELONE_EQUITIES cache does. The order must still price."""
    equity = QuoteBook()
    equity.set("SPY", bid=499.9, ask=500.1)
    options = {OCC: {"bid": 3.10, "ask": 3.20, "last": 3.15}}
    b = SimBroker(equity.get, starting_cash=100_000, sim_latency_ms=0,
                  option_quote_provider=options.get)
    await b.place(buy_to_open(qty=2))
    await wait_settled()
    assert b._positions[OCC] == 2
    assert b._multiplier[OCC] == 100.0


async def test_option_order_is_rejected_when_the_option_provider_has_no_market():
    """None still means 'no usable market' and must block the order — the sim
    must never invent a price for a contract that is not quoting."""
    equity = QuoteBook()
    equity.set("SPY", bid=499.9, ask=500.1)
    b = SimBroker(equity.get, starting_cash=100_000, sim_latency_ms=0,
                  option_quote_provider=lambda s: None)
    with pytest.raises(BrokerRejection, match="no quote available"):
        await b.place(buy_to_open())


async def test_resting_option_limit_order_fills_from_process_option_ticks():
    """Option orders rest on OCC symbols, which never appear on the equity
    stream, so `process_tick` is never called for them. Without an option tick
    pump they rest forever — for a protective stop that means silently
    unprotected."""
    equity = QuoteBook()
    options = {OCC: {"bid": 3.10, "ask": 3.20, "last": 3.15}}
    b = SimBroker(equity.get, starting_cash=100_000, sim_latency_ms=0,
                  option_quote_provider=options.get)
    await b.place(buy_to_open(qty=1, limit_px=3.00))
    await wait_settled()
    assert OCC not in b._positions, "should not fill: ask 3.20 is above the 3.00 limit"

    options[OCC] = {"bid": 2.90, "ask": 2.95, "last": 2.92}
    await b.process_option_ticks()
    await wait_settled()
    assert b._positions[OCC] == 1
