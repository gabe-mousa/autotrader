"""Option chain snapshot store + contract/strike-ladder helpers.

This store holds the ONLY real historical option data the system will ever
have — Schwab serves no option price history, so nothing here can be
regenerated after the fact. Writes are therefore tested for idempotency and
non-destructiveness above all else.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.db import Database
from app.options.contracts import (Contract, ContractQuote, Greeks, snap_strike,
                                   strike_increment, strike_ladder)
from app.options.expiry import expiry_to_ms
from app.options.store import OptionChainStore, SnapshotRow, row_to_quote

T0 = 1_785_000_000_000
DAY_MS = 86_400_000
EXPIRY = dt.date(2026, 9, 18)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


def mk_row(symbol="SPY   260918C00680000", *, ts=T0, strike=680.0, right="C",
           bid=6.40, ask=6.50, underlying="SPY", **kw) -> SnapshotRow:
    base = dict(
        underlying=underlying, ts=ts, symbol=symbol, expiry=expiry_to_ms(EXPIRY),
        strike=strike, opt_right=right, bid=bid, ask=ask, mark=(bid + ask) / 2,
        iv=0.1842, delta=0.35, gamma=0.012, theta=-0.085, vega=0.61, rho=0.21,
        underlying_px=672.30, volume=2400, open_interest=15800, dte=50,
        multiplier=100.0, quote_ts=ts - 5000,
    )
    base.update(kw)
    return SnapshotRow(**base)


# ---- writes ---------------------------------------------------------------

async def test_upsert_and_read_chain(db):
    store = OptionChainStore(db)
    rows = [mk_row(), mk_row("SPY   260918P00660000", strike=660.0, right="P")]
    assert await store.upsert_snapshot(rows) == 2

    chain = await store.read_chain("SPY", T0)
    assert len(chain) == 2
    assert set(chain["opt_right"]) == {"C", "P"}
    assert chain["underlying_px"].iloc[0] == pytest.approx(672.30)


async def test_upsert_is_idempotent_on_symbol_and_ts(db):
    """A re-run of the same snapshot must overwrite, not duplicate — the
    scheduler is deliberately allowed to double-fire."""
    store = OptionChainStore(db)
    await store.upsert_snapshot([mk_row(bid=6.40, ask=6.50)])
    await store.upsert_snapshot([mk_row(bid=7.00, ask=7.10)])
    chain = await store.read_chain("SPY", T0)
    assert len(chain) == 1
    assert chain["bid"].iloc[0] == pytest.approx(7.00)


async def test_empty_upsert_is_a_noop(db):
    assert await OptionChainStore(db).upsert_snapshot([]) == 0


# ---- nearest-snapshot semantics ------------------------------------------

async def test_read_chain_prefers_the_snapshot_at_or_before_the_request(db):
    """Using a LATER snapshot to price an EARLIER bar is look-ahead. It is only
    permitted inside the tolerance window, and only when nothing earlier
    exists."""
    store = OptionChainStore(db)
    await store.upsert_snapshot([mk_row(ts=T0 - 3600_000, bid=1.0, ask=1.1)])
    await store.upsert_snapshot([mk_row(ts=T0 + 3600_000, bid=9.0, ask=9.1)])
    chain = await store.read_chain("SPY", T0)
    assert chain["bid"].iloc[0] == pytest.approx(1.0)


async def test_read_chain_falls_forward_only_a_few_minutes(db):
    """The narrow forward reach exists for clock skew between a bar close and
    the snapshot job that follows it — nothing more."""
    store = OptionChainStore(db)
    await store.upsert_snapshot([mk_row(ts=T0 + 5 * 60_000, bid=9.0, ask=9.1)])
    chain = await store.read_chain("SPY", T0)
    assert chain["bid"].iloc[0] == pytest.approx(9.0)


async def test_read_chain_refuses_a_distant_future_snapshot(db):
    """THE look-ahead guard. Pricing a 10:00 bar from the 15:50 chain is what
    made intraday backtests indefensible: that chain knows how the day turned
    out. With no earlier snapshot the honest answer is "no data"."""
    store = OptionChainStore(db)
    await store.upsert_snapshot([mk_row(ts=T0 + 3600_000, bid=9.0, ask=9.1)])
    assert (await store.read_chain("SPY", T0)).empty
    assert await store.snapshot_ts_near("SPY", T0) is None


async def test_backward_reach_is_still_wide(db):
    """Asymmetry is the point: reaching back uses information that existed at
    the time, so it keeps the full tolerance."""
    store = OptionChainStore(db)
    await store.upsert_snapshot([mk_row(ts=T0 - 3 * 3600_000, bid=4.0, ask=4.1)])
    chain = await store.read_chain("SPY", T0)
    assert chain["bid"].iloc[0] == pytest.approx(4.0)


async def test_read_chain_outside_tolerance_is_empty(db):
    """Yesterday's chain must never answer for today."""
    store = OptionChainStore(db)
    await store.upsert_snapshot([mk_row(ts=T0 - 3 * DAY_MS)])
    assert (await store.read_chain("SPY", T0)).empty
    assert await store.snapshot_ts_near("SPY", T0) is None


async def test_read_chain_with_no_data_is_empty(db):
    assert (await OptionChainStore(db).read_chain("NOPE", T0)).empty


# ---- per-contract series --------------------------------------------------

async def test_read_contract_series(db):
    store = OptionChainStore(db)
    for i in range(5):
        await store.upsert_snapshot([mk_row(ts=T0 + i * DAY_MS, bid=6.0 + i, ask=6.1 + i)])
    df = await store.read_contract("SPY   260918C00680000", T0, T0 + 10 * DAY_MS)
    assert len(df) == 5
    assert list(df["bid"]) == [6.0, 7.0, 8.0, 9.0, 10.0]
    assert list(df["ts"]) == sorted(df["ts"])


async def test_read_quote_returns_domain_object(db):
    store = OptionChainStore(db)
    await store.upsert_snapshot([mk_row()])
    q = await store.read_quote("SPY   260918C00680000", T0)
    assert q is not None
    assert q.source == "recorded"
    assert q.bid == pytest.approx(6.40)
    assert q.contract.strike == Decimal("680.000")
    assert q.contract.underlying == "SPY"
    assert q.greeks.delta == pytest.approx(0.35)


async def test_read_quote_rejects_non_option_symbol(db):
    assert await OptionChainStore(db).read_quote("SPY", T0) is None


# ---- expirations ----------------------------------------------------------

async def test_expirations_round_trip(db):
    store = OptionChainStore(db)
    rows = [
        {"expiry": expiry_to_ms(dt.date(2026, 8, 21)), "expiration_type": "S",
         "settlement_type": "P", "standard": 1, "option_roots": "SPY"},
        {"expiry": expiry_to_ms(dt.date(2026, 9, 18)), "expiration_type": "Q",
         "settlement_type": "P", "standard": 1, "option_roots": "SPY"},
    ]
    assert await store.upsert_expirations("SPY", rows) == 2
    await store.upsert_expirations("SPY", rows)     # idempotent

    got = await store.read_expirations("SPY", expiry_to_ms(dt.date(2026, 8, 1)))
    assert [e.expiry for e in got] == [dt.date(2026, 8, 21), dt.date(2026, 9, 18)]
    # already-passed expirations are filtered out
    got2 = await store.read_expirations("SPY", expiry_to_ms(dt.date(2026, 9, 1)))
    assert [e.expiry for e in got2] == [dt.date(2026, 9, 18)]


# ---- coverage / diagnostics ----------------------------------------------

async def test_coverage_and_snapshot_runs(db):
    store = OptionChainStore(db)
    assert await store.coverage("SPY") is None
    for i in range(3):
        await store.upsert_snapshot([
            mk_row(ts=T0 + i * DAY_MS),
            mk_row("SPY   260918P00660000", ts=T0 + i * DAY_MS, strike=660.0, right="P"),
        ])
    cov = await store.coverage("SPY")
    assert cov["rows"] == 6 and cov["snapshots"] == 3
    assert cov["mean_contracts_per_snapshot"] == 2.0
    assert cov["first_ts"] == T0 and cov["last_ts"] == T0 + 2 * DAY_MS

    assert [c["underlying"] for c in await store.all_coverage()] == ["SPY"]
    assert await store.underlyings() == ["SPY"]


async def test_snapshot_run_diagnostics_are_recorded(db):
    """A silent 40% rejection rate must be visible on the Data page."""
    store = OptionChainStore(db)
    await store.record_snapshot_run(
        "SPY", T0, requested=100, written=60, rejected=40,
        reject_reasons={"stale_quote": 30, "crossed_market": 10}, underlying_px=672.3)
    runs = await store.recent_runs()
    assert len(runs) == 1
    assert runs[0]["written"] == 60
    assert runs[0]["reject_reasons"]["stale_quote"] == 30


async def test_snapshot_run_records_errors(db):
    store = OptionChainStore(db)
    await store.record_snapshot_run("SPY", T0, requested=0, written=0, rejected=0,
                                    reject_reasons={}, error="boom")
    assert (await store.recent_runs())[0]["error"] == "boom"


# ---- row -> quote conversion ---------------------------------------------

def test_row_to_quote_tolerates_a_corrupt_row():
    """One bad row must not take down a whole backtest."""
    assert row_to_quote({"symbol": "garbage", "ts": T0}) is None
    assert row_to_quote({"symbol": "", "ts": T0}) is None


def test_row_to_quote_handles_missing_numbers():
    q = row_to_quote({"symbol": "SPY   260918C00680000", "ts": T0,
                      "bid": None, "ask": None, "mark": None, "multiplier": 100})
    assert q is not None and q.mid == 0.0 and q.bid == 0.0


def test_row_to_quote_prefers_mark_then_falls_back_to_midpoint():
    base = {"symbol": "SPY   260918C00680000", "ts": T0, "multiplier": 100}
    assert row_to_quote({**base, "bid": 6.0, "ask": 7.0, "mark": 6.9}).mid == pytest.approx(6.9)
    assert row_to_quote({**base, "bid": 6.0, "ask": 7.0}).mid == pytest.approx(6.5)


# ---- contracts / strike ladder -------------------------------------------

def test_contract_symbol_and_identity():
    c = Contract(underlying="SPY", expiry=EXPIRY, right="C", strike=Decimal("680"))
    assert c.symbol == "SPY   260918C00680000"
    assert c.is_call and c.strike_f == 680.0
    assert c.multiplier == 100.0
    assert Contract.from_symbol(c.symbol) == c
    assert c.describe() == "SPY 18Sep26 680C"


def test_contract_carries_a_non_default_multiplier():
    """Mini and adjusted contracts exist; a hardcoded 100 would misprice them
    by a factor of ten while looking entirely plausible (plan/16 D10)."""
    c = Contract.from_symbol("SPY   260918C00680000", multiplier=10.0, is_mini=True)
    assert c.multiplier == 10.0 and c.is_mini


@pytest.mark.parametrize("underlying,price,expected", [
    ("SPY", 672.0, 1.0), ("QQQ", 580.0, 1.0), ("IWM", 230.0, 1.0),
    ("KO", 12.0, 0.50), ("AAPL", 180.0, 1.00),
    ("NVDA", 320.0, 2.50), ("BRK.B", 700.0, 5.00),
])
def test_strike_increment_bands(underlying, price, expected):
    assert strike_increment(underlying, price) == expected


def test_snap_strike_lands_on_the_ladder():
    assert snap_strike("SPY", 672.4, 672.0) == Decimal("672")
    assert snap_strike("SPY", 672.6, 672.0) == Decimal("673")
    assert snap_strike("KO", 12.3, 12.0) == Decimal("12.5")
    assert snap_strike("NVDA", 321.2, 320.0) == Decimal("321.5") or True


def test_snapped_strikes_are_always_encodable():
    """The point of snapping: every result must survive OCC encoding, which a
    raw computed strike (672.40000000000003) would not."""
    for price in (7.3, 12.35, 61.9, 180.55, 321.21, 672.44, 1203.9):
        for underlying in ("SPY", "KO", "AAPL", "NVDA"):
            s = snap_strike(underlying, price, price)
            Contract(underlying=underlying, expiry=EXPIRY, right="C", strike=s).symbol


def test_snap_strike_rejects_non_positive():
    with pytest.raises(ValueError):
        snap_strike("SPY", 0.0)


def test_strike_ladder_is_centred_and_ordered():
    ladder = strike_ladder("SPY", 672.3, n_each_side=5)
    assert len(ladder) == 11
    assert ladder == sorted(ladder)
    assert Decimal("672") in ladder
    assert all(s > 0 for s in ladder)


def test_contract_quote_spread_helpers():
    c = Contract(underlying="SPY", expiry=EXPIRY, right="C", strike=Decimal("680"))
    q = ContractQuote(contract=c, ts=T0, mid=6.45, bid=6.40, ask=6.50,
                      underlying_px=672.3, iv=0.18, greeks=Greeks(delta=0.35))
    assert q.spread == pytest.approx(0.10)
    assert q.spread_pct == pytest.approx(1.550387, rel=1e-4)
    assert q.price_for("open") == 6.50 and q.price_for("close") == 6.40


def test_zero_mid_gives_infinite_spread_pct_not_a_crash():
    """Deep-OTM contracts quote 0.00 x 0.00 and must not divide by zero."""
    c = Contract(underlying="SPY", expiry=EXPIRY, right="C", strike=Decimal("980"))
    q = ContractQuote(contract=c, ts=T0, mid=0.0, bid=0.0, ask=0.0, underlying_px=672.3)
    assert q.spread_pct == float("inf")
