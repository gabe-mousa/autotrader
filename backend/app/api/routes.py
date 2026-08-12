"""HTTP API. No app-level auth by design — server binds 127.0.0.1 only."""

from __future__ import annotations

import asyncio
import json
import webbrowser

import pandas as pd
from fastapi import APIRouter, HTTPException, Request

from ..auth.callback import wait_for_code
from ..auth.manager import AuthError, NotAuthenticatedError
from ..logging import get_logger
from ..marketdata.store import SCHWAB_INTRADAY_TFS
from ..schwab.client import SchwabAPIError, summarize_accounts

# order shown in the Data page coverage table: fastest native tf first
COVERAGE_TFS = ("1m", *sorted(SCHWAB_INTRADAY_TFS, key=lambda t: int(t[:-1])), "1d")

router = APIRouter(prefix="/api")
log = get_logger("api")


@router.get("/health")
async def health(request: Request) -> dict:
    app = request.app.state
    return {
        "status": "ok",
        "auth": app.auth.status(),
        "streamer": app.streamer.state,
        "ai_configured": app.settings.ai_configured,
    }


@router.get("/activity")
async def activity(request: Request, limit: int = 50) -> dict:
    """Tail of the audit log for the dashboard's recent-activity feed."""
    app = request.app.state
    limit = max(1, min(limit, 500))
    rows = await app.db.fetch_all(
        "SELECT ts, actor, category, payload_json FROM audit_log ORDER BY ts DESC, id DESC LIMIT ?",
        (limit,),
    )
    return {
        "activity": [
            {
                "ts": r["ts"],
                "actor": r["actor"],
                "category": r["category"],
                "payload": json.loads(r["payload_json"]),
            }
            for r in rows
        ]
    }


@router.get("/auth/status")
async def auth_status(request: Request) -> dict:
    return request.app.state.auth.status()


@router.post("/auth/connect")
async def auth_connect(request: Request) -> dict:
    """Start the OAuth flow: spin up the callback listener, return the authorize
    URL (also try to open the browser locally), and complete the exchange in the
    background when the redirect lands."""
    app = request.app.state
    try:
        url = app.auth.authorize_url()
    except AuthError as e:
        raise HTTPException(status_code=400, detail=str(e))

    async def flow() -> None:
        try:
            code = await wait_for_code(
                app.settings.data_dir, app.settings.autotrader_callback_port
            )
            await app.auth.exchange_code(code)
            await app.db.audit("user", "auth", {"event": "oauth_connected"})
        except asyncio.TimeoutError:
            log.warning("oauth_flow_timed_out")
        except Exception as e:  # noqa: BLE001
            log.error("oauth_flow_failed", error=str(e))

    # only one flow at a time
    existing = getattr(app, "oauth_flow_task", None)
    if existing is None or existing.done():
        app.oauth_flow_task = asyncio.create_task(flow(), name="oauth-flow")
        try:
            webbrowser.open(url)
        except Exception:  # headless/remote — the UI opens the URL itself
            pass
    return {"authorize_url": url}


# Cap chart payloads to keep transfer/parse/render cost bounded regardless of
# how wide a date range or how fine a timeframe is requested — a chart panel
# is only ever a few thousand pixels wide, so more raw points than that is
# wasted bandwidth, not extra fidelity.
MAX_CANDLE_POINTS = 2000


def _downsample_ohlc(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    """Bucket consecutive rows so the result has ~max_points rows while
    preserving true OHLC semantics per bucket (open of first row, close of
    last row, true high/low across the bucket) — plain decimation would drop
    points and understate volatility."""
    n = len(df)
    if n <= max_points:
        return df
    bucket_size = -(-n // max_points)  # ceil division
    bucket_ids = pd.Series(range(n), index=df.index) // bucket_size
    out = df.groupby(bucket_ids).agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"),
    )
    # groupby-by-position drops the DatetimeIndex — recover each bucket's
    # opening timestamp (the index value of its first row) for the x-axis.
    out.index = df.groupby(bucket_ids).apply(lambda g: g.index[0])
    return out


@router.get("/candles")
async def candles(request: Request, symbol: str, tf: str = "1d",
                  start: int | None = None, end: int | None = None) -> dict:
    """OHLCV for the chart UI. start/end epoch-ms; defaults to a sensible
    recent window per timeframe."""
    import time as _t
    app = request.app.state
    now_ms = int(_t.time() * 1000)
    if end is None:
        end = now_ms
    if start is None:
        start = end - (5 * 366 * 86_400_000 if tf == "1d" else 30 * 86_400_000)
    try:
        df = await app.marketdata.get_candles(symbol.upper(), tf, start, end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except NotAuthenticatedError:
        raise HTTPException(status_code=424, detail="Not connected to Schwab")
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    df = _downsample_ohlc(df, MAX_CANDLE_POINTS)

    # vectorized instead of df.iterrows() (row-by-row Python objects, far
    # slower on large frames) — pull columns to numpy/lists once and zip.
    # NB: don't do `.astype("int64") // 1_000_000_000` — the index's
    # datetime64 unit varies by pandas version (ns historically, us as of
    # pandas 3.x), so a fixed nanosecond divisor silently truncates seconds
    # on newer pandas. Casting through datetime64[s] is unit-independent.
    times = df.index.tz_convert("UTC").tz_localize(None).astype("datetime64[s]").astype("int64").tolist()
    candle_list = [
        {"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": int(v)}
        for t, o, h, lo, c, v in zip(
            times, df["open"], df["high"], df["low"], df["close"], df["volume"]
        )
    ]
    return {
        "symbol": symbol.upper(),
        "tf": tf,
        "candles": candle_list,
    }


@router.get("/data/coverage")
async def data_coverage(request: Request) -> dict:
    app = request.app.state
    store = app.marketdata.store
    out = []
    for sym in await store.symbols():
        entry: dict = {"symbol": sym}
        for tf in COVERAGE_TFS:
            cov = await store.coverage(sym, tf)
            entry[tf] = (
                {"first_ts": cov[0], "last_ts": cov[1], "bars": cov[2]} if cov else None
            )
        out.append(entry)
    return {"coverage": out, "recorded_bars_this_session": app.recorder.bars_recorded}


@router.post("/data/backfill")
async def backfill_symbol(request: Request, body: dict) -> dict:
    """Force a full one-time backfill for a symbol: 20y daily + all available
    1m history (~6 weeks) + all available native 5/15/30m history (~8.5 months).
    Idempotent (upserts); safe to re-run to top up."""
    symbol = str(body.get("symbol", "")).strip().upper()
    if not symbol or len(symbol) > 10 or not symbol.replace(".", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid symbol")
    app = request.app.state
    bf = app.marketdata.backfill
    try:
        daily = await bf.backfill_daily_full(symbol)
        minute = await bf.backfill_minute_max(symbol)
        intraday = await bf.backfill_intraday_all(symbol)
    except NotAuthenticatedError:
        raise HTTPException(status_code=424, detail="Not connected to Schwab")
    except SchwabAPIError as e:
        # unknown/invalid tickers surface as Schwab 400/404 — pass the gist through
        raise HTTPException(status_code=502, detail=str(e))
    intraday_total = sum(intraday.values())
    if daily == 0 and minute == 0 and intraday_total == 0:
        raise HTTPException(status_code=404, detail=f"No data returned for '{symbol}' — check the ticker")
    store = app.marketdata.store
    coverage: dict = {}
    for tf in COVERAGE_TFS:
        cov = await store.coverage(symbol, tf)
        coverage[tf] = (
            {"first_ts": cov[0], "last_ts": cov[1], "bars": cov[2]} if cov else None
        )
    await app.db.audit(
        "user",
        "backfill",
        {"symbol": symbol, "daily": daily, "minute": minute, "intraday": intraday},
    )
    return {
        "symbol": symbol,
        "daily_bars_fetched": daily,
        "minute_bars_fetched": minute,
        "intraday_bars_fetched": intraday,
        "intraday_bars_total": intraday_total,
        "coverage": coverage,
    }


@router.get("/watchlist")
async def get_watchlist(request: Request) -> dict:
    return {"symbols": await request.app.state.recorder.get_watchlist()}


@router.put("/watchlist")
async def put_watchlist(request: Request, body: dict) -> dict:
    symbols = body.get("symbols", [])
    if not isinstance(symbols, list) or len(symbols) > 50:
        raise HTTPException(status_code=400, detail="symbols must be a list (max 50)")
    saved = await request.app.state.recorder.set_watchlist(symbols)

    # kick off history bootstrap for new symbols in the background (log failures)
    import asyncio as _aio

    async def _bootstrap(sym: str) -> None:
        try:
            await request.app.state.marketdata.backfill.ensure_symbol(sym)
        except Exception as e:  # noqa: BLE001
            log.warning("watchlist_bootstrap_failed", symbol=sym, error=str(e))

    for s in saved:
        _aio.create_task(_bootstrap(s))
    return {"symbols": saved}


@router.get("/accounts")
async def accounts(request: Request) -> dict:
    app = request.app.state
    try:
        raw = await app.schwab.get_accounts()
    except NotAuthenticatedError:
        raise HTTPException(status_code=424, detail="Not connected to Schwab")
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"accounts": summarize_accounts(raw)}
