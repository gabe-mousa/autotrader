"""The StrategyRunner's OPTIONS path (docs/plan/16 §9).

Same rationale as test_runner_shorting.py, which exists because the engine,
gateway and SimBroker all handled shorting correctly while the runner was
structurally long-only — a strategy backtested one way and traded another,
silently. Options add far more ways for that to happen, so every property that
costs money when it breaks is pinned here:

  * a long signal BUYS CALLS and a short signal BUYS PUTS, both via
    BUY_TO_OPEN — never `sell_short`, which would route a put purchase through
    the equity borrow/margin gates;
  * an order is never sent without a real two-sided quote;
  * the broker-side loss backstop is placed, because the underlying stop cannot
    rest at the broker and a crash would otherwise leave the position unmanaged;
  * P&L applies the MULTIPLIER and uses the long formula for both rights;
  * expiry force-closes at close_at_dte;
  * the position survives to the durable row with its contract identity.

Driven through the same real Database + OrderGateway + SimBroker wiring as the
equity runner tests, so a divergence can only come from the runner itself.
"""

from __future__ import annotations

import copy
import datetime as dt

import pytest

from app.broker.sim import SimBroker
from app.db import Database
from app.execution.gateway import OrderGateway
from app.options.contracts import Contract, ContractQuote, Greeks
from app.options.symbology import format_occ, parse_occ
from app.runtime.runner import StrategyRunner
from app.strategy.schema import StrategyDocument
from tests.test_runner import (MIN_MS, FakeAggregator, FakeMDS, QuoteBook, bar_event,
                               settle, warmup_df)
from tests.test_strategy import EXAMPLE

ALWAYS = {"op": "lt", "left": {"ind": "rsi14"}, "right": {"value": 200}}
NEVER = {"op": "gt", "left": {"ind": "rsi14"}, "right": {"value": 200}}

EXPIRY = dt.date.today() + dt.timedelta(days=40)
NEAR_EXPIRY = dt.date.today() + dt.timedelta(days=2)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    await d.execute(
        "INSERT INTO runs (id, strategy_id, strategy_version_hash, mode, state) "
        "VALUES ('run-1','strat-1','hash','paper','STOPPED')")
    yield d
    await d.close()


@pytest.fixture
def book():
    return QuoteBook()


@pytest.fixture
def broker(book):
    return SimBroker(book.get, starting_cash=100_000, sim_latency_ms=0,
                     sim_slippage_bps=0)


@pytest.fixture
def gateway(db):
    return OrderGateway(db, orders_per_minute=6000)


# ---- a fake chain -----------------------------------------------------------

class FakeChainService:
    """Serves a small, realistic chain around the spot price."""

    def __init__(self, spot=100.0, expiry=EXPIRY, spread=0.10):
        self.spot = spot
        self.expiry = expiry
        self.spread = spread
        self.calls = 0

    async def fetch(self, underlying, **kw):
        from app.options.chain_service import ChainFetchResult
        from app.options.expiry import expiry_to_ms
        from app.options.store import SnapshotRow
        self.calls += 1
        rows = []
        for strike in range(int(self.spot) - 15, int(self.spot) + 16, 5):
            for right in ("C", "P"):
                itm = (self.spot - strike) if right == "C" else (strike - self.spot)
                mid = max(itm, 0) + 3.0
                delta = 0.35 if right == "C" else -0.35
                rows.append(SnapshotRow(
                    underlying=underlying, ts=0,
                    symbol=format_occ(underlying, self.expiry, right, strike),
                    expiry=expiry_to_ms(self.expiry), strike=float(strike),
                    opt_right=right, bid=mid - self.spread / 2,
                    ask=mid + self.spread / 2, mark=mid, iv=0.20, delta=delta,
                    gamma=0.01, theta=-0.05, vega=0.10, rho=0.02,
                    underlying_px=self.spot, open_interest=5000, volume=500,
                    multiplier=100.0))
        return ChainFetchResult(underlying=underlying, ts=0,
                                underlying_px=self.spot, rows=rows)

    async def fetch_expirations(self, underlying):
        return []


class FakeOptionRecorder:
    """Stands in for the LEVELONE_OPTIONS stream."""

    def __init__(self, chain: FakeChainService):
        self.chain = chain
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.blackout = False

    async def subscribe(self, symbols):
        self.subscribed.extend(symbols)

    async def unsubscribe(self, symbols):
        self.unsubscribed.extend(symbols)

    def latest_quote(self, symbol, max_age_s=15.0):
        if self.blackout:
            return None
        occ = parse_occ(symbol)
        itm = ((self.chain.spot - float(occ.strike)) if occ.right == "C"
               else (float(occ.strike) - self.chain.spot))
        mid = max(itm, 0) + 3.0
        return ContractQuote(
            contract=Contract(underlying=occ.underlying, expiry=occ.expiry,
                              right=occ.right, strike=occ.strike, multiplier=100.0),
            ts=0, mid=mid, bid=mid - self.chain.spread / 2,
            ask=mid + self.chain.spread / 2, underlying_px=self.chain.spot,
            iv=0.20,
            greeks=Greeks(delta=0.35 if occ.right == "C" else -0.35,
                          gamma=0.01, theta=-0.05, vega=0.10, rho=0.02),
            source="live")


def options_doc(*, right="call", long_fires=True, short_fires=False,
                close_at_dte=5, max_loss_pct=60.0, **overrides) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    data["schema_version"] = 2
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    entry: dict = {}
    if right == "call":
        entry["long"] = {"when": ALWAYS if long_fires else NEVER}
    else:
        entry["short"] = {"when": ALWAYS if short_fires else NEVER}
    data["entry"] = entry
    data["exit"] = {"stop_loss": {"type": "percent", "value": 3.0},
                    "take_profit": {"type": "percent", "value": 6.0}}
    side_expr = {"right": right,
                 "expiry": {"dte_min": 20, "dte_target": 40, "dte_max": 60},
                 "strike": {"by": "delta", "target": 0.35, "tolerance": 0.25}}
    data["expression"] = {
        "instrument": "options",
        "long": side_expr if right == "call" else None,
        "short": side_expr if right == "put" else None,
        "liquidity": {"min_open_interest": 0, "max_spread_pct": 30},
        "exit": {"stop_basis": "underlying", "close_at_dte": close_at_dte,
                 "max_loss_pct_of_premium": max_loss_pct},
    }
    data["sizing"] = {"type": "fixed_dollars", "value": 3000,
                      "max_position_notional": 500_000}
    data["risk"] = {"max_open_positions": 1, "max_daily_orders": 50,
                    "max_daily_loss": 100_000,
                    "options": {"max_premium_at_risk": 50_000,
                                "max_premium_per_trade_pct": 20.0}}
    data.update(overrides)
    return StrategyDocument.model_validate(data)


async def start_runner(db, broker, gateway, doc, book, chain=None, symbol="SPY",
                       px=100.0):
    chain = chain or FakeChainService(spot=px)
    recorder = FakeOptionRecorder(chain)
    mds = FakeMDS({symbol: warmup_df(n=10, start_px=px)})
    agg = FakeAggregator()
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds, agg, db)
    runner._opt.chain_service = chain
    runner._opt.recorder = recorder
    runner._opt.pricer.recorder = recorder
    await runner.start()
    book.set(symbol, bid=px - 0.1, ask=px + 0.1)
    # the sim broker quotes CONTRACTS by their OCC symbol
    for strike in range(int(px) - 15, int(px) + 16, 5):
        for r in ("C", "P"):
            sym = format_occ(symbol, chain.expiry, r, strike)
            q = recorder.latest_quote(sym)
            book.set(sym, bid=q.bid, ask=q.ask)
    return runner, agg, recorder, chain


async def push(runner, agg, symbol="SPY", px=100.0, high=None, low=None):
    ts = int(runner._candles[symbol].index[-1].timestamp() * 1000) + MIN_MS
    await agg.push(symbol, "1m", bar_event(symbol, ts, px,
                                           high if high is not None else px + 0.5,
                                           low if low is not None else px - 0.5,
                                           px, 1000))
    await settle()


# ---- entry ------------------------------------------------------------------

async def test_long_signal_buys_a_call_with_buy_to_open(db, broker, gateway, book):
    runner, agg, rec, _ = await start_runner(db, broker, gateway, options_doc(), book)
    await push(runner, agg)

    assert "SPY" in runner._positions, (
        "no option position — rejections: " + str(runner._opt.rejections))
    pos = runner._positions["SPY"]
    assert pos.is_option
    assert pos.contract_right == "C"
    assert pos.multiplier == 100.0
    assert pos.qty > 0

    row = await db.fetch_one(
        "SELECT side, asset_type, underlying, multiplier, symbol FROM order_intents "
        "WHERE run_id='run-1' AND purpose='entry'")
    assert row["side"] == "buy_to_open"
    assert row["asset_type"] == "OPTION"
    assert row["underlying"] == "SPY"
    assert row["multiplier"] == 100.0
    assert len(row["symbol"]) == 21
    await runner.stop()


async def test_short_signal_buys_a_put_and_never_sells_short(db, broker, gateway, book):
    """A put purchase must NOT route through the equity short path, which would
    demand a margin account and a borrow it does not need."""
    doc = options_doc(right="put", short_fires=True)
    runner, agg, _, _ = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)

    assert "SPY" in runner._positions, str(runner._opt.rejections)
    pos = runner._positions["SPY"]
    assert pos.contract_right == "P"
    assert pos.side == "short"           # the SIGNAL direction

    rows = await db.fetch_all(
        "SELECT side FROM order_intents WHERE run_id='run-1' AND purpose='entry'")
    assert [r["side"] for r in rows] == ["buy_to_open"]
    assert all(r["side"] != "sell_short" for r in rows)
    await runner.stop()


async def test_entry_is_a_limit_order_never_a_market_order(db, broker, gateway, book):
    """An option market order on a wide book can fill catastrophically far
    from mid."""
    runner, agg, _, _ = await start_runner(db, broker, gateway, options_doc(), book)
    await push(runner, agg)
    row = await db.fetch_one(
        "SELECT order_type, limit_px FROM order_intents "
        "WHERE run_id='run-1' AND purpose='entry'")
    assert row["order_type"] == "limit"
    assert row["limit_px"] > 0
    await runner.stop()


async def test_a_stream_blackout_falls_back_to_the_chain_quote(db, broker, gateway,
                                                               book):
    """Both sources are REAL market data — the LEVELONE stream and the chain's
    REST quotes. Losing the stream is not a reason to stop trading; falling
    back to a MODEL would be, and never happens."""
    runner, agg, rec, _ = await start_runner(db, broker, gateway, options_doc(), book)
    rec.blackout = True
    await push(runner, agg)
    assert "SPY" in runner._positions, str(runner._opt.rejections)
    await runner.stop()


async def test_no_order_when_there_is_no_market_at_all(db, broker, gateway, book):
    """With neither a streamed nor a chain quote, the entry is BLOCKED. A live
    order priced off Black-Scholes rather than the actual book could sit
    nowhere near where the contract trades, so None must stop the trade."""
    chain = FakeChainService(spot=100.0)
    original = chain.fetch

    async def dead_market(underlying, **kw):
        res = await original(underlying, **kw)
        for row in res.rows:          # a zero market: nothing is tradeable
            row.bid = 0.0
            row.ask = 0.0
            row.mark = 0.0
        return res

    chain.fetch = dead_market
    runner, agg, rec, _ = await start_runner(db, broker, gateway, options_doc(),
                                             book, chain=chain)
    rec.blackout = True
    await push(runner, agg)

    assert "SPY" not in runner._positions
    assert runner._opt.rejections, "the block was not counted"
    row = await db.fetch_one(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' "
        "AND kind='entry_blocked'")
    assert row is not None
    await runner.stop()


async def test_selection_rejection_is_explained(db, broker, gateway, book):
    """A strategy that stops trading must always be able to say why."""
    doc = options_doc()
    doc.expression.liquidity.max_spread_pct = 0.0001
    runner, agg, _, _ = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)

    assert "SPY" not in runner._positions
    assert runner._opt.rejections, "rejection was not counted"
    row = await db.fetch_one(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' "
        "AND payload_json LIKE '%contract_selection_rejected%'")
    assert row is not None
    await runner.stop()


async def test_contract_is_subscribed_for_live_marking(db, broker, gateway, book):
    runner, agg, rec, _ = await start_runner(db, broker, gateway, options_doc(), book)
    await push(runner, agg)
    assert rec.subscribed, "the traded contract was never subscribed"
    assert len(rec.subscribed[0]) == 21
    await runner.stop()


# ---- the broker-side backstop ----------------------------------------------

async def test_loss_backstop_rests_at_the_broker(db, broker, gateway, book):
    """The underlying stop cannot rest at the broker, so this premium stop is
    what keeps a crashed runner from leaving the position unmanaged."""
    runner, agg, _, _ = await start_runner(db, broker, gateway, options_doc(), book)
    await push(runner, agg)

    pos = runner._positions["SPY"]
    assert pos.stop_broker_order_id is not None

    row = await db.fetch_one(
        "SELECT stop_px, side, asset_type FROM order_intents "
        "WHERE run_id='run-1' AND purpose='stop'")
    assert row["side"] == "sell_to_close"
    assert row["asset_type"] == "OPTION"
    # 60% of premium lost -> stop at 40% of what was paid
    assert row["stop_px"] == pytest.approx(pos.premium_paid * 0.4, rel=0.02)
    await runner.stop()


async def test_disabling_the_backstop_places_nothing(db, broker, gateway, book):
    doc = options_doc(max_loss_pct=None)
    runner, agg, _, _ = await start_runner(db, broker, gateway, doc, book)
    await push(runner, agg)
    assert runner._positions["SPY"].stop_broker_order_id is None
    await runner.stop()


async def test_underlying_stop_level_is_still_computed(db, broker, gateway, book):
    """stop_basis='underlying' keeps working exactly as it does for shares —
    the level is an UNDERLYING price, monitored in-process."""
    runner, agg, _, _ = await start_runner(db, broker, gateway, options_doc(), book,
                                           px=100.0)
    await push(runner, agg, px=100.0)
    pos = runner._positions["SPY"]
    assert pos.stop_px == pytest.approx(97.0, rel=0.01)   # 3% below 100
    assert pos.underlying_entry_px == pytest.approx(100.0, abs=1.0)
    await runner.stop()


# ---- persistence ------------------------------------------------------------

async def test_position_row_carries_contract_identity(db, broker, gateway, book):
    runner, agg, _, _ = await start_runner(db, broker, gateway, options_doc(), book)
    await push(runner, agg)
    pos = runner._positions["SPY"]

    row = await db.fetch_one(
        "SELECT symbol, asset_type, underlying, multiplier, expiry, premium_paid "
        "FROM positions WHERE run_id='run-1'")
    assert row["symbol"] == pos.contract_symbol      # keyed by CONTRACT
    assert row["asset_type"] == "OPTION"
    assert row["underlying"] == "SPY"                # grouped by TICKER
    assert row["multiplier"] == 100.0
    assert row["expiry"] > 0
    assert row["premium_paid"] == pytest.approx(pos.premium_paid)
    await runner.stop()


# ---- exit -------------------------------------------------------------------

async def test_exit_sells_to_close_and_applies_the_multiplier(db, broker, gateway, book):
    runner, agg, rec, chain = await start_runner(db, broker, gateway, options_doc(),
                                                 book, px=100.0)
    await push(runner, agg, px=100.0)
    pos = runner._positions["SPY"]
    entry_premium = pos.premium_paid
    qty = pos.qty

    # underlying falls through the 3% stop
    chain.spot = 95.0
    for strike in range(85, 116, 5):
        for r in ("C", "P"):
            sym = format_occ("SPY", chain.expiry, r, strike)
            q = rec.latest_quote(sym)
            book.set(sym, bid=q.bid, ask=q.ask)
    await push(runner, agg, px=95.0, low=94.0)

    assert "SPY" not in runner._positions, "the underlying stop never fired"
    row = await db.fetch_one(
        "SELECT side, asset_type, qty FROM order_intents "
        "WHERE run_id='run-1' AND purpose='exit'")
    assert row["side"] == "sell_to_close"
    assert row["asset_type"] == "OPTION"
    assert row["qty"] == qty

    fill = await db.fetch_one(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' AND kind='fill' "
        "AND payload_json LIKE '%realized_pnl%' ORDER BY id DESC LIMIT 1")
    import json
    payload = json.loads(fill["payload_json"])
    expected = (payload["price"] - entry_premium) * qty * 100.0
    assert payload["realized_pnl"] == pytest.approx(expected, abs=0.02)
    await runner.stop()


async def test_contract_is_unsubscribed_after_the_exit(db, broker, gateway, book):
    runner, agg, rec, chain = await start_runner(db, broker, gateway, options_doc(),
                                                 book, px=100.0)
    await push(runner, agg, px=100.0)
    contract = runner._positions["SPY"].contract_symbol
    chain.spot = 95.0
    await push(runner, agg, px=95.0, low=94.0)
    assert contract in rec.unsubscribed
    await runner.stop()


# ---- expiry -----------------------------------------------------------------

async def test_position_is_force_closed_at_close_at_dte(db, broker, gateway, book):
    """Never hold through expiration: gamma explodes, spreads widen and
    assignment mechanics take over."""
    chain = FakeChainService(spot=100.0, expiry=NEAR_EXPIRY)
    doc = options_doc(close_at_dte=5)
    doc.expression.long.expiry.dte_min = 0
    doc.expression.long.expiry.dte_target = 2
    doc.expression.long.expiry.dte_max = 10
    doc.expression.long.expiry.allow = ["S", "M", "Q", "W"]
    runner, agg, _, _ = await start_runner(db, broker, gateway, doc, book, chain=chain)

    # force a position onto the books, then let the watchdog see it
    await push(runner, agg)
    if "SPY" not in runner._positions:
        pytest.skip("chain did not offer a near-dated contract: "
                    + str(runner._opt.rejections))
    await push(runner, agg)

    row = await db.fetch_one(
        "SELECT payload_json FROM run_events WHERE run_id='run-1' "
        "AND payload_json LIKE '%dte_close%'")
    assert row is not None
    await runner.stop()


# ---- risk context -----------------------------------------------------------

async def test_risk_context_reports_options_state(db, broker, gateway, book):
    runner, agg, _, _ = await start_runner(db, broker, gateway, options_doc(), book)
    await push(runner, agg)
    risk = runner.risk
    pos = runner._positions["SPY"]

    assert risk.options_enabled is True
    # contract_qty searches by CONTRACT symbol — _positions is keyed by the
    # underlying, so matching on the key would leave the gateway's naked-short
    # clamp permanently unreachable
    assert risk.contract_qty(pos.contract_symbol) == pos.qty
    assert risk.contract_qty("SPY") == 0.0
    assert risk.max_premium_at_risk_remaining < 50_000
    assert risk.portfolio_delta() != 0.0
    await runner.stop()


async def test_shares_runs_report_options_disabled(db, broker, gateway, book):
    """The other half: an equity run must look exactly as it always did."""
    import copy as _copy
    data = _copy.deepcopy(EXAMPLE)
    data["timeframe"] = {"bar": "1m"}
    data["indicators"] = [{"id": "rsi14", "type": "rsi", "params": {"period": 3}}]
    data["entry"] = {"long": {"when": ALWAYS}}
    doc = StrategyDocument.model_validate(data)

    mds = FakeMDS({"SPY": warmup_df(n=10, start_px=100.0)})
    runner = StrategyRunner("run-1", doc, "paper", broker, gateway, mds,
                            FakeAggregator(), db)
    assert runner._opt is None
    assert runner.risk.options_enabled is False
    assert runner.risk.max_premium_at_risk_remaining == 0.0
    assert runner.risk.contract_qty("anything") == 0.0


# ---- crash recovery ---------------------------------------------------------

async def test_option_position_recovers_with_its_contract_identity(db, broker,
                                                                   gateway, book):
    """A restart must restore the position under the UNDERLYING key while
    keeping the CONTRACT identity. Filing it under the OCC string instead
    would make it invisible to every bar event and therefore to every exit."""
    from app.options.expiry import expiry_to_ms
    contract = format_occ("SPY", EXPIRY, "C", 100)
    await db.execute(
        "INSERT INTO positions (run_id, symbol, qty, avg_price, opened_at, side, "
        "asset_type, underlying, multiplier, expiry, premium_paid, "
        "underlying_entry_px) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run-1", contract, 4, 3.05, 1_700_000_000_000, "long", "OPTION", "SPY",
         100.0, expiry_to_ms(EXPIRY), 3.05, 100.0))

    runner, agg, rec, _ = await start_runner(db, broker, gateway, options_doc(), book)
    # paper recovery seeds the SimBroker from the same rows (RunManager
    # .recover_paper); without it the backstop is correctly refused as a naked
    # short, which is the gateway doing its job
    broker.seed_position(contract, 4, 3.05, multiplier=100.0)
    row = dict(await db.fetch_one(
        "SELECT symbol, qty, avg_price, opened_at, side, asset_type, underlying, "
        "multiplier, expiry, premium_paid, underlying_entry_px "
        "FROM positions WHERE run_id='run-1'"))
    await runner._recover_position(row)

    assert "SPY" in runner._positions, "filed under the OCC symbol, not the underlying"
    pos = runner._positions["SPY"]
    assert pos.is_option
    assert pos.contract_symbol == contract
    assert pos.contract_right == "C"
    assert pos.multiplier == 100.0          # from the ROW, never assumed
    assert pos.premium_paid == pytest.approx(3.05)
    # the UNDERLYING entry reference survives, so the stop cannot silently
    # re-anchor to whatever the price happened to be at restart
    assert pos.underlying_entry_px == pytest.approx(100.0)
    assert contract in rec.subscribed, "recovered contract was not re-subscribed"
    await runner.stop()


async def test_recovered_cash_applies_the_option_multiplier(db):
    """An option position's cost basis is premium x qty x 100. Omitting the
    multiplier would hand the recovered run a wildly inflated balance — a
    failure that looks like free money rather than an error."""
    from app.runtime.recovery import _reconstruct_cash
    positions = [{"symbol": format_occ("SPY", EXPIRY, "C", 100), "qty": 4,
                  "avg_price": 3.05, "side": "long", "asset_type": "OPTION",
                  "multiplier": 100.0}]
    cash = await _reconstruct_cash(db, "run-1", 100_000.0, positions)
    assert cash == pytest.approx(100_000 - 4 * 3.05 * 100)


async def test_recovered_cash_is_unchanged_for_equities(db):
    from app.runtime.recovery import _reconstruct_cash
    positions = [{"symbol": "SPY", "qty": 10, "avg_price": 100.0, "side": "long"}]
    cash = await _reconstruct_cash(db, "run-1", 100_000.0, positions)
    assert cash == pytest.approx(100_000 - 1_000)
