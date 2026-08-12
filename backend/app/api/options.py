"""Options API — chain browsing, recorder coverage, and the watchlist.

READ-ONLY with respect to trading: nothing in this file places, cancels or
modifies an order. The only mutating routes touch the recording watchlist and
trigger a chain snapshot, both of which are data-collection operations.

Everything here is namespaced under /api/options so the options surface is
plainly separable from the equity one in the API, the same way it is in the UI
(plan/16 §13): a reader should never have to guess whether a route is about
shares or contracts.
"""

from __future__ import annotations

import datetime as dt
import time

from fastapi import APIRouter, HTTPException, Query, Request

from ..logging import get_logger
from ..options.chain_service import quote_from_schwab_quote
from ..options.expiry import days_to_expiry, generate_expirations, ms_to_expiry
from ..options.store import OptionChainStore
from ..options.symbology import (InvalidOccSymbol, canonical_underlying,
                                  describe, is_occ, parse_occ)

router = APIRouter(prefix="/api/options")
log = get_logger("api-options")


def _store(request: Request) -> OptionChainStore:
    store = getattr(request.app.state, "option_store", None)
    if store is None:
        raise HTTPException(503, "options subsystem is not initialised")
    return store


def _recorder(request: Request):
    rec = getattr(request.app.state, "option_recorder", None)
    if rec is None:
        raise HTTPException(503, "options recorder is not initialised")
    return rec


def _chains(request: Request):
    svc = getattr(request.app.state, "chain_service", None)
    if svc is None:
        raise HTTPException(503, "chain service is not initialised")
    return svc


# ---- live chain -----------------------------------------------------------

@router.get("/chain")
async def get_chain(request: Request,
                    symbol: str = Query(..., min_length=1, max_length=10),
                    max_dte: int = Query(180, ge=0, le=730),
                    contract_type: str = Query("ALL", pattern="^(ALL|CALL|PUT)$"),
                    strike_count: int = Query(60, ge=2, le=200)):
    """Flattened live chain for the browser UI.

    Returns the validated rows plus the rejection counts, so the UI can say
    "showing 412 of 480 contracts, 68 filtered" rather than silently hiding
    them — a chain that looks thin should be explainable."""
    res = await _chains(request).fetch(
        symbol.strip().upper(), max_dte=max_dte, contract_type=contract_type,
        strike_count=strike_count)
    if res.error and not res.rows:
        raise HTTPException(502, f"chain fetch failed: {res.error}")
    contracts = []
    for r in res.rows:
        expiry_date = ms_to_expiry(r.expiry)
        contracts.append({
            "symbol": r.symbol, "describe": describe(r.symbol),
            "right": r.opt_right, "strike": r.strike,
            "expiry": expiry_date.isoformat(),
            "dte": r.dte if r.dte is not None else days_to_expiry(expiry_date, res.ts),
            "bid": r.bid, "ask": r.ask, "mark": r.mark, "last": r.last,
            "bid_size": r.bid_size, "ask_size": r.ask_size,
            "spread": round((r.ask or 0) - (r.bid or 0), 4),
            "spread_pct": (round(((r.ask or 0) - (r.bid or 0)) / r.mark * 100, 3)
                           if r.mark else None),
            "volume": r.volume, "open_interest": r.open_interest, "iv": r.iv,
            "delta": r.delta, "gamma": r.gamma, "theta": r.theta,
            "vega": r.vega, "rho": r.rho, "multiplier": r.multiplier,
        })
    return {
        "underlying": res.underlying, "underlying_px": res.underlying_px,
        "ts": res.ts, "contracts": contracts,
        "requested": res.requested, "returned": len(contracts),
        "filtered": res.rejected, "filter_reasons": dict(res.reject_reasons),
        "error": res.error,
    }


@router.get("/expirations")
async def get_expirations(request: Request,
                          symbol: str = Query(..., min_length=1, max_length=10),
                          live: bool = Query(True)):
    """Expiration dates for an underlying.

    `live=true` asks Schwab; `live=false` reads what the recorder stored, and
    falls back to the generated calendar when nothing was recorded. The
    fallback is labelled in `source` — a UI must be able to distinguish a real
    listed expiration from one this system merely believes existed."""
    sym = symbol.strip().upper()
    now_ms = int(time.time() * 1000)
    if live:
        try:
            rows = await _chains(request).fetch_expirations(sym)
            await _store(request).upsert_expirations(sym, rows)
            return {"underlying": sym, "source": "schwab", "expirations": [
                {"expiry": ms_to_expiry(r["expiry"]).isoformat(),
                 "dte": days_to_expiry(ms_to_expiry(r["expiry"]), now_ms),
                 "expiration_type": r["expiration_type"],
                 "settlement_type": r["settlement_type"],
                 "standard": bool(r["standard"])} for r in rows]}
        except Exception as e:  # noqa: BLE001 — fall through to stored/generated
            log.warning("expiration_live_fetch_failed", symbol=sym, error=str(e))

    stored = await _store(request).read_expirations(sym, now_ms)
    if stored:
        return {"underlying": sym, "source": "recorded", "expirations": [
            {"expiry": e.expiry.isoformat(), "dte": days_to_expiry(e.expiry, now_ms),
             "expiration_type": e.expiration_type,
             "settlement_type": e.settlement_type, "standard": e.standard}
            for e in stored]}
    gen = generate_expirations(sym, dt.date.today())
    return {"underlying": sym, "source": "generated", "expirations": [
        {"expiry": e.expiry.isoformat(), "dte": days_to_expiry(e.expiry, now_ms),
         "expiration_type": e.expiration_type,
         "settlement_type": e.settlement_type, "standard": e.standard} for e in gen]}


@router.get("/quote")
async def get_quotes(request: Request, symbols: str = Query(..., min_length=1)):
    """Live quotes for one or more OCC option symbols."""
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    bad = [s for s in syms if not is_occ(s)]
    if bad:
        raise HTTPException(400, f"not valid OCC option symbols: {bad}")
    if not syms:
        raise HTTPException(400, "no symbols given")
    schwab = request.app.state.schwab
    payload = await schwab.get_quotes(syms, fields="quote,reference")
    out = []
    for s in syms:
        q = quote_from_schwab_quote(s, payload.get(s) or {})
        out.append(q.to_dict() if q else {"symbol": s, "unavailable": True})
    return {"quotes": out}


# ---- recorded history -----------------------------------------------------

@router.get("/history")
async def get_contract_history(request: Request,
                               symbol: str = Query(..., min_length=21, max_length=21),
                               days: int = Query(180, ge=1, le=2000)):
    """The recorded price series for one contract.

    This is the only historical option data that exists — Schwab serves none —
    so an empty result is normal and must be presented as "not recorded",
    never as "no data available"."""
    if not is_occ(symbol):
        raise HTTPException(400, f"not a valid OCC option symbol: {symbol!r}")
    now = int(time.time() * 1000)
    df = await _store(request).read_contract(symbol, now - days * 86_400_000, now)
    try:
        occ = parse_occ(symbol)
    except InvalidOccSymbol as e:
        raise HTTPException(400, str(e)) from e
    # Streamed quotes for the same contract, at the other resolution. Reported
    # as a COUNT and a range rather than the series: a well-covered contract has
    # tens of thousands of ticks and this endpoint feeds a detail panel, not a
    # download.
    ticks = await _store(request).read_ticks(symbol, now - days * 86_400_000, now)
    tick_info = {"count": 0, "first_ts": None, "last_ts": None}
    if not ticks.empty:
        tick_info = {"count": int(len(ticks)),
                     "first_ts": int(ticks["ts"].iloc[0]),
                     "last_ts": int(ticks["ts"].iloc[-1])}
    return {
        "symbol": symbol, "describe": describe(symbol),
        "underlying": occ.underlying, "right": occ.right,
        "strike": float(occ.strike), "expiry": occ.expiry.isoformat(),
        "points": [] if df.empty else df.to_dict("records"),
        "recorded": 0 if df.empty else len(df),
        "ticks": tick_info,
        "streaming": symbol in _recorder(request).tick_universe_symbols(),
    }


@router.get("/coverage")
async def coverage(request: Request):
    """What the chain recorder has captured so far, per underlying, plus the
    recent snapshot diagnostics. Powers the Data page's options section."""
    store = _store(request)
    rec = _recorder(request)
    return {
        "watchlist": await rec.get_watchlist(),
        "coverage": await store.all_coverage(),
        "recent_runs": await store.recent_runs(limit=40),
        "snapshots_taken": rec.snapshots_taken,
        "rows_recorded": rec.rows_recorded,
        "schedule": rec.schedule_et(),
    }


@router.post("/ticks/refresh")
async def refresh_tick_universe(request: Request):
    """Rebuild the streamed-contract universe now.

    The scheduled refresh is every 15 minutes; this exists so a config change
    or a watchlist edit takes effect immediately rather than costing a quarter
    hour of unrecordable quotes."""
    return await _recorder(request).refresh_tick_universe()


@router.get("/coverage/ticks")
async def tick_coverage(request: Request, days: int = 7):
    """Streamed-quote volume per underlying per day, plus live recorder stats.

    `dropped_conflated` is the number that matters for fidelity: it counts
    quotes that genuinely moved but were suppressed by the minimum write
    interval. A large value means the recording is coarser than the market —
    lower `options_tick_min_interval_ms` if those moves matter to the strategy.
    (`dropped_unchanged` is free: a repeated quote carries no information.)"""
    store = _store(request)
    return {
        "stats": _recorder(request).tick_stats(),
        "summary": await store.tick_summary(),
        "by_day": await store.tick_coverage(days=days),
    }


@router.get("/ticks/universe")
async def tick_universe(request: Request, symbol: str = ""):
    """The OCC symbols currently being streamed, optionally for one underlying.

    Powers the chain browser's "recorded" marks: a strike outside this band is
    getting a 30-minute snapshot and nothing else, and a user choosing which
    strikes to study should be able to see that before spending a month
    collecting the wrong ones."""
    rec = _recorder(request)
    u = (symbol or "").strip().upper().lstrip("$")
    syms = sorted(rec.tick_universe_symbols(u or None))
    return {"underlying": u or None, "count": len(syms), "symbols": syms}


@router.get("/coverage/intraday")
async def intraday_coverage(request: Request, symbol: str, days: int = 30):
    """Snapshots per session day for one underlying.

    Read this before trusting an intraday backtest: a day with one snapshot is
    a day the pricer was extrapolating through, and the result over that day is
    a model output rather than a measurement."""
    return {
        "underlying": (symbol or "").strip().upper().lstrip("$"),
        "days": days,
        "expected_per_day": len(_recorder(request).schedule_et()),
        "by_day": await _store(request).intraday_coverage(
            (symbol or "").strip().upper().lstrip("$"), days=days),
    }


# ---- watchlist ------------------------------------------------------------

@router.get("/watchlist")
async def get_watchlist(request: Request):
    return {"symbols": await _recorder(request).get_watchlist()}


@router.put("/watchlist")
async def set_watchlist(request: Request, body: dict):
    symbols = body.get("symbols")
    if not isinstance(symbols, list) or any(not isinstance(s, str) for s in symbols):
        raise HTTPException(400, "body must be {symbols: string[]}")
    if len(symbols) > 50:
        raise HTTPException(400, "at most 50 underlyings may be recorded")
    saved = await _recorder(request).set_watchlist(symbols)
    return {"symbols": saved}


@router.get("/calibration")
async def calibration(request: Request,
                      symbol: str = Query(..., min_length=1, max_length=10),
                      lookback_days: int = Query(180, ge=1, le=2000)):
    """How wrong the synthetic pricer is, measured against recorded chains.

    This is the number that makes synthetic backtesting defensible rather than
    hand-wavy: instead of "modelled prices are approximate", a user gets
    "median 8% price error at 21-45 DTE and 0.15-0.35 delta" and can apply it
    as a haircut — or conclude that a strategy living in a badly-modelled
    bucket should not be trusted at all.

    Returns `n_observations: 0` (not an error) when nothing has been recorded
    yet, which is the normal state early on."""
    cal = getattr(request.app.state, "calibrator", None)
    if cal is None:
        raise HTTPException(503, "calibrator is not initialised")
    return await cal.report(symbol.strip().upper(), lookback_days=lookback_days)


@router.get("/surface")
async def surface_params(request: Request,
                         symbol: str = Query(..., min_length=1, max_length=10)):
    """The vol-surface and spread parameters currently in force, and whether
    they were FITTED from recorded data or are the documented defaults."""
    cal = getattr(request.app.state, "calibrator", None)
    if cal is None:
        raise HTTPException(503, "calibrator is not initialised")
    surface, spread = await cal.load_params(symbol.strip().upper())
    return {"underlying": symbol.strip().upper(),
            "surface": surface.to_dict(), "spread": spread.to_dict(),
            "calibrated": surface.calibrated and spread.calibrated}


@router.post("/calibrate")
async def run_calibration(request: Request, body: dict | None = None):
    """Refit the surface/spread parameters from recorded chains.

    Refuses to write a fit below the observation threshold: a bad fit is worse
    than the documented default, because it carries the authority of having
    been calibrated while being pure noise."""
    cal = getattr(request.app.state, "calibrator", None)
    if cal is None:
        raise HTTPException(503, "calibrator is not initialised")
    body = body or {}
    symbol = (body.get("symbol") or "").strip().upper()
    lookback = int(body.get("lookback_days") or 180)
    if symbol:
        targets = [symbol]
    else:
        targets = await _store(request).underlyings()
    results = []
    for u in targets:
        res = await cal.fit(u, lookback_days=lookback)
        results.append(res.to_dict())
    return {"results": results}


@router.get("/cost")
async def cost_estimate(request: Request,
                        symbol: str = Query(..., min_length=1, max_length=10),
                        mid: float = Query(..., gt=0),
                        dte: int = Query(30, ge=0, le=730),
                        moneyness: float = Query(0.0),
                        qty: int = Query(1, ge=1, le=1000)):
    """Round-trip cost of a hypothetical position.

    Surfaced as a first-class endpoint because it is the number that decides
    whether a strategy is viable at all, and it belongs in front of the user
    BEFORE they run a backtest rather than after."""
    from ..options.spread import round_trip_cost
    cal = getattr(request.app.state, "calibrator", None)
    sym = symbol.strip().upper()
    _, spread = (await cal.load_params(sym)) if cal else (None, None)
    if spread is None:
        from ..options.spread import SpreadParams
        spread = SpreadParams.default_for(sym)
    return {"underlying": sym,
            **round_trip_cost(mid, spread, moneyness, dte, qty=qty),
            "spread_calibrated": spread.calibrated}


@router.post("/snapshot")
async def snapshot_now(request: Request, body: dict | None = None):
    """Take a chain snapshot immediately.

    Deliberately manual-friendly: the value of recorded data is a function of
    elapsed calendar time, so being able to grab an extra snapshot costs
    nothing and can only help. Idempotent by (symbol, ts)."""
    body = body or {}
    rec = _recorder(request)
    symbol = (body.get("symbol") or "").strip().upper()
    if symbol:
        return {"results": [await rec.snapshot_one(symbol)]}
    return {"results": await rec.snapshot_all()}


@router.post("/select")
async def dry_run_select(request: Request, body: dict):
    """Dry-run contract selection: "given this expression, what would you buy
    right now?"

    Built early and deliberately (plan/16 §12): it turns an abstract config
    block into something checkable without running a whole backtest, and it is
    how "why did my strategy take no trades" gets answered in one click rather
    than one afternoon."""
    from ..options.volsurface import SurfaceParams, VolSurface
    from ..options.pricing import SyntheticPricer
    from ..options.spread import SpreadParams
    from ..strategy.expression import (RealChainView, Rejection, select_contract)
    from ..strategy.schema import Liquidity, SideExpression

    symbol = (body.get("symbol") or "").strip().upper()
    if not symbol:
        raise HTTPException(400, "symbol is required")
    try:
        side_expr = SideExpression.model_validate(body.get("expression") or {})
        liq = Liquidity.model_validate(body.get("liquidity") or {})
    except Exception as e:  # noqa: BLE001 — surface the validation error verbatim
        raise HTTPException(422, f"invalid expression: {e}") from e

    res = await _chains(request).fetch(symbol)
    if res.error and not res.rows:
        raise HTTPException(502, f"chain fetch failed: {res.error}")
    spot = res.underlying_px or 0.0
    if spot <= 0:
        raise HTTPException(502, "no underlying price available")

    # Real chain rows carry real quotes; those are what selection is checked
    # against, so the preview reflects what would actually be bought.
    quotes = {}
    surface = VolSurface(underlying=symbol, atm_30=0.20,
                         params=SurfaceParams.default_for(symbol), source="preview")
    pricer = SyntheticPricer(surface=surface, spread=SpreadParams.default_for(symbol))
    from ..options.contracts import Contract, ContractQuote, Greeks
    from ..options.symbology import parse_occ  # noqa: F811
    for row in res.rows:
        try:
            occ = parse_occ(row.symbol)
        except InvalidOccSymbol:
            continue
        if not row.ask or row.ask <= 0:
            continue
        quotes[row.symbol] = ContractQuote(
            contract=Contract(underlying=canonical_underlying(occ.underlying), expiry=occ.expiry,
                              right=occ.right, strike=occ.strike,
                              multiplier=row.multiplier or 100.0),
            ts=res.ts, mid=row.mark or ((row.bid or 0) + row.ask) / 2,
            bid=row.bid or 0.0, ask=row.ask, underlying_px=spot, iv=row.iv or 0.0,
            greeks=Greeks(delta=row.delta or 0.0, gamma=row.gamma or 0.0,
                          theta=row.theta or 0.0, vega=row.vega or 0.0,
                          rho=row.rho or 0.0),
            volume=row.volume, open_interest=row.open_interest, source="live")

    view = RealChainView(underlying=symbol, rows=res.rows, quotes=quotes)
    got = select_contract(side_expr, liq, view, symbol, spot, res.ts,
                          atr=body.get("atr"), sigma=pricer.surface.atm_30)
    if isinstance(got, Rejection):
        return {"selected": None, "underlying": symbol, "underlying_px": spot,
                "rejection": {"reason": got.reason, "detail": got.detail}}

    q = got.quote
    return {
        "underlying": symbol, "underlying_px": spot, "rejection": None,
        "selected": {
            **got.contract.to_dict(),
            "describe": got.contract.describe(),
            "dte": got.expiry_dte,
            "bid": q.bid if q else None, "ask": q.ask if q else None,
            "mid": q.mid if q else None,
            "spread_pct": q.spread_pct if q else None,
            "iv": q.iv if q else None,
            "delta": q.greeks.delta if q else None,
            "theta": q.greeks.theta if q else None,
            "vega": q.greeks.vega if q else None,
            "open_interest": q.open_interest if q else None,
            "cost_per_contract": round((q.ask if q else 0) * got.contract.multiplier, 2),
        },
    }
