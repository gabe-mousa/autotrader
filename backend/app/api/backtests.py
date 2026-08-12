"""Backtest run API: launch (in the process pool), persist, list, fetch."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import yaml
from fastapi import APIRouter, HTTPException, Request

from ..auth.manager import NotAuthenticatedError
from ..backtest.engine import BacktestConfig, run_backtest_serialized
from ..backtest.metrics import compute_metrics
from ..backtest.sweep import apply_combo, generate_combos, parse_grid
from ..logging import get_logger
from ..schwab.client import SchwabAPIError
from ..strategy.hashing import content_hash
from ..strategy.lint import is_warning, lint
from ..strategy.schema import StrategyDocument
from ..strategy.timeframe import BAR_MS, estimate_warmup_bars

_SUMMARY_KEYS = ("total_return_pct", "sharpe", "max_drawdown_pct", "win_rate_pct", "trade_count")

router = APIRouter(prefix="/api")
log = get_logger("api-backtests")


#: Fields the ENGINE puts on an options trade row that the fixed
#: `backtest_trades` columns cannot hold. Stored as one JSON blob rather than a
#: dozen sparse columns that would be NULL on every equity row.
_OPTION_TRADE_FIELDS = (
    "instrument", "contract", "contract_label", "right", "strike", "expiry",
    "multiplier", "dte_at_entry", "dte_at_exit", "underlying_entry_px",
    "underlying_exit_px", "entry_iv", "entry_delta", "premium_paid", "partial",
)


def _trade_detail(trade: dict) -> str | None:
    """The options detail for one trade row, or None for a shares trade."""
    detail = {k: trade[k] for k in _OPTION_TRADE_FIELDS if k in trade}
    return json.dumps(detail) if detail.get("instrument") == "option" else None


def _expand_trade(row: dict) -> dict:
    """Merge `detail_json` back onto a trade row, so the API returns exactly
    what the engine produced. Rows written before options existed have no
    detail and pass through untouched."""
    raw = row.pop("detail_json", None)
    if not raw:
        return row
    try:
        row.update(json.loads(raw))
    except (ValueError, TypeError):
        pass
    return row


async def _load_doc_by_hash(app, strategy_version_hash: str) -> StrategyDocument | None:
    """Look up the frozen strategy snapshot a backtest ran against, via the
    strategy_versions index (content_hash -> file_path). Used to surface
    symbol/timeframe on the backtest detail response (feature 1) — the
    backtests table itself only stores the config, not the strategy doc."""
    row = await app.db.fetch_one(
        "SELECT file_path FROM strategy_versions WHERE content_hash=? LIMIT 1",
        (strategy_version_hash,))
    if not row:
        return None
    try:
        raw = yaml.safe_load(open(row["file_path"]).read())
        return StrategyDocument.model_validate(raw)
    except (OSError, yaml.YAMLError, Exception):  # noqa: BLE001
        return None


async def _strategy_ref(app, strategy_id: str | None, strategy_version_hash: str) -> dict | None:
    """slug/name of the strategy a backtest ran against, for the detail page's
    link back to the editor.

    Prefers backtests.strategy_id — recorded at run time and unambiguous. Only
    falls back to the content_hash -> strategy_versions index for rows written
    before that column existed, where the answer can genuinely be ambiguous:
    content_hash excludes meta, so a copied or renamed strategy shares its
    original's hash and any of them is an equally valid match."""
    if strategy_id:
        row = await app.db.fetch_one(
            "SELECT slug, name FROM strategies WHERE id=?", (strategy_id,))
        if row:
            return row
    return await app.db.fetch_one(
        "SELECT s.slug, s.name FROM strategy_versions sv "
        "JOIN strategies s ON s.id = sv.strategy_id "
        "WHERE sv.content_hash=? LIMIT 1",
        (strategy_version_hash,))


async def _fetch_candles_with_warmup(app, doc: StrategyDocument, cfg: BacktestConfig) -> dict:
    """Fetch candles (async, may auto-backfill) for every symbol in the
    strategy's universe, extended with EXTRA history before cfg.start so
    indicators are already warmed up by the first evaluated bar — without
    this, a short date range (e.g. a 1-week backtest with an RSI(14)) would
    have a NaN indicator for nearly the whole window, silently suppressing
    almost every signal. run_backtest() itself restricts actual
    evaluation/trades/equity output to [cfg.start, cfg.end]; the pre-start
    rows are warm-up only. Shared by the single-run and sweep endpoints —
    the ONE code path that fetches candles for a backtest.

    Raises HTTPException on auth/API failure or empty candle data, same as
    the original inline implementation in run_backtest_api."""
    start_ms = int(time.mktime(time.strptime(cfg.start, "%Y-%m-%d")) * 1000)
    end_ms = int(time.mktime(time.strptime(cfg.end, "%Y-%m-%d")) * 1000) + 86_400_000
    warmup_ms = estimate_warmup_bars(doc) * BAR_MS[doc.timeframe.bar]
    fetch_start_ms = start_ms - warmup_ms
    candles = {}
    # the market-filter reference symbol (plan/15 §C1) is not part of the traded
    # universe but its bars are required — the engine FAILS CLOSED without them,
    # so a missing fetch here would silently block every entry
    wanted = list(doc.universe.symbols)
    if doc.market_filter is not None and doc.market_filter.symbol not in wanted:
        wanted.append(doc.market_filter.symbol)

    # An OPTIONS backtest additionally needs its volatility anchor ($VIX/$VXN/
    # $RVX). That series IS the observed 30-day implied volatility of the
    # underlying's index and is what keeps the pricer from falling back to
    # realized vol — which, without the variance risk premium, prices options
    # too cheap and manufactures profit (plan/16 D6). Fetched like any other
    # symbol; if it is unavailable the engine falls back and SAYS SO in the
    # result's vol_source_mix rather than pretending.
    from ..options.volsurface import anchor_for
    anchors: set[str] = set()
    if getattr(doc, "expression", None) is not None and \
            doc.expression.instrument == "options":
        for sym in doc.universe.symbols:
            a = anchor_for(sym)
            if a:
                anchors.add(a)
        wanted.extend(a for a in sorted(anchors) if a not in wanted)
    try:
        for sym in wanted:
            df = await app.marketdata.get_candles(sym, doc.timeframe.bar, fetch_start_ms, end_ms)
            if not df.empty:
                candles[sym] = df
    except NotAuthenticatedError:
        raise HTTPException(status_code=424, detail="Not connected to Schwab (needed to backfill)")
    except SchwabAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not candles:
        raise HTTPException(status_code=404, detail="no candle data for the requested range")
    return candles


def _ms(iso_date: str, *, end: bool = False) -> int:
    """"YYYY-MM-DD" -> epoch ms, matching `_fetch_candles_with_warmup`'s
    convention exactly (local midnight; +1 day for an inclusive end)."""
    ms = int(time.mktime(time.strptime(iso_date, "%Y-%m-%d")) * 1000)
    return ms + 86_400_000 if end else ms


async def _load_option_ticks(app, doc: StrategyDocument, start_ms: int,
                             end_ms: int) -> list[dict]:
    """Recorded option quotes covering the backtest window.

    Loaded HERE rather than in the engine because backtests execute in a
    ProcessPoolExecutor: the workers have no event loop and no DB connection,
    so everything has to cross the pickle boundary as plain data.

    Returns [] for a shares strategy, for a window predating the recorder, or
    when nothing was captured — all of which the engine handles by modelling,
    and by saying so in the result's assumptions."""
    if getattr(doc, "expression", None) is None or \
            doc.expression.instrument != "options":
        return []
    store = getattr(app, "option_store", None)
    if store is None:
        return []
    rows: list[dict] = []
    for sym in doc.universe.symbols:
        try:
            rows.extend(await store.read_ticks_for_underlying(sym, start_ms, end_ms))
        except Exception as e:  # noqa: BLE001 — recorded quotes are an
            # ENHANCEMENT; failing to load them must degrade to modelled
            # pricing, never fail the backtest.
            log.warning("option_tick_load_failed", underlying=sym, error=str(e))
    return rows


async def _run_and_persist_backtest(
    app, doc: StrategyDocument, cfg: BacktestConfig, candles: dict,
    slug: str, sweep_id: str | None = None, sweep_combo: dict | None = None,
    option_ticks: list[dict] | None = None,
) -> dict:
    """Submit one backtest to the process pool and persist the result — the
    ONE code path both POST /backtests and POST /backtests/sweep (feature 3)
    call. `sweep_combo`, if given, is stashed inside config_json under
    `_sweep_combo` (see GET /backtests/sweep/{sweep_id}'s docstring for why:
    it's the simplest way to recover each backtest's lever values without a
    schema migration, at the cost of `config_json` carrying one extra key
    that isn't part of BacktestConfig itself — get_backtest/list_backtests
    both re-parse config_json as plain JSON, so this extra key is silently
    along for the ride and never breaks either existing response shape)."""
    run_id = str(uuid.uuid4())
    h = content_hash(doc)
    config_json = cfg.model_dump(mode="json")
    if sweep_combo is not None:
        config_json["_sweep_combo"] = sweep_combo
    await app.db.execute(
        "INSERT INTO backtests (id, strategy_version_hash, sweep_id, config_json, status, "
        "started_at, strategy_id) VALUES (?,?,?,?,?,?,?)",
        (run_id, h, sweep_id, json.dumps(config_json), "running", int(time.time() * 1000),
         doc.meta.id),
    )
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            app.pool, run_backtest_serialized,
            doc.model_dump(mode="json", by_alias=True), cfg.model_dump(mode="json"),
            candles, option_ticks or [])
    except Exception as e:  # noqa: BLE001
        await app.db.execute(
            "UPDATE backtests SET status='error', metrics_json=? WHERE id=?",
            (json.dumps({"error": str(e)}), run_id))
        log.error("backtest_failed", run_id=run_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"backtest failed: {e}")

    conn = app.db.conn
    await conn.executemany(
        "INSERT INTO backtest_equity (backtest_id, ts, equity, drawdown) VALUES (?,?,?,?)",
        _equity_rows(run_id, result.equity_points),
    )
    await conn.executemany(
        """INSERT INTO backtest_trades (backtest_id, symbol, side, entry_ts, entry_px,
           exit_ts, exit_px, qty, pnl, pnl_pct, mae, mfe, bars_held, exit_reason,
           detail_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(run_id, t["symbol"], t["side"], t["entry_ts"], t["entry_px"], t["exit_ts"],
          t["exit_px"], t["qty"], t["pnl"], t["pnl_pct"], t["mae_pct"], t["mfe_pct"],
          t["bars_held"], t["exit_reason"], _trade_detail(t)) for t in result.trades],
    )
    await app.db.execute(
        "UPDATE backtests SET status='done', finished_at=?, metrics_json=? WHERE id=?",
        (int(time.time() * 1000),
         json.dumps({"metrics": result.metrics, "assumptions": result.assumptions,
                     "open_positions": result.open_positions}), run_id),
    )
    await app.db.audit("user", "backtest", {"run_id": run_id, "slug": slug, "hash": h[:8]})
    return {"id": run_id, "strategy_version_hash": h, "metrics": result.metrics,
            "assumptions": result.assumptions, "trade_count": len(result.trades)}


@router.post("/backtests")
async def run_backtest_api(request: Request, body: dict) -> dict:
    app = request.app.state
    slug = body.get("slug")
    if not slug:
        raise HTTPException(status_code=400, detail="slug required")
    try:
        doc = app.strategy_files.load(slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"strategy {slug!r} not found")
    errors = [p for p in lint(doc) if not is_warning(p)]
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    try:
        cfg = BacktestConfig.model_validate(body.get("config", {}))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))

    candles = await _fetch_candles_with_warmup(app, doc, cfg)
    ticks = await _load_option_ticks(app, doc, _ms(cfg.start), _ms(cfg.end, end=True))
    return await _run_and_persist_backtest(app, doc, cfg, candles, slug,
                                           option_ticks=ticks)


# NOTE: registered BEFORE /backtests/{run_id} for the same reason as
# /backtests/compare above — "sweep" would otherwise be swallowed as a
# {run_id} path param on GET /backtests/sweep/{sweep_id}.
@router.post("/backtests/sweep")
async def run_sweep_api(request: Request, body: dict) -> dict:
    app = request.app.state
    slug = body.get("slug")
    if not slug:
        raise HTTPException(status_code=400, detail="slug required")
    try:
        base_doc = app.strategy_files.load(slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"strategy {slug!r} not found")
    try:
        cfg = BacktestConfig.model_validate(body.get("config", {}))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))
    try:
        levers = parse_grid(body.get("grid", {}))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    base_data = base_doc.model_dump(mode="json", by_alias=True)
    combos = generate_combos(levers)

    # validate + lint every combination FIRST (same lint call POST /backtests
    # uses — never skipped), so one bad combo doesn't blow up the whole sweep.
    valid: list[tuple[dict, StrategyDocument]] = []
    skipped: list[dict] = []
    for combo in combos:
        combo_doc_data = apply_combo(base_data, levers, combo)
        try:
            candidate = StrategyDocument.model_validate(combo_doc_data)
        except Exception as e:  # noqa: BLE001
            skipped.append({"combo": combo, "reason": f"schema: {e}"})
            continue
        errors = [p for p in lint(candidate) if not is_warning(p)]
        if errors:
            skipped.append({"combo": combo, "reason": "; ".join(errors)})
            continue
        valid.append((combo, candidate))

    sweep_id = str(uuid.uuid4())
    await app.db.execute(
        "INSERT INTO sweeps (id, strategy_version_hash, param_grid_json, created_at) "
        "VALUES (?,?,?,?)",
        (sweep_id, content_hash(base_doc), json.dumps(body.get("grid", {})),
         int(time.time() * 1000)),
    )

    # Fetch candles once per DISTINCT universe+timeframe rather than once per
    # combo. Sweeping `timeframe.bar` means combos no longer necessarily share
    # a bar size, and a 5m combo can't run off 1h candles — but combos that DO
    # share a bar still share one fetch. Within each group, reuse the single
    # candidate needing the MOST warm-up history; every other combo in that
    # group needs no more than that.
    groups: dict[tuple, list[tuple[int, dict, StrategyDocument]]] = {}
    for i, (combo, candidate) in enumerate(valid):
        key = (candidate.timeframe.bar, tuple(candidate.universe.symbols))
        groups.setdefault(key, []).append((i, combo, candidate))

    indexed: list[tuple[int, dict]] = []
    for key, members in groups.items():
        warmup_doc = max((d for _, _, d in members), key=estimate_warmup_bars)
        try:
            candles = await _fetch_candles_with_warmup(app, warmup_doc, cfg)
        except HTTPException as e:
            # One bar size having no data for the window (e.g. 1m history
            # doesn't reach back that far) must not kill the whole sweep —
            # skip that group the same way a failed lint skips a combo.
            for _, combo, _ in members:
                skipped.append({"combo": combo, "reason": f"candles ({key[0]}): {e.detail}"})
            continue
        # Loaded ONCE per group, not per combo: a sweep can be hundreds of
        # runs over the same window, and the tick set is identical for all of
        # them.
        group_ticks = await _load_option_ticks(app, warmup_doc, _ms(cfg.start),
                                               _ms(cfg.end, end=True))
        for i, combo, candidate in members:
            r = await _run_and_persist_backtest(
                app, candidate, cfg, candles, slug, sweep_id=sweep_id, sweep_combo=combo,
                option_ticks=group_ticks)
            indexed.append((i, {
                "combo": combo, "backtest_id": r["id"],
                "metrics_summary": {k: r["metrics"].get(k) for k in _SUMMARY_KEYS},
            }))
    # grouping reorders the runs; report them back in combo order.
    results = [r for _, r in sorted(indexed, key=lambda x: x[0])]

    return {"sweep_id": sweep_id, "results": results, "skipped": skipped}


@router.get("/backtests/sweep/{sweep_id}")
async def get_sweep(request: Request, sweep_id: str) -> dict:
    app = request.app.state
    sweep_row = await app.db.fetch_one(
        "SELECT strategy_version_hash, param_grid_json, created_at FROM sweeps WHERE id=?",
        (sweep_id,))
    if not sweep_row:
        raise HTTPException(status_code=404, detail="sweep not found")
    rows = await app.db.fetch_all(
        "SELECT id, strategy_id, config_json, metrics_json FROM backtests WHERE sweep_id=? "
        "ORDER BY started_at", (sweep_id,))
    results = []
    for r in rows:
        config = json.loads(r["config_json"])
        combo = config.pop("_sweep_combo", {})
        metrics = json.loads(r["metrics_json"])["metrics"] if r["metrics_json"] else {}
        results.append({
            "combo": combo, "backtest_id": r["id"],
            "metrics_summary": {k: metrics.get(k) for k in _SUMMARY_KEYS},
        })
    # Which strategy this sweep was run against, so the detail page can link to
    # its editor and show its document. sweeps.strategy_version_hash is the
    # BASE strategy's hash (combos are never frozen), but that hash is
    # ambiguous across copies/renames — prefer any member's strategy_id, which
    # is recorded per run. Both are absent on sweeps predating that column.
    member_strategy_id = next((r["strategy_id"] for r in rows if r["strategy_id"]), None)
    strategy = await _strategy_ref(app, member_strategy_id, sweep_row["strategy_version_hash"])
    return {"sweep_id": sweep_id, "grid": json.loads(sweep_row["param_grid_json"]),
            "strategy_slug": strategy["slug"] if strategy else None,
            "strategy_name": strategy["name"] if strategy else None,
            "results": results}


def _equity_rows(run_id: str, points):
    # Equity is stored UNROUNDED, deliberately. Sortino divides by the standard
    # deviation of only the NEGATIVE per-bar returns, and a backtest's equity is
    # flat on most bars (no open position -> cash unchanged). Two equity values
    # that differ by a fraction of a microdollar are distinct floats to the
    # engine — contributing a ~1e-12 negative return — but become exactly equal
    # once rounded, so that return turns into a hard zero and drops out of the
    # downside set. With only a handful of negative returns in a run, gaining or
    # losing one near-zero member moves the downside std sharply: measured
    # sortino 0.0869 (engine) vs 0.0952 (recomputed from equity rounded to 6dp)
    # on the same 40-bar backtest, with 38/40 rows differing only past the 6th
    # decimal. Sharpe is unaffected (total std is dominated by the large moves),
    # which is why this stayed invisible. Anything recomputing metrics from
    # stored equity — see the what-if endpoint — must read back exactly what the
    # engine computed on, or its numbers silently disagree with the run's own.
    peak = float("-inf")
    rows = []
    for ts, eq in points:
        peak = max(peak, eq)
        dd = eq / peak - 1 if peak > 0 else 0.0
        rows.append((run_id, int(ts.timestamp() * 1000), eq, round(dd, 6)))
    return rows


@router.get("/backtests")
async def list_backtests(request: Request, slug: str | None = None) -> dict:
    app = request.app.state
    rows = await app.db.fetch_all(
        "SELECT id, strategy_version_hash, strategy_id, sweep_id, config_json, status, "
        # The UI collapses each sweep to ONE row, so a 200-row budget could be
        # spent by two or three sweeps and hide every standalone run behind
        # them. Sweep members are cheap here (summary metrics only).
        "started_at, finished_at, metrics_json FROM backtests ORDER BY started_at DESC "
        "LIMIT 2000")
    strategies = await app.db.fetch_all("SELECT id, slug, name FROM strategies")
    by_id = {r["id"]: r for r in strategies}
    # Fallback for rows written before backtests.strategy_id existed. This map
    # is lossy ON PURPOSE-ish: content_hash excludes meta, so copies/renames
    # share a hash and collapse to one arbitrary name here. That ambiguity is
    # exactly why strategy_id is preferred — see _strategy_ref.
    by_hash = {r["content_hash"]: r for r in await app.db.fetch_all(
        "SELECT sv.content_hash, s.slug, s.name FROM strategy_versions sv "
        "JOIN strategies s ON s.id = sv.strategy_id")}
    out = []
    for r in rows:
        metrics = json.loads(r["metrics_json"])["metrics"] if r["metrics_json"] and \
            r["status"] == "done" else {}
        strat = by_id.get(r["strategy_id"]) if r["strategy_id"] else None
        strat = strat or by_hash.get(r["strategy_version_hash"])
        # `slug` was accepted but never applied before strategy_id existed —
        # there was no reliable way to attribute a run to one strategy.
        if slug and (strat is None or strat["slug"] != slug):
            continue
        cfg = json.loads(r["config_json"])
        out.append({
            "id": r["id"], "hash": r["strategy_version_hash"][:8],
            # sweep members are collapsed under one expandable row in the UI;
            # `sweep_combo` is the lever values that produced this run (stashed
            # in config_json by _run_and_persist_backtest).
            "sweep_id": r["sweep_id"],
            "sweep_combo": cfg.get("_sweep_combo"),
            "strategy_name": strat["name"] if strat else None,
            "strategy_slug": strat["slug"] if strat else None,
            "status": r["status"], "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "config": cfg,
            "summary": {k: metrics.get(k) for k in _SUMMARY_KEYS} if metrics else None,
        })
    return {"backtests": out}


MAX_EQUITY_POINTS = 3000


def _downsample_equity(rows: list, max_points: int) -> list:
    """Bucket consecutive rows down to ~max_points, keeping both the min and
    max equity per bucket (min/max decimation) so equity peaks and drawdown
    troughs survive — plain stride subsampling would smooth them away. A
    backtest run over a 1-minute-bar strategy across months can otherwise
    produce 100k+ rows for a chart panel a few thousand pixels wide."""
    n = len(rows)
    if n <= max_points:
        return rows
    bucket_size = -(-n // max(1, max_points // 2))  # ceil div, 2 pts/bucket
    out = []
    for i in range(0, n, bucket_size):
        chunk = rows[i:i + bucket_size]
        lo = min(chunk, key=lambda r: r["equity"])
        hi = max(chunk, key=lambda r: r["equity"])
        picked = {id(lo): lo, id(hi): hi}.values()
        out.extend(sorted(picked, key=lambda r: r["ts"]))
    return out


async def _backtest_detail(app, run_id: str) -> dict | None:
    """Shared by GET /backtests/{run_id} and GET /backtests/compare — the ONE
    code path that assembles a backtest's full detail payload. Returns None
    if run_id doesn't exist (caller decides 404 vs. skip)."""
    row = await app.db.fetch_one("SELECT * FROM backtests WHERE id=?", (run_id,))
    if not row:
        return None
    payload = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
    equity = _downsample_equity(await app.db.fetch_all(
        "SELECT ts, equity, drawdown FROM backtest_equity WHERE backtest_id=? ORDER BY ts",
        (run_id,)), MAX_EQUITY_POINTS)
    trades = [_expand_trade(dict(r)) for r in await app.db.fetch_all(
        "SELECT * FROM backtest_trades WHERE backtest_id=? ORDER BY entry_ts", (run_id,))]
    doc = await _load_doc_by_hash(app, row["strategy_version_hash"])
    strategy = await _strategy_ref(app, row["strategy_id"], row["strategy_version_hash"])
    # Every symbol the backtest could have traded: the strategy's universe,
    # unioned with any symbol that actually appears in a trade or open
    # position (a sweep-generated combo has no frozen strategy_versions row,
    # so `doc` is None there and the traded symbols are all we have).
    traded = {t["symbol"] for t in trades}
    traded |= {p["symbol"] for p in payload.get("open_positions", [])}
    universe = list(doc.universe.symbols) if doc else []
    symbols = universe + sorted(traded - set(universe))
    return {
        "id": run_id,
        "strategy_version_hash": row["strategy_version_hash"],
        "status": row["status"],
        # slug/name of the strategy this ran against, so the UI can link
        # straight to its editor. None when the version index has no row
        # for this hash (sweep combos, hand-deleted files).
        "strategy_slug": strategy["slug"] if strategy else None,
        "strategy_name": strategy["name"] if strategy else None,
        # `symbol` (first universe symbol) is kept for existing callers;
        # `symbols` is the full selectable list for the price chart.
        "symbol": symbols[0] if symbols else None,
        "symbols": symbols,
        "bar": doc.timeframe.bar if doc else None,
        "config": json.loads(row["config_json"]),
        "metrics": payload.get("metrics", payload),
        "assumptions": payload.get("assumptions", {}),
        "equity": [{"time": r["ts"] // 1000, "equity": r["equity"],
                    "drawdown": r["drawdown"]} for r in equity],
        # entry_ts/exit_ts are stored as epoch MS (matching every other
        # timestamp column); convert to epoch SECONDS here, same as the
        # equity points above — the frontend multiplies by 1000 for Date().
        "trades": [
            {**dict(r), "entry_ts": r["entry_ts"] // 1000, "exit_ts": r["exit_ts"] // 1000}
            for r in trades
        ],
        # never-closed positions when the window ended — same epoch-ms-in-DB
        # -> epoch-seconds-over-the-wire convention as trades/equity above.
        "open_positions": [
            {**p, "entry_ts": p["entry_ts"] // 1000,
             "as_of_ts": p["as_of_ts"] // 1000 if p["as_of_ts"] is not None else None}
            for p in payload.get("open_positions", [])
        ],
    }


# NOTE: registered BEFORE /backtests/{run_id} — FastAPI matches routes in
# declaration order, and "/backtests/compare"/"/backtests/sweep*" would
# otherwise be swallowed by the {run_id} path param.
@router.get("/backtests/compare")
async def compare_backtests(request: Request, ids: str) -> dict:
    app = request.app.state
    id_list = [i for i in ids.split(",") if i]
    if len(id_list) == 0:
        raise HTTPException(status_code=400, detail="ids required")
    if len(id_list) > 4:
        raise HTTPException(status_code=400, detail="compare supports at most 4 backtests")
    out = []
    for run_id in id_list:
        detail = await _backtest_detail(app, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"backtest {run_id!r} not found")
        out.append(detail)
    return {"backtests": out}


@router.get("/backtests/{run_id}")
async def get_backtest(request: Request, run_id: str) -> dict:
    app = request.app.state
    detail = await _backtest_detail(app, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="backtest not found")
    return detail


# ---- what-if: recompute with some trades excluded ---------------------------
# Read-only and non-destructive by design: nothing is written, so the stored
# result of a run is always the real one. The UI keeps the excluded set in
# component state, so a reload restores the full picture.


def _bar_seconds_for(equity_index, doc: StrategyDocument | None) -> float:
    """Bar duration for annualization. Prefer the strategy's own timeframe;
    fall back to the median gap in the equity series for sweep combos, whose
    frozen strategy snapshot may be gone."""
    if doc is not None:
        return BAR_MS[doc.timeframe.bar] / 1000
    if len(equity_index) < 2:
        return 86_400.0
    import pandas as pd

    return float(pd.Series(equity_index).diff().dropna().dt.total_seconds().median())


def _covered_bars(equity_index, trades: list[dict]) -> set[int]:
    """Epoch-ms of every bar during which any of `trades` was held, as
    [entry_ts, exit_ts). Overlapping positions collapse into one set, matching
    the engine's `if positions:` check (concurrent holds are one bar, not N)."""
    covered: set[int] = set()
    stamps = [int(ts.timestamp() * 1000) for ts in equity_index]
    for t in trades:
        entry_ms, exit_ms = t["entry_ts"], t["exit_ts"]
        covered.update(ms for ms in stamps if entry_ms <= ms < exit_ms)
    return covered


def _adjusted_bars_in_market(equity_index, kept: list[dict], removed: list[dict],
                             stored_exposure_pct: float | None, total_bars: int) -> int:
    """Bars-in-market for the what-if run.

    Deliberately anchored to the ENGINE's own exposure rather than recounted
    from scratch: the engine's `bars_in_market` depends on exactly when within
    a bar it opens/closes positions (signal bar vs. fill bar, pending orders
    executing at the next open), and reproducing that here would duplicate
    engine internals and silently drift the moment they change. Recounting
    from trade timestamps measured 20% vs. the engine's 25% on a real run.

    So: take the engine's number as the baseline and subtract only the bars the
    EXCLUDED trades held alone (bars also covered by a kept trade were in the
    market either way). Excluding nothing is then exactly the stored value."""
    if stored_exposure_pct is None:
        return len(_covered_bars(equity_index, kept))
    base = round(stored_exposure_pct / 100 * total_bars)
    if not removed:
        return base
    freed = _covered_bars(equity_index, removed) - _covered_bars(equity_index, kept)
    return max(0, base - len(freed))


@router.post("/backtests/{run_id}/what-if")
async def backtest_what_if(request: Request, run_id: str, body: dict) -> dict:
    """Recompute a finished backtest's metrics as if `exclude_trade_ids` had
    never been taken — for sanity-checking how much one outlier trade is
    carrying a result.

    The equity curve is adjusted by removing each excluded trade's realized PnL
    from every point at or after its exit. This is a FIRST-ORDER what-if: it
    does not re-simulate, so it can't know that the freed capital might have
    funded a different trade, or that position sizing on later trades would
    have differed. Every other trade is left exactly as it was. That's why the
    response carries `approximate: true` and the UI says so — the honest use is
    "how much does this one trade matter", not "what would really have
    happened".
    """
    import pandas as pd

    app = request.app.state
    row = await app.db.fetch_one("SELECT * FROM backtests WHERE id=?", (run_id,))
    if not row:
        raise HTTPException(status_code=404, detail="backtest not found")

    raw_ids = body.get("exclude_trade_ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="exclude_trade_ids must be a list")
    try:
        exclude = {int(i) for i in raw_ids}
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="exclude_trade_ids must be integers")

    all_trades = [_expand_trade(dict(r)) for r in await app.db.fetch_all(
        "SELECT * FROM backtest_trades WHERE backtest_id=? ORDER BY entry_ts", (run_id,))]
    known = {t["id"] for t in all_trades}
    unknown = exclude - known
    if unknown:
        raise HTTPException(status_code=404,
                            detail=f"trades not in this backtest: {sorted(unknown)}")
    if len(exclude) == len(all_trades) and all_trades:
        raise HTTPException(status_code=400, detail="cannot exclude every trade")

    kept = [t for t in all_trades if t["id"] not in exclude]
    removed = [t for t in all_trades if t["id"] in exclude]

    # full-resolution equity (NOT the downsampled detail payload — metrics are
    # computed off every bar, and downsampling would quietly change sharpe/DD)
    eq_rows = await app.db.fetch_all(
        "SELECT ts, equity FROM backtest_equity WHERE backtest_id=? ORDER BY ts", (run_id,))
    if len(eq_rows) < 2:
        raise HTTPException(status_code=409, detail="backtest has no equity curve to recompute")
    base_equity = pd.Series({pd.Timestamp(r["ts"], unit="ms", tz="UTC"): float(r["equity"])
                             for r in eq_rows}).sort_index()
    equity = base_equity.copy()
    for t in removed:
        exit_ts = pd.Timestamp(t["exit_ts"], unit="ms", tz="UTC")
        equity.loc[equity.index >= exit_ts] -= float(t["pnl"])

    doc = await _load_doc_by_hash(app, row["strategy_version_hash"])
    stored = (json.loads(row["metrics_json"]) if row["metrics_json"] else {})
    stored_metrics = stored.get("metrics", stored)
    bar_seconds = _bar_seconds_for(equity.index, doc)

    def metrics_for(trades: list[dict], eq, removed_trades: list[dict]) -> dict:
        return compute_metrics(
            eq,
            [{"pnl": t["pnl"], "bars_held": t["bars_held"]} for t in trades],
            _adjusted_bars_in_market(eq.index, trades, removed_trades,
                                     stored_metrics.get("exposure_pct"), len(eq)),
            len(eq),
            bar_seconds,
        )

    metrics = metrics_for(kept, equity, removed)
    # Recompute the untouched run through the SAME path, so the UI can show a
    # like-for-like delta. This is not redundant with the stored metrics: a run
    # whose persisted metrics disagree with its own persisted equity (older
    # rows exist where they were computed at different bar resolutions) would
    # otherwise make excluding one trade look like it moved sharpe enormously,
    # when nearly all of that gap is the stale baseline. Comparing against this
    # keeps the "how much is this trade worth" answer honest.
    baseline = metrics_for(all_trades, base_equity, [])
    drawdown = (equity / equity.cummax() - 1) * 100
    points = [{"ts": int(ts.timestamp() * 1000), "equity": round(v, 4),
               "drawdown": round(float(drawdown.loc[ts]), 4)}
              for ts, v in equity.items()]
    return {
        "id": run_id,
        "approximate": True,
        "excluded_trade_ids": sorted(exclude),
        "excluded_pnl": round(sum(float(t["pnl"]) for t in removed), 2),
        "metrics": metrics,
        # the same run with nothing excluded, computed the same way — compare
        # against THIS, not the stored metrics (see above)
        "baseline_metrics": baseline,
        "equity": [{"time": p["ts"] // 1000, "equity": p["equity"],
                    "drawdown": p["drawdown"]}
                   for p in _downsample_equity(points, MAX_EQUITY_POINTS)],
        "trades": [{**t, "entry_ts": t["entry_ts"] // 1000, "exit_ts": t["exit_ts"] // 1000}
                   for t in kept],
    }
