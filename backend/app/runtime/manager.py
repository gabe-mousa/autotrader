"""RunManager — owns the set of active StrategyRunners (docs/plan/06 lifecycle
states, 07-paper-trading promotion gate, 08-runtime-concurrency resource
arbitration).

Responsibilities:
- `start_paper(slug)`: enforce the hard promotion-gate prerequisite (this
  exact content-hash version has >=1 completed backtest — plan 07), create
  the `runs` DB row, build a per-run `SimBroker` fed by live
  `LEVELONE_EQUITIES` quotes, and start its `StrategyRunner`.
- `pause`/`resume`/`stop`/`flatten` by run_id, delegating to the runner.
- `status`/`list_active` for the future API layer (checklist item 10).

Live quote fan-out: RunManager registers its OWN `LEVELONE_EQUITIES` handler
on the shared `StreamerClient` (handlers are a list — see
`StreamerClient.on_data` — so this composes with `CandleRecorder`'s
independent handler on the same service) and routes each tick to whichever
active `SimBroker`s care about that symbol. `streamer.subscribe`/`unsubscribe`
ref-count internally (`SubscriptionManager`), so a run's subscription and
CandleRecorder's watchlist subscription to the same symbol don't fight each
other. LEVELONE_EQUITIES fields are deltas (only changed fields present per
tick — same as `CandleRecorder._on_quote`), so a per-tick merge against the
MarketDataService quote cache fills in the fields this tick didn't carry.

Crash/restart recovery (checklist item 9) and the HTTP API (item 10) are
deliberately NOT in this file — this is the in-process lifecycle owner they
both sit on top of. Wiring a `RunManager` instance into `main.py`'s lifespan
is also deferred to item 10, once restart recovery exists — starting paper
runs from here today would create runs that a process restart silently
orphans.

Documented v1 gaps carried over unchanged from `StrategyRunner` (item 7).
RunManager neither adds nor removes any of them; it only starts runners.
CORRECTED 2026-07-30 (audit C4) — this list said "trailing-stop live
maintenance and session-window/flatten_eod enforcement are not yet
implemented", which materially understated the divergence at the time (it
omitted shorting, `market_filter`, `selection` and `entry.pyramid`) and is now
wrong in the other direction: all of those have since been implemented. Rather
than restate a list that goes stale the moment someone closes another gap, the
single source of truth is `strategy/live_support.py::live_support_gaps(doc)`,
which is what lint, the run-start event log, the promotion gate and the
Running UI all read. As of the audit-remediation campaign the only BLOCKING gap
it can report is `orders.exit`, which is honoured by neither the engine nor the
runner; the rest are documented approximations that do not block promotion
(`_is_last_bar_of_day` on early-close days, one pyramid add per bar, a
partially-consumed scale-out ladder not surviving a restart, and a raised
`unfilled_timeout_sec` being clamped).

RunManager also IS the gateway's `GlobalRiskContext` (check #5 of the 8-check
stack, `execution/types.py`'s Protocol) — it's the only object that can see
every active run at once, which is what cross-run limits need. Before this,
`StrategyRunner` never passed a `global_risk` argument to `gateway.submit()`
at all, so check #5 (global notional cap, global daily-loss trip, symbol
allow/blocklist) silently never ran for ANY run — a real gap the Protocol's
own docstring flagged as "RunManager will supply the real implementation."
`_activate()` now injects `global_risk=self` into every `StrategyRunner` it
builds, closing it."""

from __future__ import annotations

import json
import time
import uuid

from ..broker.base import Broker
from ..broker.schwab import SchwabBroker
from ..broker.sim import SimBroker
from ..db import Database
from ..execution.gateway import OrderGateway
from ..logging import get_logger
from ..schwab.client import SchwabClient
from ..strategy.files import StrategyFiles
from ..strategy.hashing import content_hash
from ..strategy.live_support import blocking_gaps, gap_payloads
from .bar_aggregator import BarAggregator
from .runner import StrategyRunner

log = get_logger("run-manager")


def _options_promotion_issues(doc) -> list[str]:
    """Extra requirements before an OPTIONS strategy may trade real money
    (docs/plan/16 §11.4). Empty list for every shares strategy.

    These are deliberately about INFORMED CONSENT rather than performance. An
    options backtest is priced by a MODEL — Schwab sells no historical option
    data — so the numbers a user is promoting on are model output, not observed
    outcomes. The gate makes them acknowledge that and set a real dollar
    exposure, rather than discovering both after the first fill."""
    expr = getattr(doc, "expression", None)
    if expr is None or expr.instrument != "options":
        return []

    issues: list[str] = []
    opt_risk = doc.risk.options
    from ..strategy.schema import OptionRisk as _OptionRisk
    default_premium = _OptionRisk.model_fields["max_premium_at_risk"].default
    if opt_risk.max_premium_at_risk == default_premium:
        issues.append(
            f"risk.options.max_premium_at_risk is still the default "
            f"(${default_premium:,.0f}) — set it explicitly so the dollar "
            f"exposure is a deliberate choice")
    if expr.exit.max_loss_pct_of_premium is None and \
            expr.exit.stop_basis == "underlying":
        issues.append(
            "no broker-side protection: with stop_basis='underlying' and "
            "max_loss_pct_of_premium disabled, nothing rests at the broker and "
            "the stop depends on this app staying alive")
    if expr.pricing.vrp_mult < 1.0:
        issues.append(
            f"expression.pricing.vrp_mult={expr.pricing.vrp_mult} prices options "
            "below where they have historically traded, so the backtest this "
            "promotion rests on is optimistic by construction")
    return issues

GLOBAL_LIMITS_KEY = "global_risk_limits"
DEFAULT_GLOBAL_LIMITS = {
    "max_total_notional": 250_000.0,
    "max_total_daily_loss": 2_000.0,
    "symbol_blocklist": [],
    "symbol_allowlist": None,  # None = all symbols allowed
}


class RunManager:
    def __init__(self, db: Database, strategy_files: StrategyFiles, mds, streamer,
                gateway: OrderGateway, aggregator: BarAggregator, schwab: SchwabClient,
                promotion_min_paper_days: float = 14, promotion_min_round_trips: int = 10,
                chain_service=None, option_recorder=None):
        self._db = db
        self._strategy_files = strategy_files
        self._mds = mds
        self._streamer = streamer
        self._gateway = gateway
        self._aggregator = aggregator
        self._schwab = schwab
        # Options runs need these to select and price contracts. None for a
        # shares-only deployment and in tests; OptionsLive is unused there.
        self._chain_service = chain_service
        self._option_recorder = option_recorder
        self._promotion_min_paper_days = promotion_min_paper_days
        self._promotion_min_round_trips = promotion_min_round_trips
        self._runners: dict[str, StrategyRunner] = {}
        self._brokers: dict[str, Broker] = {}
        self._symbols: dict[str, list[str]] = {}
        self._global_limits = dict(DEFAULT_GLOBAL_LIMITS)  # overridable via app_state
        streamer.on_data("LEVELONE_EQUITIES", self._on_quote)

    # ---- GlobalRiskContext (execution/types.py Protocol) -------------------
    async def load_global_limits(self) -> None:
        """Call once after construction (main.py's lifespan, same pattern as
        `strategy_files.reindex()`) to pick up any persisted override. Safe
        to skip in tests — permissive built-in defaults apply until this
        (or `set_global_limits`) is called."""
        stored = await self._db.state_get(GLOBAL_LIMITS_KEY)
        if stored:
            self._global_limits.update(stored)

    async def set_global_limits(self, actor: str = "user", **overrides) -> dict:
        allowed = set(DEFAULT_GLOBAL_LIMITS)
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"unknown global limit key(s): {sorted(unknown)}")
        self._global_limits.update(overrides)
        await self._db.state_set(GLOBAL_LIMITS_KEY, self._global_limits)
        await self._db.audit(actor, "global_risk_limits", {"overrides": overrides})
        return dict(self._global_limits)

    def get_global_limits(self) -> dict:
        return dict(self._global_limits)

    @property
    def max_total_notional(self) -> float:
        return self._global_limits["max_total_notional"]

    @property
    def max_total_daily_loss(self) -> float:
        return self._global_limits["max_total_daily_loss"]

    @property
    def symbol_blocklist(self) -> frozenset[str]:
        return frozenset(self._global_limits["symbol_blocklist"])

    @property
    def symbol_allowlist(self) -> frozenset[str] | None:
        allow = self._global_limits["symbol_allowlist"]
        return frozenset(allow) if allow is not None else None

    def total_notional(self) -> float:
        total = 0.0
        for runner in self._runners.values():
            for symbol, pos in runner._positions.items():
                px = runner._last_price.get(symbol, pos.avg_price)
                total += abs(pos.qty * px)
        return total

    def total_daily_pnl(self) -> float:
        return sum(r.risk.daily_pnl() for r in self._runners.values())

    # ---- promotion-gate prerequisite (plan 07: backtest before paper) -----
    async def _latest_backtest_id(self, strategy_hash: str) -> str | None:
        row = await self._db.fetch_one(
            "SELECT id FROM backtests WHERE strategy_version_hash=? AND status='done' LIMIT 1",
            (strategy_hash,))
        return row["id"] if row else None

    async def _require_backtested(self, strategy_hash: str, slug: str) -> str:
        backtest_id = await self._latest_backtest_id(strategy_hash)
        if backtest_id is None:
            raise ValueError(
                f"strategy {slug!r} (version {strategy_hash[:8]}) has no completed backtest "
                "yet — run a backtest before starting a paper run (promotion gate, plan 07)")
        return backtest_id

    # ---- promotion-gate prerequisite (plan 07: paper evidence before live) -
    async def _paper_evidence(self, strategy_hash: str) -> dict | None:
        row = await self._db.fetch_one(
            """SELECT id, started_at, stopped_at FROM runs
               WHERE mode='paper' AND strategy_version_hash=?
               ORDER BY started_at DESC LIMIT 1""",
            (strategy_hash,))
        if row is None:
            return None
        elapsed_ms = (row["stopped_at"] or int(time.time() * 1000)) - row["started_at"]
        fills = await self._db.fetch_all(
            "SELECT payload_json FROM run_events WHERE run_id=? AND kind='fill'", (row["id"],))
        round_trips = sum(1 for f in fills if json.loads(f["payload_json"]).get("side") == "sell")
        return {"paper_run_id": row["id"], "elapsed_days": elapsed_ms / 86_400_000,
               "round_trips": round_trips}

    async def _promotion_evidence(self, strategy_hash: str) -> dict:
        """Non-raising status check — shared by `_require_promotable` (which
        raises on it) and the public `promotion_status` (a read-only "would
        this pass?" check the promotion wizard's first step calls before the
        user commits to anything, api/runs.py's GET /runs/live/eligibility)."""
        backtest_id = await self._latest_backtest_id(strategy_hash)
        evidence = await self._paper_evidence(strategy_hash)
        met = (evidence is not None
              and evidence["elapsed_days"] >= self._promotion_min_paper_days
              and evidence["round_trips"] >= self._promotion_min_round_trips)
        return {"backtest_id": backtest_id, "evidence": evidence, "met": met}

    async def promotion_status(self, slug: str) -> dict:
        doc = self._strategy_files.load(slug)  # raises FileNotFoundError, same as start_paper
        h = content_hash(doc)
        status = await self._promotion_evidence(h)
        evidence = status["evidence"]
        options_issues = _options_promotion_issues(doc)
        return {
            "eligible": (status["backtest_id"] is not None and evidence is not None
                         and status["met"] and not blocking_gaps(doc)
                         and not options_issues),
            # Options-specific readiness (docs/plan/16 §11.4). Surfaced
            # separately from live_support_gaps because the UI must be able to
            # show the "your backtest was MODELLED" acknowledgement distinctly
            # from a feature-support gap — they need different words.
            "is_options": getattr(doc, "expression", None) is not None
            and doc.expression.instrument == "options",
            "options_issues": options_issues,
            "has_backtest": status["backtest_id"] is not None,
            "backtest_id": status["backtest_id"],
            "has_paper_run": evidence is not None,
            "paper_run_id": evidence["paper_run_id"] if evidence else None,
            "paper_elapsed_days": evidence["elapsed_days"] if evidence else 0.0,
            "paper_round_trips": evidence["round_trips"] if evidence else 0,
            "min_paper_days": self._promotion_min_paper_days,
            "min_round_trips": self._promotion_min_round_trips,
            # AUDIT C1 — the gate used to check only backtest-exists +
            # paper-days + round-trips, and never inspected the DOCUMENT. A
            # strategy relying on a lever the live runtime ignored passed
            # cleanly. `eligible` now also requires no BLOCKING gap.
            "live_support_gaps": gap_payloads(doc),
        }

    async def _require_promotable(self, strategy_hash: str, slug: str, override: bool) -> dict:
        """override_gate=True bypasses EVERY promotion-readiness check below
        — no backtest, no paper run at all, or a too-thin one — an explicit
        user choice (not the plan 07 default). schema.sql's promotions
        table has backtest_run_id/paper_run_id nullable specifically to
        allow this; every override is still journaled (start_live's
        db.audit call) regardless of what it skipped."""
        status = await self._promotion_evidence(strategy_hash)
        evidence = status["evidence"]
        if not override:
            if status["backtest_id"] is None:
                raise ValueError(
                    f"strategy {slug!r} (version {strategy_hash[:8]}) has no completed backtest "
                    "yet — run a backtest before promoting to live (promotion gate, plan 07)")
            if evidence is None:
                raise ValueError(
                    f"strategy {slug!r} (version {strategy_hash[:8]}) has never been paper-traded "
                    "— pass override_gate=True to promote anyway (journaled)")
            if not status["met"]:
                raise ValueError(
                    f"strategy {slug!r} (version {strategy_hash[:8]}) doesn't meet the promotion "
                    f"minimums yet (need >={self._promotion_min_paper_days:.0f} days AND "
                    f">={self._promotion_min_round_trips} round trips; have "
                    f"{evidence['elapsed_days']:.1f} days, {evidence['round_trips']} round trips) — "
                    "pass override_gate=True to promote anyway (journaled)")
            # AUDIT C1 — a document-level check, not an evidence-level one: no
            # amount of paper trading makes a lever the live runtime ignores
            # start working, so this cannot be satisfied by waiting.
            gaps = blocking_gaps(self._strategy_files.load(slug))
            if gaps:
                detail = "; ".join(f"{gap.field}: {gap.detail}" for gap in gaps)
                raise ValueError(
                    f"strategy {slug!r} (version {strategy_hash[:8]}) uses features the LIVE "
                    f"runtime does not honour as backtested — {detail} — pass "
                    "override_gate=True to promote anyway (journaled)")
            # Options readiness. Also a document-level check that waiting
            # cannot satisfy.
            issues = _options_promotion_issues(self._strategy_files.load(slug))
            if issues:
                raise ValueError(
                    f"strategy {slug!r} (version {strategy_hash[:8]}) is not ready for LIVE "
                    f"options trading — {'; '.join(issues)} — pass override_gate=True "
                    "to promote anyway (journaled)")
        return {"backtest_run_id": status["backtest_id"],
               "paper_run_id": evidence["paper_run_id"] if evidence else None,
               "paper_elapsed_days": evidence["elapsed_days"] if evidence else 0.0,
               "paper_round_trips": evidence["round_trips"] if evidence else 0,
               "gate_met": status["met"]}

    # ---- starting a paper run ----------------------------------------------
    async def start_paper(self, slug: str, starting_cash: float = 100_000.0,
                          sim_latency_ms: float = 250, sim_slippage_bps: float = 1.0) -> str:
        doc = self._strategy_files.load(slug)
        h = content_hash(doc)
        await self._require_backtested(h, slug)
        strategy_row = await self._db.fetch_one("SELECT id FROM strategies WHERE slug=?", (slug,))
        if strategy_row is None:
            raise ValueError(f"strategy {slug!r} not indexed — save it before starting a run")

        run_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO runs (id, strategy_id, strategy_version_hash, mode, state,
               resume_policy, started_at, starting_equity)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, strategy_row["id"], h, "paper", "STARTING", "auto_resume",
             int(time.time() * 1000), starting_cash))

        broker = SimBroker(self._quote_provider, starting_cash=starting_cash,
                           sim_latency_ms=sim_latency_ms, sim_slippage_bps=sim_slippage_bps)
        runner, symbols = self._activate(run_id, doc, broker)
        await self._streamer.subscribe("LEVELONE_EQUITIES", symbols)
        try:
            await runner.start()
        except Exception:
            await self._deactivate_on_failure(run_id, symbols)
            raise
        return run_id

    # ---- promoting to live (plan 07: promotion gate) -----------------------
    async def start_live(self, slug: str, account_hash: str, override_gate: bool = False,
                         actor: str = "user") -> str:
        """Arms a strategy for live trading. The run is ALWAYS created paused
        (runner.start(start_paused=True) — a synchronous state, not a
        start-then-queue-a-pause race, see runner.py's start() docstring) —
        this is what plan 08's LIVE_ARMED state means: a human must
        separately call activate_live() before it ever submits a real
        order. Never auto-trades on its own."""
        doc = self._strategy_files.load(slug)
        h = content_hash(doc)

        if await self._gateway.kill_switch_active():
            raise ValueError("the global kill switch is active — cannot arm a live run right now")

        strategy_row = await self._db.fetch_one("SELECT id FROM strategies WHERE slug=?", (slug,))
        if strategy_row is None:
            raise ValueError(f"strategy {slug!r} not indexed — save it before starting a run")
        strategy_id = strategy_row["id"]

        existing_live = await self._db.fetch_one(
            "SELECT id FROM runs WHERE strategy_id=? AND mode='live' AND stopped_at IS NULL",
            (strategy_id,))
        if existing_live is not None:
            raise ValueError(
                f"strategy {slug!r} already has an active live run ({existing_live['id']}) — "
                "stop it before promoting again")

        accounts = await self._schwab.get_account_numbers()
        if account_hash not in {a["hashValue"] for a in accounts}:
            raise ValueError(f"account_hash {account_hash!r} is not a connected Schwab account")

        evidence = await self._require_promotable(h, slug, override_gate)

        # Real account equity at arm time — runs.starting_equity is NOT NULL
        # in spirit everywhere else (start_paper always sets it from
        # starting_cash); RunDetail.tsx calls .toLocaleString() on it
        # unconditionally, so leaving it NULL for live runs crashes the page.
        broker = SchwabBroker(self._schwab, account_hash)
        account_info = await broker.get_account()
        starting_equity = account_info.get("equity")

        run_id = str(uuid.uuid4())
        await self._db.execute(
            """INSERT INTO runs (id, strategy_id, strategy_version_hash, mode, state,
               resume_policy, started_at, starting_equity, account_hash)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, strategy_id, h, "live", "STARTING", "require_confirm",
             int(time.time() * 1000), starting_equity, account_hash))

        runner, symbols = self._activate(run_id, doc, broker, mode="live")
        await self._streamer.subscribe("LEVELONE_EQUITIES", symbols)
        try:
            await runner.start(start_paused=True)
        except Exception:
            await self._deactivate_on_failure(run_id, symbols)
            raise

        promotion_id = str(uuid.uuid4())
        gate_snapshot = {**evidence, "override": override_gate}
        await self._db.execute(
            """INSERT INTO promotions (id, strategy_version_hash, backtest_run_id, paper_run_id,
               gate_snapshot_json, confirmed_at) VALUES (?,?,?,?,?,?)""",
            (promotion_id, h, evidence["backtest_run_id"], evidence["paper_run_id"],
             json.dumps(gate_snapshot), int(time.time() * 1000)))
        await self._db.execute("UPDATE runs SET promotion_id=? WHERE id=?",
                               (promotion_id, run_id))
        await self._db.audit(actor, "promotion", {
            "run_id": run_id, "slug": slug, "account_hash": account_hash,
            **gate_snapshot})
        log.warning("strategy_promoted_to_live", run_id=run_id, slug=slug,
                   account_hash=account_hash, override=override_gate)
        return run_id

    async def activate_live(self, run_id: str, actor: str = "user") -> None:
        """The second, separate click that actually lets a freshly-armed live
        run start submitting real orders — never implicit in start_live()."""
        row = await self._db.fetch_one("SELECT mode, state FROM runs WHERE id=?", (run_id,))
        if row is None:
            raise ValueError(f"no run {run_id!r}")
        if row["mode"] != "live":
            raise ValueError(f"run {run_id!r} is not a live run")
        if row["state"] != "PAUSED":
            raise ValueError(
                f"run {run_id!r} is {row['state']!r}, not PAUSED — nothing to activate")
        await self.resume(run_id)
        await self._db.audit(actor, "promotion", {"event": "live_activated", "run_id": run_id})
        log.warning("live_run_activated", run_id=run_id)

    # ---- crash/restart recovery (called by app/runtime/recovery.py) -------
    async def recover_paper(self, run_id: str, slug: str, starting_cash: float,
                            positions: list[dict], today_counters: dict,
                            start_paused: bool = False,
                            sim_latency_ms: float = 250, sim_slippage_bps: float = 1.0) -> None:
        """Reactivate a run left dangling by an unclean shutdown. Unlike
        `start_paper`, the `runs` row already exists (from before the crash)
        and the promotion gate was already satisfied when it was first
        started — recovery re-enters an already-approved run, it doesn't
        re-decide whether one should start. `starting_cash`/`positions`/
        `today_counters` are pre-computed by recovery.py from durable DB
        state (the old SimBroker instance is gone — see recovery.py's module
        docstring for the full "why" and its known v1 limits)."""
        doc = self._strategy_files.load(slug)
        broker = SimBroker(self._quote_provider, starting_cash=starting_cash,
                           sim_latency_ms=sim_latency_ms, sim_slippage_bps=sim_slippage_bps)
        for p in positions:
            # The multiplier must be seeded too: without it the SimBroker marks
            # a recovered OPTION position at 1x and reports equity 100x too
            # small — and equity feeds sizing, so the error would compound into
            # every order the recovered run places.
            broker.seed_position(p["symbol"], p["qty"], p["avg_price"],
                                 multiplier=float(p.get("multiplier") or 1.0))
        runner, symbols = self._activate(run_id, doc, broker)
        await self._streamer.subscribe("LEVELONE_EQUITIES", symbols)
        try:
            await runner.start(recovered_positions=positions, today_counters=today_counters)
            if start_paused:
                await runner.pause()
        except Exception:
            await self._deactivate_on_failure(run_id, symbols)
            raise

    async def recover_live(self, run_id: str, slug: str, account_hash: str,
                           positions: list[dict], today_counters: dict,
                           adopted_protective: dict[str, dict] | None = None) -> None:
        """Re-adopt a LIVE run left dangling by an unclean shutdown, against a
        freshly built `SchwabBroker` for the run's own account.

        Three things make this different from `recover_paper`, all of them
        because the broker here is real and outlived our process:

        * No cash/position reconstruction. Schwab IS the position record;
          `positions` is passed in already reconciled against it by
          recovery.py, not derived from our journal.
        * Protective orders that are still resting at Schwab are ADOPTED, not
          re-placed (`adopted_protective`, see `SchwabBroker.adopt`).
        * The run ALWAYS comes back PAUSED, whatever it was doing before the
          crash. Recovery restores the app's ability to see, protect and
          flatten a live position — it never resumes real-money order entry on
          its own. That still takes the same explicit `activate_live()`
          confirmation a fresh promotion does, which is why `start_paused` is
          not a parameter here."""
        doc = self._strategy_files.load(slug)
        broker = SchwabBroker(self._schwab, account_hash)
        runner, symbols = self._activate(run_id, doc, broker, mode="live")
        await self._streamer.subscribe("LEVELONE_EQUITIES", symbols)
        try:
            await runner.start(recovered_positions=positions,
                               today_counters=today_counters,
                               adopted_protective=adopted_protective or {},
                               start_paused=True)
        except Exception:
            await self._deactivate_on_failure(run_id, symbols)
            raise

    def _activate(self, run_id: str, doc, broker, mode: str = "paper"
                  ) -> tuple[StrategyRunner, list[str]]:
        runner = StrategyRunner(run_id, doc, mode, broker, self._gateway, self._mds,
                                self._aggregator, self._db, global_risk=self,
                                chain_service=self._chain_service,
                                option_recorder=self._option_recorder)
        # An options run's SimBroker has no way to price an OCC symbol on its
        # own — the equity quote cache returns None for every one. The runner's
        # OptionsLive is that source, and it does not exist until the line
        # above, so the injection happens here rather than at construction.
        opt = getattr(runner, "_opt", None)
        if opt is not None and hasattr(broker, "set_option_quote_provider"):
            broker.set_option_quote_provider(opt.quote_dict)
        symbols = list(doc.universe.symbols)
        self._runners[run_id] = runner
        self._brokers[run_id] = broker
        self._symbols[run_id] = symbols
        return runner, symbols

    async def _deactivate_on_failure(self, run_id: str, symbols: list[str]) -> None:
        self._runners.pop(run_id, None)
        broker = self._brokers.pop(run_id, None)
        self._symbols.pop(run_id, None)
        await self._stop_broker(broker)
        await self._streamer.unsubscribe("LEVELONE_EQUITIES", symbols)
        await self._db.execute("UPDATE runs SET state='ERROR' WHERE id=?", (run_id,))

    @staticmethod
    async def _stop_broker(broker) -> None:
        """SchwabBroker.stop() cancels its background fill-polling task
        (SimBroker has no such task, hence no stop() at all -- getattr
        makes this safe for both). Forgetting this call leaves a
        SchwabBroker's poll loop running orphaned forever: it keeps hitting
        GET .../orders/{id} every poll_interval_s with nothing left
        tracking or able to cancel it, for as long as the process runs."""
        stop = getattr(broker, "stop", None)
        if stop is not None:
            await stop()

    def _quote_provider(self, symbol: str) -> dict | None:
        return self._mds.latest_cached_quote(symbol, max_age_s=3600)

    async def _on_quote(self, content: list[dict]) -> None:
        for item in content:
            symbol = item.get("key")
            if not symbol or not any(symbol in syms for syms in self._symbols.values()):
                continue
            cached = self._mds.latest_cached_quote(symbol, max_age_s=3600) or {}
            bid = item.get("1", cached.get("bid"))
            ask = item.get("2", cached.get("ask"))
            last = item.get("3", cached.get("last"))
            for run_id, broker in self._brokers.items():
                # SimBroker fills off these synthetic ticks; SchwabBroker
                # doesn't implement process_tick at all — a live run's fills
                # come from its own ACCT_ACTIVITY/polling path instead
                # (already validated by the live probe), so it must never be
                # called here.
                if broker.mode == "paper" and symbol in self._symbols.get(run_id, ()):
                    await broker.process_tick(symbol, bid, ask, last)
                    # An options run's resting orders are on OCC symbols, which
                    # never appear on LEVELONE_EQUITIES. The underlying's tick
                    # is their clock; their prices come from the option quote
                    # provider. Without this a paper option order — including a
                    # protective stop — rests forever.
                    await broker.process_option_ticks()

    # ---- control ------------------------------------------------------------
    def _get(self, run_id: str) -> StrategyRunner:
        runner = self._runners.get(run_id)
        if runner is None:
            raise KeyError(f"no active run {run_id!r}")
        return runner

    async def pause(self, run_id: str) -> None:
        await self._get(run_id).pause()

    async def resume(self, run_id: str) -> None:
        await self._get(run_id).resume()

    async def flatten(self, run_id: str) -> None:
        await self._get(run_id).flatten()

    async def stop(self, run_id: str) -> dict:
        runner = self._runners.get(run_id)
        if runner is None:
            # No in-memory runner — most commonly a live run recovery.py
            # could not safely reconcile against Schwab (state=
            # SUSPENDED_AUTH: expired refresh token, ambiguous account — see
            # its module docstring). Reconnecting is the preferred exit and
            # the UI offers it first, but it needs Schwab reachable; this is
            # the exit that always works. Without this branch such a run
            # could NEVER be stopped again: `_get` would raise KeyError forever and the
            # run would sit non-terminal, still counting as "the active
            # live run" for `start_live`'s one-live-run-per-strategy check.
            # This only closes the local bookkeeping row — it does NOT
            # touch anything on Schwab's side (there is no broker instance
            # left to do so through); callers must surface `forced=True`
            # to the user as an explicit "check Schwab directly" signal.
            return await self._force_close_dangling(run_id)
        await runner.stop()
        await self._db.execute("UPDATE runs SET stopped_at=? WHERE id=?",
                               (int(time.time() * 1000), run_id))
        symbols = self._symbols.pop(run_id, [])
        self._runners.pop(run_id, None)
        broker = self._brokers.pop(run_id, None)
        await self._stop_broker(broker)
        if symbols:
            await self._streamer.unsubscribe("LEVELONE_EQUITIES", symbols)
        return {"forced": False}

    async def _force_close_dangling(self, run_id: str) -> dict:
        row = await self._db.fetch_one("SELECT stopped_at, mode FROM runs WHERE id=?", (run_id,))
        if row is None or row["stopped_at"] is not None:
            raise KeyError(f"no active run {run_id!r}")
        await self._db.execute(
            "UPDATE runs SET state='STOPPED', stopped_at=? WHERE id=?",
            (int(time.time() * 1000), run_id))
        await self._db.audit("user", "run_control", {
            "run_id": run_id, "event": "force_stopped_no_active_process", "mode": row["mode"]})
        log.warning("run_force_stopped_no_active_process", run_id=run_id, mode=row["mode"])
        return {"forced": True, "mode": row["mode"]}

    def status(self, run_id: str) -> dict:
        return self._get(run_id).status()

    def list_active(self) -> list[dict]:
        return [r.status() for r in self._runners.values()]

    def live_run_symbols(self) -> dict[str, list[str]]:
        """{run_id: symbols} for every currently-active run whose broker mode
        is 'live' — used by the reconciliation engine (execution/
        reconciliation.py) to know which symbols are "ours" to compare
        against broker truth. Empty until Phase 6's start_live() exists;
        the reconciliation engine is written to be a safe no-op until then."""
        return {run_id: syms for run_id, syms in self._symbols.items()
               if self._runners[run_id].mode == "live"}

    # ---- kill switch mass-cancel (API's /api/kill activation, opt-in) -----
    async def cancel_all_working_orders(self, actor: str = "user") -> dict[str, list[str]]:
        """Mass-cancel every resting order across every ACTIVE run's broker.
        Delegates to `OrderGateway.kill_switch_cancel_all` per run (bypasses
        the 8-check stack — cancels must work even while the kill switch is
        blocking new placements). Returns {run_id: [order_ids that failed to
        cancel]} for runs with any failure, for manual review."""
        failed: dict[str, list[str]] = {}
        for run_id, broker in self._brokers.items():
            ids = broker.working_order_ids()
            if not ids:
                continue
            bad = await self._gateway.kill_switch_cancel_all(broker, ids, actor=actor)
            if bad:
                failed[run_id] = bad
        return failed
