"""End-to-end: create an options strategy through the REAL API, lint it,
backtest it, and read the result back — the same path a user takes.

Deliberately run against a temp strategies directory and a temp database, so
it never touches the strategies on disk or the running paper/live runs. Plan 15
validated shorting the same way (four throwaway strategies, deleted after), and
this is the options equivalent.

What it proves that the unit tests do not: the pieces compose. A document
written by the editor validates, lints, reaches the engine, selects contracts,
produces trades whose P&L is arithmetically checkable, and comes back through
the API with the honesty block attached.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_phase0 import make_settings

BARS = 420


def make_frame(seed=11, drift=0.0005, start_px=100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.011, BARS)
    close = start_px * np.exp(np.cumsum(rets))
    idx = pd.date_range("2024-01-02", periods=BARS, freq="B", tz="UTC")
    open_ = np.concatenate([[start_px], close[:-1]])
    high = np.maximum(close, open_) * 1.004
    low = np.minimum(close, open_) * 0.996
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": 1_000_000}, index=idx)


class FakeMDS:
    """Serves the traded symbol and its $VIX anchor, so the engine takes the
    real vol path rather than the realized-vol fallback."""

    def __init__(self):
        self.frames = {
            "SPY": make_frame(),
            # a VIX series that moves, so the anchor is visibly in use
            "$VIX": make_frame(seed=3, drift=0.0, start_px=18.0),
        }
        self.asked: list[str] = []

    async def get_candles(self, symbol, tf, start_ms, end_ms):
        self.asked.append(symbol)
        return self.frames.get(symbol, pd.DataFrame())

    def latest_cached_quote(self, symbol, max_age_s=10.0):
        return None


OPTIONS_STRATEGY = {
    "schema_version": 2,
    "meta": {"name": "E2E Options Call Buyer",
             "description": "throwaway: buys 35-delta calls on a 5-day SMA cross"},
    "universe": {"symbols": ["SPY"]},
    "timeframe": {"bar": "1d"},
    "indicators": [{"id": "sma5", "type": "sma", "params": {"period": 5}}],
    "entry": {"long": {"when": {"op": "gt", "left": {"price": "close"},
                                "right": {"ind": "sma5"}}},
              "cooldown_bars": 5},
    "exit": {"stop_loss": {"type": "percent", "value": 3.0},
             "take_profit": {"type": "percent", "value": 7.0}},
    "expression": {
        "instrument": "options",
        "long": {"right": "call",
                 "expiry": {"dte_min": 25, "dte_target": 40, "dte_max": 60,
                            "allow": ["S", "M", "Q"]},
                 "strike": {"by": "delta", "target": 0.35, "tolerance": 0.2}},
        "liquidity": {"min_open_interest": 0, "max_spread_pct": 25},
        "exit": {"stop_basis": "underlying", "close_at_dte": 3,
                 "max_loss_pct_of_premium": 60},
        "pricing": {"vrp_mult": 1.15, "fill_aggression": 1.0},
    },
    "sizing": {"type": "percent_of_equity", "value": 3,
               "max_position_notional": 200_000},
    "risk": {"max_open_positions": 1, "max_daily_loss": 50_000,
             "options": {"max_premium_at_risk": 20_000,
                         "max_premium_per_trade_pct": 5.0}},
}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    import app.config as config_mod
    import app.main as main_mod

    # `autotrader_strategies_dir` MUST be overridden: it defaults to the REAL
    # repo strategies/ folder, so a test that creates a strategy through the API
    # writes into it for real. Every other API test file does this; omitting it
    # here created three throwaway strategies on disk before it was caught.
    s = make_settings(tmp_path, autotrader_strategies_dir=tmp_path / "strategies")
    monkeypatch.setattr(config_mod, "get_settings", lambda: s)
    monkeypatch.setattr(main_mod, "get_settings", lambda: s)

    async with main_mod.app.router.lifespan_context(main_mod.app):
        app = main_mod.app
        mds = FakeMDS()
        app.state.marketdata = mds
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test",
                               timeout=120) as c:
            yield c, app, mds


async def test_options_strategy_round_trip(client):
    c, app, mds = client

    # ---- 1. create it, exactly as the editor does -------------------------
    created = await c.post("/api/strategies", json={"document": OPTIONS_STRATEGY})
    assert created.status_code in (200, 201), created.text
    slug = created.json()["slug"]

    # ---- 2. it reads back as an options strategy -------------------------
    listing = (await c.get("/api/strategies")).json()["strategies"]
    row = next(s for s in listing if s["slug"] == slug)
    assert row["instrument"] == "options", "the list must badge it as options"

    detail = (await c.get(f"/api/strategies/{slug}")).json()
    assert detail["document"]["expression"]["instrument"] == "options"
    assert detail["document"]["expression"]["long"]["right"] == "call"

    # ---- 3. it lints clean ------------------------------------------------
    lint = (await c.post("/api/strategies/validate",
                         json={"document": OPTIONS_STRATEGY})).json()
    assert lint["valid"] is True, lint["errors"]
    assert lint["errors"] == []

    # ---- 4. backtest it ---------------------------------------------------
    started = await c.post("/api/backtests", json={
        "slug": slug,
        "config": {"start": "2024-02-01", "end": "2025-06-01",
                   "initial_capital": 100_000},
    })
    assert started.status_code in (200, 201), started.text
    backtest_id = started.json()["id"]

    result = (await c.get(f"/api/backtests/{backtest_id}")).json()
    assert result["status"] == "done", result.get("error")

    # the $VIX anchor must have been fetched — otherwise the engine silently
    # fell back to realized vol, which is a materially different backtest
    assert "$VIX" in mds.asked, "the volatility anchor was never requested"

    # ---- 5. it traded, and the trades are self-describing options --------
    trades = result["trades"]
    assert trades, ("no trades — rejections: "
                    + str(result["assumptions"].get("options", {})
                          .get("selection_rejections")))
    for t in trades:
        assert t["instrument"] == "option"
        assert len(t["contract"]) == 21
        assert t["multiplier"] == 100.0
        assert t["dte_at_entry"] >= 25
        assert t["dte_at_exit"] >= 0, "held through expiration"

    # ---- 6. every trade's P&L checks out by hand -------------------------
    for t in trades:
        gross = (t["exit_px"] - t["entry_px"]) * t["qty"] * t["multiplier"]
        expected = gross - 2 * 0.65 * t["qty"]
        tol = 2e-4 * t["qty"] * t["multiplier"] + 0.01
        assert t["pnl"] == pytest.approx(expected, abs=tol), t
        # and a long option can never lose more than its premium
        assert t["pnl"] >= -(t["premium_paid"] + 2 * 0.65 * t["qty"]) - 0.01

    # ---- 7. the honesty block came back ----------------------------------
    opts = result["assumptions"]["options"]
    assert opts["pricing_source"] == "synthetic"
    assert "Black-Scholes" in opts["model"]
    assert opts["vrp_mult"] == 1.15
    assert "min_open_interest" in opts["liquidity_NOT_enforced"]
    assert opts["expired_unclosed_count"] == 0
    assert "vol_index_anchor" in opts["vol_source_mix"], opts["vol_source_mix"]
    assert "return_distribution_warning" in result["metrics"]

    # ---- 8. promotion refuses until the options gates are satisfied ------
    promo = (await c.get("/api/runs/live/eligibility",
                         params={"slug": slug})).json()
    assert promo["is_options"] is True
    assert promo["eligible"] is False        # no paper run yet
    assert isinstance(promo["options_issues"], list)


async def test_a_shares_strategy_is_unaffected_end_to_end(client):
    """The other half of every guarantee in this project: an equity strategy
    goes through the same path and gains nothing options-flavoured."""
    c, app, mds = client
    doc = {k: v for k, v in OPTIONS_STRATEGY.items() if k != "expression"}
    doc["meta"] = {"name": "E2E Shares Control"}
    doc["risk"] = {"max_open_positions": 1, "max_daily_loss": 50_000}

    created = await c.post("/api/strategies", json={"document": doc})
    assert created.status_code in (200, 201), created.text
    slug = created.json()["slug"]

    row = next(s for s in (await c.get("/api/strategies")).json()["strategies"]
               if s["slug"] == slug)
    assert row["instrument"] == "shares"

    started = await c.post("/api/backtests", json={
        "slug": slug,
        "config": {"start": "2024-02-01", "end": "2025-06-01",
                   "initial_capital": 100_000}})
    result = (await c.get(f"/api/backtests/{started.json()['id']}")).json()
    assert result["status"] == "done"
    assert "options" not in result["assumptions"]
    assert "return_distribution_warning" not in result["metrics"]
    assert all(t.get("instrument") != "option" for t in result["trades"])


async def test_a_document_with_no_broker_side_protection_cannot_even_be_saved(client):
    """Stronger than a promotion block: an options document whose stop is
    monitored in-process AND has no broker-side backstop is refused at SAVE.

    That falls out of the existing blocking-gap convention rather than being a
    special case, and it is the right outcome — such a position has no
    protection at all if this app dies, so it should not become a stored
    strategy someone later promotes by habit."""
    c, app, mds = client
    doc = {**OPTIONS_STRATEGY, "meta": {"name": "E2E Options Unprotected"}}
    doc["expression"] = {
        **OPTIONS_STRATEGY["expression"],
        "exit": {"stop_basis": "underlying", "close_at_dte": 3,
                 "max_loss_pct_of_premium": None},
    }
    created = await c.post("/api/strategies", json={"document": doc})
    assert created.status_code == 400
    errors = " ".join(created.json()["detail"]["errors"])
    assert "stop_basis" in errors
    assert "NO broker-side protection" in errors


async def test_promotion_gate_reports_informed_consent_issues(client):
    """Saveable, but not promotable: the default premium cap and a weakened
    variance risk premium are choices a user must make deliberately."""
    c, app, mds = client
    doc = {**OPTIONS_STRATEGY, "meta": {"name": "E2E Options Consent"}}
    doc["risk"] = {"max_open_positions": 1, "max_daily_loss": 50_000}   # default caps
    doc["expression"] = {
        **OPTIONS_STRATEGY["expression"],
        "pricing": {"vrp_mult": 0.7, "fill_aggression": 1.0},
    }
    created = await c.post("/api/strategies", json={"document": doc})
    assert created.status_code in (200, 201), created.text
    slug = created.json()["slug"]

    promo = (await c.get("/api/runs/live/eligibility",
                         params={"slug": slug})).json()
    issues = " ".join(promo["options_issues"])
    assert "max_premium_at_risk" in issues
    assert "vrp_mult" in issues
    assert promo["eligible"] is False
