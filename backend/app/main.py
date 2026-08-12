"""App factory & lifecycle. Binds 127.0.0.1 only (enforced by Makefile/run
commands); no app-level auth by design — see docs/plan/01-architecture.md."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .ai.client import AIManager
from .api.ai import router as ai_router
from .api.backtests import router as backtests_router
from .api.execution import router as execution_router
from .api.live_probe import router as live_probe_router
from .api.optimize import router as optimize_router
from .api.options import router as options_router
from .api.orders import router as orders_router
from .api.routes import router
from .api.runs import router as runs_router
from .api.settings import router as settings_router
from .api.strategies import router as strategies_router
from .auth.manager import AuthManager
from .config import get_settings
from .db import Database
from .execution.gateway import OrderGateway
from .execution.reconciliation import ReconciliationEngine
from .live.probe import LiveProbeService
from .logging import get_logger, setup_logging
from .marketdata.backfill import Backfiller
from .marketdata.calendar import MarketCalendar
from .marketdata.service import MarketDataService
from .marketdata.store import CandleStore
from .optimize.study import OptimizerRegistry
from .options.calibrate import Calibrator
from .options.chain_service import ChainService
from .options.store import OptionChainStore
from .runtime.bar_aggregator import BarAggregator
from .runtime.manager import RunManager
from .runtime.recovery import recover_at_startup
from .schwab.client import SchwabClient
from .strategy.files import StrategyFiles
from .streamer.acct_activity import AcctActivityRecorder
from .streamer.chart_raw import ChartRawRecorder
from .streamer.client import StreamerClient
from .streamer.option_recorder import ChainRecorder, snapshot_times
from .streamer.recorder import CandleRecorder

log = get_logger("main")

_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


class SPAStaticFiles(StaticFiles):
    """Client-side routes (e.g. /backtests) aren't real files on disk — a
    plain StaticFiles mount 404s on refresh for anything but '/'. Falling
    back to index.html for any 404 lets react-router take over, same as
    every other SPA host (Vercel/Netlify rewrite rules, etc.). Starlette's
    StaticFiles raises HTTPException(404) rather than returning a 404
    response, so the fallback has to catch it, not inspect a status code."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    app.state.settings = settings

    db = Database(settings.db_path)
    await db.connect()
    app.state.db = db

    auth = AuthManager(settings)
    auth.start_background_refresh()
    app.state.auth = auth

    schwab = SchwabClient(auth)
    app.state.schwab = schwab

    # market data stack
    store = CandleStore(db)
    backfiller = Backfiller(schwab, store)
    mds = MarketDataService(store, backfiller)
    app.state.marketdata = mds
    calendar = MarketCalendar(db, schwab)
    app.state.calendar = calendar

    # streamer + recorder (recording watchlist is a primary-goal component)
    streamer = StreamerClient(auth, schwab)
    app.state.streamer = streamer
    recorder = CandleRecorder(db, store, streamer, mds)
    app.state.recorder = recorder
    # diagnostic: capture every raw CHART_EQUITY item verbatim, before any
    # field-mapping, to root-cause the chart_bar_rejected_invalid_ohlc
    # pattern found live 2026-07-23/24 (see app/streamer/chart_raw.py).
    # Read-only, no effect on any order path — same rationale as
    # AcctActivityRecorder below.
    chart_raw = ChartRawRecorder(db, streamer)
    app.state.chart_raw = chart_raw

    # options data stack (docs/plan/16-options-trading.md). Purely ADDITIVE:
    # nothing here touches the equity path, and with an empty options
    # watchlist (the default) the ChainRecorder does no work at all. It is
    # constructed early and unconditionally because the value of recorded
    # option data is a pure function of elapsed calendar time — Schwab serves
    # NO option price history, so a day not recorded is a day lost forever
    # (plan/16 decision D7).
    option_store = OptionChainStore(db)
    app.state.option_store = option_store
    chain_service = ChainService(schwab, option_store)
    app.state.chain_service = chain_service
    option_slots = snapshot_times(settings.options_snapshot_interval_min,
                                  settings.options_snapshot_first_et,
                                  settings.options_snapshot_last_et)
    option_recorder = ChainRecorder(
        db, option_store, chain_service, streamer, calendar=calendar,
        intraday_stale_quote_s=settings.options_intraday_stale_quote_s,
        snapshot_slots=option_slots,
        tick_recording=settings.options_tick_recording,
        tick_strikes=settings.options_tick_strikes,
        tick_expiries=settings.options_tick_expiries,
        tick_max_symbols=settings.options_tick_max_symbols,
        tick_min_interval_ms=settings.options_tick_min_interval_ms)
    app.state.option_recorder = option_recorder
    app.state.calibrator = Calibrator(db, option_store)

    streamer.start()
    await recorder.start()
    await option_recorder.start_tick_flusher()

    # compute pool for backtests/sweeps (never blocks the live loop)
    import os
    from concurrent.futures import ProcessPoolExecutor
    app.state.pool = ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) - 1))

    # parameter-optimization studies (docs/plan/14-optimizer.md). Studies run
    # as background tasks in this process, so any left `running` in the DB
    # belong to a process that no longer exists -- mark them `interrupted`
    # rather than pretending they're alive. Not auto-resumed on purpose:
    # silently continuing a study whose code or strategy may have changed
    # underneath it would be worse than making the user press the button.
    app.state.optimizer = OptimizerRegistry()
    await db.execute(
        "UPDATE optimization_studies SET status='interrupted', finished_at=? "
        "WHERE status='running'", (int(time.time() * 1000),))

    # strategies: disk is the source of truth; index it on every start
    strategy_files = StrategyFiles(settings.strategies_dir, db)
    app.state.strategy_files = strategy_files
    await strategy_files.reindex()

    # paper-trading runtime (phase 5): OrderGateway is the single chokepoint
    # every order intent funnels through (60/min throttle per the Schwab
    # registration, ground rules); BarAggregator turns live 1m bars into a
    # strategy's configured timeframe; RunManager owns the set of active
    # StrategyRunners AND is the gateway's GlobalRiskContext (see
    # runtime/manager.py's module docstring). Crash/restart recovery MUST
    # run before anything else touches run_manager — see runtime/recovery.py.
    gateway = OrderGateway(db, orders_per_minute=60)
    app.state.gateway = gateway
    aggregator = BarAggregator(streamer)
    app.state.aggregator = aggregator
    run_manager = RunManager(db, strategy_files, mds, streamer, gateway, aggregator, schwab,
                             promotion_min_paper_days=settings.promotion_min_paper_days,
                             promotion_min_round_trips=settings.promotion_min_round_trips,
                             chain_service=chain_service,
                             option_recorder=option_recorder)
    app.state.run_manager = run_manager
    await run_manager.load_global_limits()
    recovery_summary = await recover_at_startup(db, run_manager)
    if any(recovery_summary.values()):
        log.warning("startup_recovery", **{k: v for k, v in recovery_summary.items() if v})

    # Phase 6 slice 1 — automatic kill-switch trips + reconciliation ("trust
    # Schwab, not our journal", docs/plan/06-execution-safety.md). A safe
    # no-op today (zero live runs exist until start_live() lands), scheduled
    # below every 5min during market hours so it's ready the moment they do.
    reconciliation = ReconciliationEngine(db, schwab, run_manager, gateway)
    app.state.reconciliation = reconciliation

    # AI generation (degrades gracefully when no key is configured)
    app.state.ai = AIManager(settings.anthropic_api_key, db=db)

    # Phase 5.5 — live plumbing probes: OFF by default (live_probe_enabled),
    # and gated per-request again in api/live_probe.py before app.state.live_probe
    # is ever touched. AcctActivityRecorder just captures raw messages for
    # later inspection (see its module docstring) — harmless read-only, so it
    # subscribes regardless of the flag; nothing else here can place an order
    # unless the flag is on AND every mutating call is separately confirmed.
    acct_activity = AcctActivityRecorder(db, streamer)
    app.state.acct_activity = acct_activity
    await acct_activity.start()
    if settings.live_probe_enabled:
        app.state.live_probe = LiveProbeService(
            db, schwab, gateway, settings.live_probe_max_qty, acct_activity,
            mds, settings.live_probe_max_notional, streamer)
        log.warning("live_probe_enabled", max_qty=settings.live_probe_max_qty,
                   max_notional=settings.live_probe_max_notional)

    # scheduled jobs: nightly top-up after the close (8pm ET ≈ 00:00 UTC)
    scheduler = AsyncIOScheduler(timezone="America/New_York")

    async def nightly() -> None:
        symbols = await recorder.get_watchlist()
        if symbols:
            await backfiller.nightly_topup(symbols)

    scheduler.add_job(nightly, CronTrigger(hour=20, minute=15))

    # Option chain snapshots, INTRADAY. One job per slot from
    # options_snapshot_first_et to options_snapshot_last_et, every
    # options_snapshot_interval_min minutes (default 30 -> 13 sweeps/day),
    # always including 15:50 as the last slot.
    #
    # The cadence is the point: an option chain cannot be backfilled from any
    # source, so today's interval is a permanent ceiling on the time resolution
    # of every backtest anyone will ever run against this data. A strategy that
    # trades at 10:15 can only be defended if the pricer was calibrated against
    # chains recorded at 10:15.
    #
    # Each job re-checks weekday AND market-open itself (see
    # ChainRecorder.scheduled_snapshot) and writes are idempotent by
    # (symbol, ts), so a double-fire is harmless. No-op while the options
    # watchlist is empty.
    for _h, _m in option_slots:
        scheduler.add_job(option_recorder.scheduled_snapshot,
                          CronTrigger(day_of_week="mon-fri", hour=_h, minute=_m),
                          id=f"option_snapshot_{_h:02d}{_m:02d}",
                          # A sweep that overruns its slot must not stack up
                          # behind the next one and double the REST load.
                          max_instances=1, coalesce=True, misfire_grace_time=300)
    log.info("option_snapshot_schedule", slots=option_recorder.schedule_et(),
             interval_min=settings.options_snapshot_interval_min)

    # Streamed option quotes. The recorded band is defined RELATIVE TO SPOT, so
    # it is rebuilt every 15 minutes during the session: a universe pinned at
    # the open is centred on the wrong strikes by the afternoon, and would be
    # recording wings while the money moved elsewhere. Each refresh is a diff,
    # so a normal one changes a handful of subscriptions.
    if settings.options_tick_recording:
        scheduler.add_job(option_recorder.refresh_tick_universe,
                          CronTrigger(day_of_week="mon-fri", hour="9-16",
                                      minute="*/15"),
                          id="option_tick_universe",
                          max_instances=1, coalesce=True, misfire_grace_time=120)

        async def prune_ticks_job() -> None:
            # Chain snapshots are irreplaceable and kept forever; ticks are
            # bulky and their value decays, so they age out. 0 disables.
            if not settings.options_tick_retention_days:
                return
            cutoff = int(time.time() * 1000) - \
                settings.options_tick_retention_days * 86_400_000
            deleted = await option_store.prune_ticks(cutoff)
            if deleted:
                log.info("option_ticks_pruned", deleted=deleted,
                         retention_days=settings.options_tick_retention_days)

        scheduler.add_job(prune_ticks_job, CronTrigger(hour=21, minute=30))

        # Build the universe NOW rather than waiting for the next */15 slot.
        # Without this a restart mid-session records nothing for up to fifteen
        # minutes, and those quotes cannot be backfilled from anywhere.
        # Deferred to a task so a slow chain fetch never delays startup, and
        # safe before the streamer has logged in: SubscriptionManager holds the
        # interest and _resubscribe_all replays it on LOGIN.
        async def _initial_tick_universe() -> None:
            try:
                await option_recorder.refresh_tick_universe()
            except Exception as e:  # noqa: BLE001
                log.warning("initial_tick_universe_failed", error=str(e))

        asyncio.create_task(_initial_tick_universe())

    # Weekly surface/spread refit from whatever chains have been recorded.
    # Refuses to write a fit below its observation threshold, so this is a
    # no-op until enough data exists rather than a source of confident noise.
    async def calibrate_job() -> None:
        for u in await option_store.underlyings():
            try:
                await app.state.calibrator.fit(u)
            except Exception as e:  # noqa: BLE001 — one bad symbol must not stop the sweep
                log.warning("calibration_failed", underlying=u, error=str(e))

    scheduler.add_job(calibrate_job, CronTrigger(day_of_week="sat", hour=6, minute=0))

    async def reconcile_job() -> None:
        if await app.state.calendar.is_open_now():
            await reconciliation.run_once()

    scheduler.add_job(reconcile_job, IntervalTrigger(minutes=5))
    scheduler.start()
    app.state.scheduler = scheduler
    log.info("reconciliation_job_scheduled", interval_minutes=5)

    await db.audit("system", "lifecycle", {"event": "startup"})
    log.info("startup_complete", db=str(settings.db_path))
    try:
        yield
    finally:
        await db.audit("system", "lifecycle", {"event": "shutdown"})
        await app.state.optimizer.shutdown()
        app.state.pool.shutdown(wait=False, cancel_futures=True)
        scheduler.shutdown(wait=False)
        # Before the streamer goes: flushes whatever is still buffered rather
        # than discarding up to a second of quotes on every restart.
        await option_recorder.stop_tick_flusher()
        await aggregator.stop()
        await streamer.stop()
        await schwab.close()
        await auth.stop()
        await db.close()


app = FastAPI(title="autotrader", lifespan=lifespan)
app.include_router(router)
app.include_router(strategies_router)
app.include_router(backtests_router)
app.include_router(ai_router)
app.include_router(runs_router)
app.include_router(live_probe_router)
app.include_router(orders_router)
app.include_router(execution_router)
app.include_router(optimize_router)
app.include_router(options_router)
app.include_router(settings_router)

if _FRONTEND_DIST.exists():
    app.mount("/", SPAStaticFiles(directory=_FRONTEND_DIST, html=True), name="ui")
