"""Options API, wired through the REAL app lifespan (same ASGI-transport
pattern as test_api_orders.py). Confirms the options stack is constructed at
startup, that its routes are namespaced and reachable, and — importantly — that
with an empty watchlist the whole subsystem is inert."""

from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.options.store import SnapshotRow
from app.options.expiry import expiry_to_ms
from tests.test_phase0 import make_settings

import datetime as dt

NOW = int(time.time() * 1000)
SYM = "SPY   260918C00680000"


class FakeSchwab:
    """Stands in for the real client on app.state so no network is touched."""

    def __init__(self):
        self.chain_calls = []

    async def get_option_chain(self, symbol, **kw):
        self.chain_calls.append((symbol, kw))
        return {
            "symbol": symbol, "status": "SUCCESS", "underlyingPrice": 672.30,
            "callExpDateMap": {"2026-09-18:50": {"680.0": [{
                "putCall": "CALL", "symbol": SYM, "bidPrice": 6.40, "askPrice": 6.50,
                "mark": 6.45, "strikePrice": 680.0, "delta": 0.35, "gamma": 0.012,
                "theta": -0.085, "vega": 0.61, "rho": 0.21, "volatility": 18.42,
                "totalVolume": 2400, "openInterest": 15800, "daysToExpiration": 50,
                "multiplier": 100.0, "quoteTimeInLong": NOW - 5000,
                "bidSize": 40, "askSize": 55,
            }]}},
            "putExpDateMap": {},
        }

    async def get_expiration_chain(self, symbol):
        return {"expirationList": [
            {"expiration": "2026-09-18", "expirationType": "Q",
             "settlementType": "P", "standard": True}]}

    async def get_quotes(self, symbols, fields="quote"):
        return {SYM: {"quote": {"bidPrice": 6.40, "askPrice": 6.50, "mark": 6.45,
                                "delta": 0.35, "volatility": 18.42,
                                "underlyingPrice": 672.30, "quoteTime": NOW},
                      "reference": {"multiplier": 100.0}}}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    import app.config as config_mod
    import app.main as main_mod

    s = make_settings(tmp_path)
    monkeypatch.setattr(config_mod, "get_settings", lambda: s)
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)

    async with main_mod.app.router.lifespan_context(main_mod.app):
        app = main_mod.app
        fake = FakeSchwab()
        app.state.chain_service._schwab = fake
        app.state.fake_schwab = fake
        app.state.schwab = fake
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, app


# ---- wiring ---------------------------------------------------------------

async def test_options_stack_is_constructed_at_startup(client):
    _, app = client
    assert app.state.option_store is not None
    assert app.state.chain_service is not None
    assert app.state.option_recorder is not None


async def test_empty_watchlist_means_the_subsystem_is_inert(client):
    """The default state. Options must cost nothing until the user opts in —
    this is what keeps the running equity strategies unaffected."""
    c, app = client
    r = await c.get("/api/options/watchlist")
    assert r.status_code == 200 and r.json()["symbols"] == []
    assert await app.state.option_recorder.snapshot_all() == []
    assert app.state.fake_schwab.chain_calls == []


# ---- chain ----------------------------------------------------------------

async def test_get_chain(client):
    c, _ = client
    r = await c.get("/api/options/chain", params={"symbol": "spy"})
    assert r.status_code == 200
    body = r.json()
    assert body["underlying"] == "SPY"
    assert body["underlying_px"] == pytest.approx(672.30)
    assert body["returned"] == 1
    contract = body["contracts"][0]
    assert contract["symbol"] == SYM
    assert contract["describe"] == "SPY 18Sep26 680C"
    assert contract["right"] == "C"
    assert contract["strike"] == 680.0
    assert contract["spread"] == pytest.approx(0.10)
    assert contract["iv"] == pytest.approx(0.1842)


async def test_chain_reports_what_it_filtered(client):
    """A thin-looking chain must be explainable, not mysterious."""
    c, _ = client
    body = (await c.get("/api/options/chain", params={"symbol": "SPY"})).json()
    assert "filtered" in body and "filter_reasons" in body
    assert body["requested"] >= body["returned"]


# ---- expirations ----------------------------------------------------------

async def test_expirations_live(client):
    c, _ = client
    body = (await c.get("/api/options/expirations",
                        params={"symbol": "SPY", "live": True})).json()
    assert body["source"] == "schwab"
    assert body["expirations"][0]["expiry"] == "2026-09-18"


async def test_expirations_fall_back_to_generated_and_say_so(client):
    """A UI must be able to distinguish a real listed expiration from one this
    system merely believes existed."""
    c, _ = client
    body = (await c.get("/api/options/expirations",
                        params={"symbol": "KO", "live": False})).json()
    assert body["source"] == "generated"
    assert body["expirations"]


# ---- quotes ---------------------------------------------------------------

async def test_quote_requires_occ_symbols(client):
    c, _ = client
    r = await c.get("/api/options/quote", params={"symbols": "SPY"})
    assert r.status_code == 400
    assert "OCC" in r.json()["detail"]


async def test_quote_returns_live_quote(client):
    c, _ = client
    body = (await c.get("/api/options/quote", params={"symbols": SYM})).json()
    q = body["quotes"][0]
    assert q["bid"] == 6.40 and q["ask"] == 6.50
    assert q["source"] == "live"


# ---- history / coverage ---------------------------------------------------

async def test_history_is_empty_when_nothing_recorded(client):
    c, _ = client
    body = (await c.get("/api/options/history", params={"symbol": SYM})).json()
    assert body["recorded"] == 0 and body["points"] == []
    assert body["strike"] == 680.0 and body["right"] == "C"


async def test_history_rejects_a_non_option_symbol(client):
    c, _ = client
    assert (await c.get("/api/options/history", params={"symbol": "SPY"})).status_code == 422


async def test_coverage_after_a_snapshot(client):
    c, app = client
    await app.state.option_store.upsert_snapshot([SnapshotRow(
        underlying="SPY", ts=NOW, symbol=SYM, expiry=expiry_to_ms(dt.date(2026, 9, 18)),
        strike=680.0, opt_right="C", bid=6.4, ask=6.5, mark=6.45)])
    body = (await c.get("/api/options/coverage")).json()
    assert body["coverage"][0]["underlying"] == "SPY"
    assert body["coverage"][0]["rows"] == 1


# ---- watchlist ------------------------------------------------------------

async def test_watchlist_put_normalises(client):
    c, _ = client
    r = await c.put("/api/options/watchlist", json={"symbols": [" spy ", "qqq", "SPY"]})
    assert r.status_code == 200
    assert r.json()["symbols"] == ["QQQ", "SPY"]
    assert (await c.get("/api/options/watchlist")).json()["symbols"] == ["QQQ", "SPY"]


@pytest.mark.parametrize("body", [{}, {"symbols": "SPY"}, {"symbols": [1, 2]}])
async def test_watchlist_put_rejects_bad_bodies(client, body):
    c, _ = client
    assert (await c.put("/api/options/watchlist", json=body)).status_code == 400


async def test_watchlist_is_capped(client):
    c, _ = client
    r = await c.put("/api/options/watchlist",
                    json={"symbols": [f"SYM{i}" for i in range(60)]})
    assert r.status_code == 400


async def test_manual_snapshot(client):
    c, app = client
    r = await c.post("/api/options/snapshot", json={"symbol": "SPY"})
    assert r.status_code == 200
    assert r.json()["results"][0]["written"] == 1
    cov = await app.state.option_store.coverage("SPY")
    assert cov["rows"] == 1


# ---- calibration & cost (phase 2) -----------------------------------------

async def test_calibration_report_with_no_data(client):
    """The normal early state: no recorded chains yet. Must report zero
    observations rather than erroring — the absence of data is information."""
    c, _ = client
    body = (await c.get("/api/options/calibration", params={"symbol": "SPY"})).json()
    assert body["n_observations"] == 0
    assert "note" in body


async def test_surface_params_default_to_uncalibrated(client):
    """A user must always be able to tell a FITTED surface from a guessed one."""
    c, _ = client
    body = (await c.get("/api/options/surface", params={"symbol": "SPY"})).json()
    assert body["calibrated"] is False
    assert body["surface"]["vrp_mult"] == pytest.approx(1.15)
    assert body["surface"]["skew_b"] < 0        # downside skew by default
    assert body["spread"]["tier"] == 1          # SPY is the most liquid tier


async def test_calibrate_refuses_a_fit_without_enough_data(client):
    c, _ = client
    r = await c.post("/api/options/calibrate", json={"symbol": "SPY"})
    assert r.status_code == 200
    results = r.json()["results"]
    assert results[0]["written"] is False
    assert "observations" in results[0]["reason"]


async def test_cost_endpoint_surfaces_the_round_trip_arithmetic(client):
    """The number that decides whether a strategy is viable at all, in front of
    the user BEFORE they run a backtest rather than after."""
    c, _ = client
    body = (await c.get("/api/options/cost",
                        params={"symbol": "SPY", "mid": 3.0, "dte": 30})).json()
    assert body["premium"] == pytest.approx(300.0)
    assert body["commission"] == pytest.approx(1.30)
    assert 0.5 < body["pct_of_premium"] < 3.0
    assert body["spread_calibrated"] is False


async def test_cost_endpoint_shows_illiquid_names_are_brutal(client):
    c, _ = client
    liquid = (await c.get("/api/options/cost",
                          params={"symbol": "SPY", "mid": 3.0, "dte": 7})).json()
    junk = (await c.get("/api/options/cost",
                        params={"symbol": "ZZZZ", "mid": 3.0, "dte": 7})).json()
    assert junk["pct_of_premium"] > 4 * liquid["pct_of_premium"]
