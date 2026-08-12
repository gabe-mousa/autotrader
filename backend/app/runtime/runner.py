"""StrategyRunner — one asyncio task per active strategy (docs/plan/08).

Event loop: BarClosed -> evaluate rules (via the SAME StrategyEvaluator that
drives backtests) -> submit intents through OrderGateway. FillEvent -> update
position state, place/adjust resting protective stop/target orders. Control
-> pause/resume/stop/flatten.

Shorting (plan/15): the runner evaluates BOTH `entry.long` and `entry.short`
(long first — a symbol holds at most one position, so a document whose sides
both fire takes the long, identical to the backtest engine), opens shorts with
`sell_short`, closes them with `buy_to_cover`, mirrors protective stop/target
direction via `strategy/stops.py`, and uses `exit.signal_short` for a short's
signal exit. Position direction is an explicit `side` field with a POSITIVE
quantity (plan/15 decision #1) — see `_LivePosition`.

Deliberate v1 scope limits (documented, not silently skipped):
- Indicators are recomputed over the FULL accumulated candle history on every
  bar close (not incrementally) — cheap at bar-close cadence (>=1 min) and
  guarantees zero logic drift from the backtest engine, at the cost of a
  recompute that's more expensive than strictly necessary. Acceptable trade.
- `exit.trailing_stop` is maintained at BAR CLOSE (not per tick): the resting
  broker stop is amended whenever the ratchet moves it, which makes the live
  trail arithmetically identical to the backtest engine's at the cost of at
  most one amendment per bar. An `activate_at` gate keeps the trail dormant
  (contributing no stop at all — only `stop_loss` protects) until the
  favourable excursion reaches it. Crash recovery rebuilds the trail from the
  reloaded candle history; see `_recover_trail`.
- `market_filter` IS enforced, including its optional `exit_when` (see
  `_market_ok` / `_market_exit`). The reference symbol is subscribed at
  `start()` like any universe symbol, its bars accumulate in a SEPARATE frame
  (`_mf_candles`) and never drive entry evaluation or day rollover unless the
  symbol is genuinely also in `universe.symbols`. The gate reads the reference
  bar at or BEFORE the traded bar's timestamp (`searchsorted(..., "right")-1`),
  which is the same look-ahead guard the engine uses, and a configured filter
  with NO usable reference bar FAILS CLOSED — entries are blocked, and a
  `market_filter_no_data` event is logged once so "the gate is blocking" is
  distinguishable from "the gate is broken".
  PARITY CAVEAT, live-only: the engine evaluates a whole timestamp at once, so
  the reference bar for time t always exists before t is evaluated. Live, the
  reference symbol's bar and a traded symbol's bar for the same wall-clock
  minute arrive as two separate events in an arbitrary order. If the traded bar
  lands first, the newest reference bar available is the PREVIOUS one, so the
  gate can decide on a one-bar-stale regime read (or, on the very first bar
  after start when warm-up returned nothing for the reference symbol, fail
  closed). Both directions are conservative-or-equal relative to the backtest,
  and neither can ever look ahead.
- `entry.pyramid` (scale-IN) IS enforced (`_maybe_pyramid`). Adds go out as a
  distinct `pyramid` order purpose so the gateway can apply the daily budgets
  without applying `max_open_positions`, which an add must escape — it grows a
  position that already counted against the limit. The position merges to one
  holding at the volume-weighted average and its stop/target are re-derived from
  that average. ONE add per bar, where the engine's loop can consume several:
  each level is spaced off the previous add's actual fill, which live has not
  happened yet. Crash recovery does not restore a partly-built ladder, the same
  documented limitation as `exit.scale_outs`.
- `orders.exit` is STILL not honoured — by the engine either (it fills exits at
  the bar open with no order-spec handling), so implementing it live would
  create a fresh divergence rather than close one. It is reported by the
  live-support gap list instead. `orders.unfilled_timeout_sec` is still clamped
  to `ORDER_WAIT_CEILING_S`, but the clamp now logs an `orders_timeout_clamped`
  run event instead of being silent (audit A7).
- Short sizing reserves margin against open shorts (`_short_margin_committed`),
  matching the engine's `cash - reserved_margin`. Audit A8a described this as
  live UNDER-sizing shorts; it was the reverse — the runner passed raw broker
  cash, which for a short includes the sale proceeds, so it over-sized once a
  short was open. Shorts are also clamped to `max_short_notional` the way the
  engine clamps them, rather than being rejected outright.
- Maintenance margin / forced buy-in IS enforced (`_check_maintenance_margin`):
  every short is covered with reason `margin_call` when equity falls below
  `risk.short.maintenance_margin_pct` of short market value, matching the
  engine's step 3b. This is the unbounded-loss side of the book, so it is
  checked on every bar rather than once per timestamp.
- `selection` (cross-sectional top-N) IS enforced. It is the one rule that
  cannot be decided from a single symbol's bar, so entries on a `selection`
  document pass through a BARRIER (`_flush_selection_if_ready`): candidates for
  a timestamp are buffered and ranked once the whole universe has reported that
  timestamp, or once a later bar proves it is over. Exits are never buffered.
  Winners are entered in symbol-name order so that a binding
  `max_open_positions` resolves reproducibly instead of by bar arrival order.
  A document without `selection` takes exactly its pre-A3 path — no buffering,
  no added latency.
- `session.entry_window` / `session.flatten_eod` ARE enforced (see
  `_in_entry_window` / `_is_last_bar_of_day`), with one unavoidable
  difference from the backtest engine on EARLY-CLOSE days — documented on
  `_is_last_bar_of_day`.
- `exit.scale_outs` levels are placed as resting partial limit orders at
  entry, and the protective stop is resized down as each fills. Crash
  recovery does NOT re-arm a partially-consumed ladder (which levels already
  filled isn't journaled) — the remainder exits via stop/target/signal, and
  the run logs a `scale_outs_not_restored` event when that happens.
- OCO linkage (stop cancels target and vice versa) is implemented in THIS
  layer by watching fills and calling broker.cancel() on the sibling — not
  inside the broker itself (matches SimBroker's own documented design, and
  is how SchwabBroker will work too, submitting native OCO structures).

On ANY unhandled exception while processing an event, the runner logs it,
sets state=ERROR, and STOPS its own loop (fail toward stopped, not toward
silently continuing in a possibly-corrupted state) — resting broker-side
protective orders are never touched on an internal crash, so open positions
stay protected regardless (plan 06 §Stops: resting at the broker)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

import pandas as pd

from ..broker.base import Broker, BrokerEvent, OrderRequest
from ..db import Database
from ..execution.gateway import OrderGateway
from ..execution.types import OrderIntentRequest
from ..logging import get_logger
from ..options.expiry import expiry_to_ms
from ..options.pricing import transact_price
from ..options.symbology import describe as describe_symbol
from ..options.symbology import canonical_underlying, is_occ, parse_occ
from ..strategy.evaluator import StrategyEvaluator
from ..strategy.expression import Rejection, right_for_side
from ..strategy.live_support import gap_payloads
from ..strategy.market_filter import MarketFilterEval
from ..strategy.schema import StrategyDocument
from ..strategy.sizing import compute_contract_qty, compute_position_size
from ..strategy.stops import (Side, compute_scale_levels, compute_stop_price,
                              compute_trail_activation_px, compute_trail_px, trail_is_armed)
from ..strategy.timeframe import BAR_MS
from .bar_aggregator import BarAggregator, BarEvent
from .options_live import OptionsLive, is_options_run
from .risk import RunRiskTracker

log = get_logger("runner")

DEFAULT_WARMUP_BARS = 300

# Same constant, same name, same purpose as backtest/engine.py's ET — every
# session-clock decision (entry_window, flatten_eod, day rollover) converts to
# exchange-local time so DST can never silently shift a window by an hour.
ET = "America/New_York"

# Regular-session close, ET. `Session.hours` is `Literal["regular"]` (extended
# hours are reserved post-v1), so the runner's bars are RTH-only and the last
# bar of a normal trading day is the one CLOSING at 16:00.
SESSION_CLOSE_ET = (16, 0)

# Ceiling on `orders.unfilled_timeout_sec` as passed to the gateway. `max_wait_s`
# is how long the gateway BLOCKS waiting on the rate limiter, and blocking the
# runner's event loop for a configured 300s would stall every other symbol the
# run trades. The clamp is deliberate; what audit A7 flagged is that it used to
# be silent (see `_order_wait_s` and the `orders_timeout_clamped` event).
ORDER_WAIT_CEILING_S = 30


def _position_side(position: "_LivePosition") -> Side:
    """The side a live position is held on — the single place the trailing-stop
    code asks the question. Now backed by a real field (see `_LivePosition.side`
    for the positive-qty + explicit-side representation decision)."""
    return position.side


def _entry_order_side(side: Side) -> str:
    """The broker instruction that OPENS a position on `side`. SELL_SHORT is a
    genuinely different order from SELL — see broker/base.py's OrderSide."""
    return "buy" if side == "long" else "sell_short"


def _option_entry_side(side: Side) -> str:
    """The instruction that opens an OPTION position for a signal direction.

    Both directions BUY: a long signal buys calls, a short signal buys puts
    (plan/16 D2). The right was already decided by contract selection, so the
    instruction is the same either way — and it is emphatically NOT
    `sell_short`, which would route a put purchase through the equity
    short-selling path and its margin/borrow gates."""
    return "buy_to_open"


def _realized_pnl(side: Side, avg_price: float, exit_price: float, qty: float) -> float:
    """P&L on closing `qty` shares. Because quantities are unsigned, direction
    must be applied HERE and nowhere else — a short profits when the exit price
    is BELOW its average entry. Also the value recovery.py's `_reconstruct_cash`
    sums, where it equals a closed round trip's exact net cash effect for either
    side (sell notional − buy notional, whichever came first)."""
    return ((exit_price - avg_price) if side == "long" else (avg_price - exit_price)) * qty


def _close_order_side(side: Side) -> str:
    """The broker instruction that CLOSES a position on `side`. A long is
    closed by SELL, a short by BUY_TO_COVER (which the gateway additionally
    clamps against the open short so it can never buy through zero)."""
    return "sell" if side == "long" else "buy_to_cover"


@dataclass
class _LivePosition:
    """One open position.

    REPRESENTATION (docs/plan/15-shorting-and-expressiveness.md decision #1):
    `qty` is ALWAYS POSITIVE and direction lives in the explicit `side` field —
    the same convention the backtest engine uses (`engine.py`'s Position.side),
    so a strategy's paper/live behaviour and its backtest cannot drift apart.
    Signed quantities would push sign handling into every P&L, sizing and
    margin expression, which is exactly where direction bugs hide.

    The DURABLE `positions` row carries the same explicit `side` column
    (schema.sql), so direction survives a restart as data rather than as a
    sign convention. Its `qty` is still stored SIGNED — negative meaning short
    — because SimBroker's `seed_position` is signed internally and recovery.py's
    `_reconstruct_cash` gets shorts right for free that way (a short's opening
    fill ADDS proceeds to cash, which subtracting a negative cost basis does).
    `_upsert_position_row` applies the sign on the way out and
    `_recover_position` strips it on the way back in, so this class itself
    never holds a negative quantity."""

    symbol: str
    qty: float
    avg_price: float
    opened_at: int
    side: Side = "long"
    stop_broker_order_id: str | None = None
    target_broker_order_id: str | None = None
    bars_held: int = 0
    # resting partial take-profits (exit.scale_outs), broker_order_id -> shares.
    # Each fills at most once; as they fill, `qty` shrinks and the protective
    # stop is resized to match so it never tries to sell more than is held.
    scale_order_qty: dict[str, float] = field(default_factory=dict)
    original_qty: float = 0.0
    # ---- trailing stop (exit.trailing_stop) ----
    # Maintained at BAR CLOSE off the bar's high/low, deliberately not per
    # tick: it makes the live ratchet arithmetically identical to the backtest
    # engine's, and caps stop amendments at one per bar instead of one per
    # quote. `trail_px` is the current trail level; the RESTING stop order is
    # amended to max(stop_loss, trail_px) for a long whenever that moves.
    trail_dist: float | None = None
    trail_px: float | None = None
    trail_activate_px: float | None = None
    trail_armed: bool = True
    # ET session date this position was last SEEN in, stamped on the first bar
    # the runner processes while holding it (and pre-seeded from `opened_at` on
    # crash recovery). `flatten_eod` uses it to notice a position that outlived
    # its session — see `_stale_session`. Deliberately not derived from
    # `opened_at` alone: that is a real-clock fill timestamp, while the session
    # comparison has to be made in BAR time.
    session_day: object | None = None
    # ---- pyramiding (entry.pyramid, audit A6) ----------------------------
    # `add_qty` is the share count per add, fixed at the original entry's size ×
    # `size_pct`; `next_add_px` is the FAVOURABLE price that triggers the next
    # add, re-derived from each add's own fill so adds are spaced rather than
    # clustered. None once `max_adds` is spent. Not journaled, so crash recovery
    # does not restore a partly-built ladder — same documented limitation as
    # `exit.scale_outs`.
    adds_done: int = 0
    add_qty: float = 0.0
    next_add_px: float | None = None
    stop_px: float | None = None          # the price currently resting at the broker
    high_water: float = 0.0
    low_water: float = 0.0
    # ---- options (docs/plan/16 §9.1) --------------------------------------
    # All None/1.0 for a share position, so every expression elsewhere reads
    # identically whether or not this run trades options.
    #
    # NOTE `symbol` stays the UNDERLYING and `_positions` stays keyed by it —
    # the same structural choice the backtest engine makes, and what keeps
    # entry gating, selection, market_filter, the day roll and every stop
    # calculation working unchanged. `contract_symbol` is the OCC contract that
    # is actually held.
    contract_symbol: str | None = None
    contract_right: str | None = None      # "C" | "P"
    contract_strike: float | None = None
    contract_expiry: object | None = None  # datetime.date
    multiplier: float = 1.0
    #: premium paid per contract — for a LONG option this is also the maximum
    #: possible loss, known exactly at entry (plan/16 D2). It is what makes the
    #: in-process underlying stop acceptable: a crash cannot lose more than this.
    premium_paid: float = 0.0
    entry_iv: float | None = None
    entry_delta: float | None = None
    #: the underlying price at entry. `avg_price` holds the option PREMIUM for
    #: an options position, so the underlying reference is kept separately —
    #: every stop/target/trail level is an underlying price under the default
    #: stop_basis and must be compared against the right number.
    underlying_entry_px: float = 0.0

    @property
    def is_option(self) -> bool:
        return self.contract_symbol is not None


@dataclass
class _PendingIntent:
    intent_id: str
    broker_order_id: str
    purpose: str
    symbol: str
    # The side of the POSITION this order belongs to (not the order's own
    # instruction): carried here because BrokerEvent/Fill don't report a side,
    # so a fill for an entry that hasn't produced a `_LivePosition` yet has no
    # other way to know whether it opened a long or a short.
    side: Side = "long"
    # ---- options ----
    # Carried for the same reason as `side`: a Fill reports a price and a
    # quantity, not an instrument, so the contract identity has to survive from
    # submission to fill or the resulting position cannot be built.
    contract_symbol: str | None = None
    multiplier: float = 1.0
    underlying_px: float = 0.0
    quote_iv: float | None = None
    quote_delta: float | None = None


class StrategyRunner:
    def __init__(self, run_id: str, doc: StrategyDocument, mode: str, broker: Broker,
                gateway: OrderGateway, mds, aggregator: BarAggregator, db: Database,
                global_risk=None, chain_service=None, option_recorder=None):
        self.run_id = run_id
        self.doc = doc
        self.mode = mode
        # "MARGIN" | "CASH" | "" — "" denies shorting (plan/15 §B4). Paper runs
        # report MARGIN via RunRiskTracker; live reads it from the broker.
        self._account_type = ""
        self.broker = broker
        self.gateway = gateway
        self.mds = mds
        self.aggregator = aggregator
        self.db = db
        # RunManager (execution/types.py's GlobalRiskContext) — None in tests
        # that construct a StrategyRunner directly; the gateway's check #5
        # (global limits) simply no-ops when it's absent, same as before
        # RunManager injected the real implementation.
        self.global_risk = global_risk
        self.state = "STOPPED"
        # ---- options (docs/plan/16 §9) ---------------------------------------
        # None for every shares strategy, which is every run today. Every
        # options branch in this file is guarded by `self._opt is not None`, so
        # a run that is not an options run takes exactly the path it always did.
        # The chain service is what makes selection possible at all: without it
        # `chain_for()` returns (None, 0.0) and EVERY entry is blocked with
        # `no_chain_data`, which is exactly what an unwired runner did.
        self._opt = (OptionsLive(doc, chain_service=chain_service,
                                 recorder=option_recorder)
                     if is_options_run(doc) else None)

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._task: asyncio.Task | None = None
        self._candles: dict[str, pd.DataFrame] = {}
        # ---- market_filter (regime gate) state -------------------------------
        # The reference symbol's bars are kept OUT of `self._candles` on
        # purpose: `_candles` is the set of TRADEABLE frames (positions,
        # trailing recompute, ATR lookups and `_handle_bar`'s entry path all key
        # off it), and a reference symbol like SPY that nobody asked to trade
        # must not become tradeable just because it is used as a gate. When the
        # reference symbol IS also in the universe it simply lives in both.
        self._mf_candles: pd.DataFrame | None = None
        # (frame_len, last_close) -> compiled eval. The gate is recompiled from
        # the whole reference frame like every other indicator in this runtime
        # (see the docstring's recompute-on-every-bar note), but a single bar
        # asks the gate up to three questions (exit, long entry, short entry),
        # so the compile is memoised for that bar rather than repeated.
        self._mf_eval_cache: tuple[tuple[int, float], MarketFilterEval] | None = None
        self._mf_no_data_logged = False
        self._positions: dict[str, _LivePosition] = {}
        self._orders: dict[str, _PendingIntent] = {}
        self._pending_entries: set[str] = set()
        # symbols with a scale-IN add order working. Separate from
        # `_pending_entries`, which the gateway's duplicate-entry gate reads: an
        # add is not an entry and must not look like one, but two concurrent
        # adds in the same symbol would double the intended size.
        self._pending_adds: set[str] = set()
        self._last_price: dict[str, float] = {}
        self._cooldown_until_bar: dict[str, int] = {}
        self._entries_today: dict[str, int] = {}
        self._orders_today = 0
        self._realized_pnl_today = 0.0
        self._day = None
        # ---- selection (cross-sectional top-N) state -------------------------
        # symbol -> ((frame_len, last_close), evaluator). `selection` has to read
        # the ranking metric on EVERY universe symbol, and the runner already
        # compiles one symbol's full frame per arriving bar; caching that
        # compile keeps the total work at one compile per symbol per bar instead
        # of one per symbol per bar PER RANKING, which on the live 81-symbol run
        # would be the difference between fine and unusable.
        self._eval_cache: dict[str, tuple[tuple[int, float], StrategyEvaluator]] = {}
        # bar ts -> symbols that have reported a bar at it (any symbol, whether
        # or not it is an entry candidate) and ts -> candidate events awaiting a
        # ranking. Both empty and untouched when the document has no `selection`.
        self._sel_reported: dict[int, set[str]] = {}
        self._sel_candidates: dict[int, tuple[dict[str, BarEvent], bool]] = {}

        self.risk = RunRiskTracker(self)
        broker.subscribe_events(self._on_broker_event_raw)

    # ---- lifecycle ---------------------------------------------------------
    async def start(self, warmup_bars: int = DEFAULT_WARMUP_BARS,
                    recovered_positions: list[dict] | None = None,
                    today_counters: dict | None = None, start_paused: bool = False,
                    adopted_protective: dict[str, dict] | None = None) -> None:
        """`recovered_positions`/`today_counters`/`adopted_protective` are set
        only by crash/restart recovery (`app/runtime/recovery.py`) — a fresh
        start never passes them.

        `adopted_protective` is LIVE recovery only: `{symbol: {"stop"|"target":
        {"broker_order_id", "intent_id", "px", "qty"}}}` for protective orders
        that are STILL RESTING at Schwab. Those must be re-adopted, never
        re-placed — see `_recover_position` and `SchwabBroker.adopt`.

        Order matters: candles/warmup load FIRST (positions recovery
        re-derives ATR-based stop prices from `self._candles`), counters
        restore next, THEN positions (so `_upsert_position_row` /
        `_place_protective` run against the final state), and the run only
        flips to RUNNING/PAUSED once all of that has succeeded — if
        re-placing a recovered position's protective order fails, this
        raises and the run never reaches either (see recovery.py's per-run
        error handling, which never lets one bad run block recovering the
        others).

        `start_paused`: sets state PAUSED synchronously here, before
        `_task` is ever created, instead of the caller starting RUNNING and
        then separately awaiting `pause()` — `pause()`/`resume()` only
        enqueue a control message for `_run_loop` to process asynchronously,
        so start-then-pause has a real (if narrow) window where the run is
        genuinely RUNNING and could process a queued bar before the pause
        lands. `RunManager.start_live` depends on this being a hard
        guarantee, not a probabilistic one."""
        bar_ms = BAR_MS[self.doc.timeframe.bar]
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - warmup_bars * bar_ms
        for symbol in self.doc.universe.symbols:
            df = await self.mds.get_candles(symbol, self.doc.timeframe.bar, start_ms, end_ms)
            self._candles[symbol] = df
            if not df.empty:
                self._last_price[symbol] = float(df["close"].iloc[-1])
            await self.aggregator.register(symbol, self.doc.timeframe.bar, self._enqueue_bar)

        # ---- market_filter reference symbol (audit A4) ----------------------
        # The gate is evaluated on ANOTHER symbol's bars, so that symbol needs
        # the same warm-up load and the same live subscription as a traded one —
        # otherwise the frame stays empty and the gate fails closed forever.
        # `BarAggregator.register` is ref-counted so a double registration would
        # be safe, but registering twice would also mean unregistering once in
        # `stop()` leaves a live subscription behind, so it is guarded here
        # instead: a reference symbol already in the universe is subscribed by
        # the loop above and only needs its own frame.
        mf = self.doc.market_filter
        if mf is not None:
            mf_df = await self.mds.get_candles(mf.symbol, self.doc.timeframe.bar,
                                               start_ms, end_ms)
            self._mf_candles = mf_df
            if mf.symbol not in self.doc.universe.symbols:
                await self.aggregator.register(mf.symbol, self.doc.timeframe.bar,
                                               self._enqueue_bar)

        # Account type gates live shorting (plan/15 §B4): the gateway rejects
        # `sell_short` unless this reads MARGIN. Read once at start and
        # refreshed on every entry, so a CASH account can never short even if
        # the strategy asks to. A failure here leaves it UNKNOWN, which DENIES.
        await self._refresh_account_type()

        if today_counters:
            self._orders_today = today_counters.get("orders_today", 0)
            self._entries_today = dict(today_counters.get("entries_today", {}))
            self._realized_pnl_today = today_counters.get("realized_pnl_today", 0.0)

        for row in recovered_positions or []:
            await self._recover_position(
                row, adopted=(adopted_protective or {}).get(row["symbol"]))

        self.state = "PAUSED" if start_paused else "RUNNING"
        await self._update_run_state()
        # AUDIT C1 — the run's OWN event log records what it is not honouring
        # exactly as backtested, so the record exists at the moment it starts
        # mattering rather than only in the editor.
        gaps = gap_payloads(self.doc)
        if gaps:
            await self._log_event("live_support_gaps", {"gaps": gaps}, level="warn")
        if self.doc.orders.unfilled_timeout_sec > ORDER_WAIT_CEILING_S:
            await self._log_event(
                "config", {"event": "orders_timeout_clamped",
                           "configured_s": self.doc.orders.unfilled_timeout_sec,
                           "applied_s": ORDER_WAIT_CEILING_S}, level="warn")
        await self._log_event("state", {"event": "started", "mode": self.mode,
                                        "start_paused": start_paused,
                                        "recovered_positions": len(recovered_positions or [])})
        self._task = asyncio.create_task(self._run_loop(), name=f"runner-{self.run_id}")

    async def _refresh_account_type(self) -> None:
        """Read CASH/MARGIN from the broker. Any failure leaves the previous
        value (initially "") — which the gateway treats as "cannot short", so
        a broker hiccup degrades toward refusing shorts rather than allowing
        one into an account that cannot carry it."""
        try:
            account = await self.broker.get_account()
        except Exception as e:  # noqa: BLE001 — never block start on this
            log.warning("account_type_refresh_failed", run_id=self.run_id, error=str(e))
            return
        self._account_type = str(account.get("account_type", "") or "").upper()

    async def _recover_position(self, row: dict, adopted: dict | None = None) -> None:
        """Rebuild one open position from its durable `positions` row (the
        old SimBroker instance that was tracking it died with the process —
        see recovery.py's module docstring for why paper crash recovery
        reconstructs FROM the DB rather than reconciling against a broker).
        Re-derives protective stop/target prices from the strategy's own
        config against the position's avg_price — same formula as a real
        fill (`_on_entry_filled`) — and re-places them on the fresh
        SimBroker. Raises on failure (same invariant as a live fill: never
        continue with a configured-but-unprotected position), which fails
        THIS run's recovery without affecting any other run.

        `adopted` (live recovery only) names protective orders that are still
        RESTING at the broker: `{"stop"|"target": {"broker_order_id",
        "intent_id", "px", "qty"}}`. Those are re-attached rather than
        re-placed, because a Schwab DAY order outlives our process — placing a
        second stop for the same shares would oversell on the way out. The
        adopted price is the price ACTUALLY resting at the broker, not a
        recomputed one, so what the runner believes matches what will trigger.
        Anything not present in `adopted` (never placed, or already
        filled/canceled/expired while we were down) falls through to the
        normal re-place path below — including the empty dict, which is the
        correct input for a live position whose protection is genuinely gone."""
        raw_qty = float(row["qty"])
        # Direction comes from the explicit `positions.side` column. Rows
        # predating that column (or predating the caller selecting it) carry
        # either nothing or the DEFAULT 'long' while their qty may still be
        # negative — so a stored 'short', OR a negative quantity, both mean
        # short. That keeps a legacy short recovering as a short instead of
        # silently flipping to a long, which would place its protective stop on
        # the wrong side of the market.
        stored = str(row.get("side") or "").lower() if isinstance(row, dict) else ""
        side: Side = "short" if (stored == "short" or raw_qty < 0) else "long"
        qty = abs(raw_qty)

        # OPTION rows are keyed by the CONTRACT symbol but the in-memory map is
        # keyed by the UNDERLYING, so the two identities are separated here.
        # Getting this wrong would file the position under an OCC string that no
        # bar event ever matches, leaving it invisible to every exit path.
        row_asset = str(row.get("asset_type") or "EQUITY").upper() \
            if isinstance(row, dict) else "EQUITY"
        is_option_row = row_asset == "OPTION" and is_occ(str(row["symbol"]))
        if is_option_row:
            occ = parse_occ(str(row["symbol"]))
            symbol = str(row.get("underlying") or canonical_underlying(occ.underlying))
        else:
            symbol = row["symbol"]

        position = _LivePosition(
            symbol=symbol, qty=qty, avg_price=row["avg_price"],
            opened_at=row["opened_at"], side=side, original_qty=qty,
            high_water=row["avg_price"], low_water=row["avg_price"],
            # Seeded (rather than left None) so a `flatten_eod` position that
            # crashed across the close is swept on the first bar of the new
            # session instead of being adopted into it as if it were fresh.
            session_day=pd.Timestamp(row["opened_at"], unit="ms",
                                     tz="UTC").tz_convert(ET).date())
        if is_option_row:
            position.contract_symbol = str(row["symbol"])
            position.contract_right = occ.right
            position.contract_strike = float(occ.strike)
            position.contract_expiry = occ.expiry
            # The multiplier comes from the ROW, never assumed: recovering an
            # option at multiplier 1 would understate it by 100x everywhere.
            position.multiplier = float(row.get("multiplier") or 100.0)
            position.premium_paid = float(row.get("premium_paid")
                                          or row["avg_price"] or 0.0)
            # The UNDERLYING entry price is restored from its own column:
            # `avg_price` holds the premium, so without it every
            # underlying-based stop would silently re-anchor to whatever the
            # price happened to be at restart.
            und_entry = float(row.get("underlying_entry_px") or 0.0)
            position.underlying_entry_px = und_entry
            position.high_water = position.low_water = und_entry
            if self._opt is not None:
                await self._opt.subscribe([position.contract_symbol])
        self._positions[symbol] = position
        if self.doc.exit.scale_outs:
            # The `positions` row records the CURRENT quantity but not which
            # scale levels already filled, so re-arming the ladder from it
            # could double-sell a level that's already gone. Recovery
            # deliberately restores only the stop/target below and lets the
            # remainder exit through them — surfaced as an event rather than
            # silently dropped.
            await self._log_event(
                "recovery", {"event": "scale_outs_not_restored", "symbol": symbol,
                             "reason": "filled levels are not journaled; remainder exits via "
                                       "stop/target/signal"}, level="warning")
        await self._log_event("recovery", {"event": "position_recovered", "symbol": symbol,
                                           "side": side, "qty": position.qty,
                                           "avg_price": position.avg_price})
        e = self.doc.exit
        adopted = adopted or {}

        # OPTIONS take a different protective path entirely. The equity path
        # below rests a stop/target on the UNDERLYING's price, which for an
        # option position would be an order to sell shares this run does not
        # own. What an option position rests at the broker is its PREMIUM
        # backstop; the underlying stop is monitored in-process and needs no
        # order at all (plan/16 §9.4).
        if position.is_option:
            resting = await self._adopt_protective(symbol, position, "stop",
                                                   adopted.get("stop"))
            if resting is None and self._opt is not None:
                backstop_px = self._opt.max_loss_stop_px(position.premium_paid)
                if backstop_px is not None:
                    position.stop_broker_order_id = await self._place_option_backstop(
                        symbol, position, position.qty, backstop_px)
                    if position.stop_broker_order_id is None:
                        raise RuntimeError(
                            "recovery failed to re-place the option loss backstop "
                            f"for {symbol}")
            # The underlying stop level is re-derived on the first bar after
            # recovery, once an underlying price is known again — `avg_price`
            # holds the premium here and cannot supply one.
            await self._upsert_position_row(position)
            return

        # MIRRORED for a short: the protective stop rests ABOVE the entry and
        # the target BELOW it. `compute_stop_price(side=, kind=)` owns that rule
        # so recovery cannot compute it differently from a live fill.
        if e.stop_loss:
            resting = await self._adopt_protective(symbol, position, "stop", adopted.get("stop"))
            if resting is None:
                atr_value = self._atr_value_for(symbol, e.stop_loss)
                px = compute_stop_price(e.stop_loss, position.avg_price, atr_value,
                                        side=side, kind="stop")
                position.stop_broker_order_id = await self._place_protective(
                    symbol, position.qty, "stop", px, position.avg_price, side=side)
                position.stop_px = px
        await self._recover_trail(symbol, position)
        if e.take_profit:
            resting = await self._adopt_protective(symbol, position, "target",
                                                   adopted.get("target"))
            if resting is None:
                atr_value = self._atr_value_for(symbol, e.take_profit)
                px = compute_stop_price(e.take_profit, position.avg_price, atr_value,
                                        side=side, kind="target")
                position.target_broker_order_id = await self._place_protective(
                    symbol, position.qty, "target", px, position.avg_price, side=side)
        await self._upsert_position_row(position)
        if (e.stop_loss and position.stop_broker_order_id is None) or \
                (e.take_profit and position.target_broker_order_id is None):
            raise RuntimeError(
                f"recovery failed to re-place a configured protective order for {symbol}")

    async def _adopt_protective(self, symbol: str, position: _LivePosition, purpose: str,
                                spec: dict | None) -> str | None:
        """Re-attach one still-resting protective order to this run (live
        recovery). Returns its broker_order_id, or None when there is nothing
        to adopt — in which case the caller places a fresh order.

        Restores the three links that died with the old process: the broker's
        own tracking (`SchwabBroker.adopt`, so the poller sees its fill and
        `cancel`/`replace` can reach it), this runner's `_orders` map keyed by
        broker_order_id (so the fill is routed to the stop/target exit path
        instead of being dropped as unrecognised), and the position's
        stop/target ids and `stop_px`.

        A quantity mismatch between the resting order and the recovered
        position — possible when a scale-out or partial fill landed and the
        replacement stop never got placed before the crash — leaves shares
        unprotected. For the STOP that is repaired immediately by replacing it
        at the position's real quantity (raising if that fails, the same
        halt-the-run invariant as everywhere else); for the TARGET, which
        cannot lose money by being too small, it is reported and left."""
        if not spec or not spec.get("broker_order_id"):
            return None
        adopt = getattr(self.broker, "adopt", None)
        if adopt is None:  # SimBroker: nothing survives the process, never adopt
            return None
        oid, px = str(spec["broker_order_id"]), spec.get("px")
        resting_qty = float(spec.get("qty") or position.qty)
        req = OrderRequest(
            intent_id=spec.get("intent_id") or oid, symbol=symbol,
            side=_close_order_side(position.side), qty=resting_qty,
            order_type="stop" if purpose == "stop" else "limit",
            stop_px=px if purpose == "stop" else None,
            limit_px=None if purpose == "stop" else px)
        adopt(oid, req, float(spec.get("filled_qty") or 0.0))
        self._orders[oid] = _PendingIntent(req.intent_id, oid, purpose, symbol)
        if purpose == "stop":
            position.stop_broker_order_id = oid
            position.stop_px = px
        else:
            position.target_broker_order_id = oid
        await self._log_event("recovery", {
            "event": "protective_adopted", "symbol": symbol, "purpose": purpose,
            "side": position.side, "broker_order_id": oid, "qty": resting_qty,
            "price": round(px, 4) if px is not None else None})
        if abs(resting_qty - position.qty) > 1e-9:
            await self._log_event("recovery", {
                "event": "protective_qty_mismatch", "symbol": symbol, "purpose": purpose,
                "resting_qty": resting_qty, "position_qty": position.qty,
                "action": "stop replaced at the position quantity" if purpose == "stop"
                          else "target left as-is (a short target cannot lose money)"},
                level="error")
            if purpose == "stop" and px is not None:
                await self._replace_protective_stop(symbol, position, px)
                return position.stop_broker_order_id
        return oid

    async def pause(self) -> None:
        await self._queue.put(("control", "pause"))

    async def resume(self) -> None:
        await self._queue.put(("control", "resume"))

    async def flatten(self) -> None:
        await self._queue.put(("control", "flatten"))

    async def stop(self) -> None:
        await self._queue.put(("control", "stop"))
        if self._task:
            await self._task
        for symbol in self.doc.universe.symbols:
            await self.aggregator.unregister(symbol, self.doc.timeframe.bar, self._enqueue_bar)
        # exactly mirrors the conditional registration in `start()` — the
        # reference symbol was only subscribed separately when it is NOT a
        # universe symbol, so it is only unsubscribed separately in that case
        mf = self.doc.market_filter
        if mf is not None and mf.symbol not in self.doc.universe.symbols:
            await self.aggregator.unregister(mf.symbol, self.doc.timeframe.bar,
                                            self._enqueue_bar)

    def status(self) -> dict:
        """Live run state for the API/UI.

        Option positions carry their contract identity and CURRENT greeks, so
        the UI can show what is actually held rather than an OCC string — a
        user reasons about "a 35-delta SPY call expiring in 22 days", not about
        `SPY   260918C00680000`."""
        positions: dict[str, dict] = {}
        for sym, p in self._positions.items():
            entry: dict = {"qty": p.qty, "side": p.side, "avg_price": p.avg_price,
                           "bars_held": p.bars_held,
                           "asset_type": "OPTION" if p.is_option else "EQUITY"}
            if p.is_option:
                q = self._opt.quote(p.contract_symbol) if self._opt else None
                dte = (self._opt.dte(p.contract_expiry, int(time.time() * 1000))
                       if self._opt and p.contract_expiry else None)
                entry.update({
                    "contract": p.contract_symbol,
                    "contract_label": describe_symbol(p.contract_symbol),
                    "right": p.contract_right, "strike": p.contract_strike,
                    "expiry": p.contract_expiry.isoformat() if p.contract_expiry else None,
                    "dte": dte, "multiplier": p.multiplier,
                    "premium_paid": p.premium_paid,
                    "premium_at_risk": round(p.premium_paid * p.qty * p.multiplier, 2),
                    "underlying_entry_px": p.underlying_entry_px,
                    "mark": round(q.mid, 4) if q else None,
                    "delta": round(q.greeks.delta, 4) if q else p.entry_delta,
                    "theta": round(q.greeks.theta, 4) if q else None,
                    "vega": round(q.greeks.vega, 4) if q else None,
                    "iv": round(q.iv, 4) if q else p.entry_iv,
                    "unrealized_pnl": (round((q.mid - p.avg_price) * p.qty * p.multiplier, 2)
                                       if q else None),
                })
            positions[sym] = entry

        out = {
            "run_id": self.run_id, "state": self.state, "mode": self.mode,
            "positions": positions,
            "pending_entries": sorted(self._pending_entries),
            "orders_today": self._orders_today,
            "realized_pnl_today": round(self._realized_pnl_today, 2),
        }
        if self._opt is not None:
            g = self._opt.portfolio_greeks(self._positions)
            out["options"] = {
                "instrument": "options",
                "portfolio_delta": round(g["delta"], 2),
                "portfolio_vega": round(g["vega"], 2),
                "premium_at_risk": round(g["premium_at_risk"], 2),
                "max_premium_at_risk": self.doc.risk.options.max_premium_at_risk,
                "breaches": self._opt.breaches(self._positions),
                "rejections": dict(sorted(self._opt.rejections.items())),
                # The UI badges this: an underlying-based stop is monitored
                # in-process rather than resting at the broker (plan/16 §9.4).
                "stop_basis": self.doc.expression.exit.stop_basis,
                "broker_side_protection":
                    self.doc.expression.exit.max_loss_pct_of_premium is not None,
            }
        return out

    # ---- ingestion from BarAggregator / Broker ------------------------------
    async def _enqueue_bar(self, event: BarEvent) -> None:
        try:
            self._queue.put_nowait(("bar", event))
        except asyncio.QueueFull:
            log.error("event_queue_full", run_id=self.run_id, symbol=event.symbol)
            self.state = "DEGRADED_DATA"
            await self._update_run_state()

    async def _on_broker_event_raw(self, event: BrokerEvent) -> None:
        try:
            self._queue.put_nowait(("broker_event", event))
        except asyncio.QueueFull:
            log.error("event_queue_full_broker_event", run_id=self.run_id)

    # ---- main loop -----------------------------------------------------------
    async def _run_loop(self) -> None:
        while True:
            kind, payload = await self._queue.get()
            try:
                if kind == "bar":
                    await self._handle_bar(payload)
                elif kind == "broker_event":
                    await self._handle_broker_event(payload)
                elif kind == "control":
                    if await self._handle_control(payload):
                        return
            except Exception as e:  # noqa: BLE001 — fail toward stopped, never silent-continue
                log.error("runner_crashed", run_id=self.run_id, kind=kind, error=str(e))
                self.state = "ERROR"
                await self._update_run_state()
                await self._log_event("error", {"kind": kind, "error": str(e)}, level="error")
                return

    async def _handle_control(self, cmd: str) -> bool:
        if cmd == "pause":
            self.state = "PAUSED"
            await self._update_run_state()
            await self._log_event("state", {"event": "paused"})
        elif cmd == "resume":
            if self.state == "PAUSED":
                self.state = "RUNNING"
                await self._update_run_state()
                await self._log_event("state", {"event": "resumed"})
        elif cmd == "flatten":
            for symbol, position in list(self._positions.items()):
                px = self._last_price.get(symbol, position.avg_price)
                await self._submit_exit(symbol, position, px, reason="flatten")
        elif cmd == "stop":
            self.state = "STOPPED"
            await self._update_run_state()
            await self._log_event("state", {"event": "stopped"})
            return True
        return False

    # ---- bar handling ----------------------------------------------------------
    async def _handle_bar(self, event: BarEvent) -> None:
        """Processes one bar, then — for a `selection` document only — asks the
        barrier whether this bar completed a timestamp's universe.

        The barrier check lives OUT here rather than inside `_handle_bar_inner`
        because that method has many early returns (a short frame, every exit
        path) and the check has to run exactly once per universe bar AFTER all
        of that bar's own processing. A symbol that exited or holds a position
        still reports its bar to the barrier; only its candidacy is skipped."""
        # Option expiry is checked FIRST, mirroring the engine's step 1b: a
        # contract at its close-out DTE must be resolved before any other exit
        # logic marks or stops it, and an expired one must never be traded.
        await self._check_option_expiry(event.ts)
        await self._tick_held_options()
        # The in-process underlying stop runs BEFORE the bar's other logic, in
        # the same position the engine checks stops: before entries, so a stop
        # and a re-entry cannot both happen on one bar.
        await self._check_option_underlying_stops(event)
        await self._handle_bar_inner(event)
        if self.doc.selection is not None and event.symbol in self.doc.universe.symbols:
            await self._flush_selection_if_ready(event.ts)
        # LAST, after this bar's exits and entries — the engine's ordering (its
        # forced buy-in is step 3b, after entry evaluation and before the equity
        # mark), so a cover is never triggered by an entry the same bar has not
        # yet taken.
        await self._check_maintenance_margin()
        await self._check_option_risk()

    async def _handle_bar_inner(self, event: BarEvent) -> None:
        if self.state not in ("RUNNING", "PAUSED"):
            return
        symbol = event.symbol
        mf = self.doc.market_filter
        if mf is not None and symbol == mf.symbol:
            # a reference bar always updates the gate's frame, whether or not
            # the symbol is also traded
            self._append_mf_bar(event)
        if symbol not in self.doc.universe.symbols:
            # REFERENCE-ONLY symbol: no position, no entry evaluation, and
            # deliberately NO `_maybe_roll_day` / `_last_price`. Parity basis:
            # the engine's merged timeline is built from `frames`, which it
            # populates from `doc.universe.symbols` ONLY (engine.py's frame
            # loop) — the reference frame is read by `market_ok`/`market_exit`
            # via searchsorted and never contributes a timestamp to the loop.
            # So a reference bar cannot roll the engine's day, reset its
            # per-day counters, or trigger its stale-session sweep, and it must
            # not do any of those things here either. It is a data update, not
            # a clock tick.
            return
        self._maybe_roll_day(event.ts)
        self._append_bar(symbol, event)
        self._last_price[symbol] = event.close

        # Registered BEFORE the short-frame guard below: a symbol still warming
        # up has genuinely reported its bar for this timestamp, and a barrier
        # that waited for it to become evaluable would stall the whole universe
        # behind the slowest warm-up.
        if self.doc.selection is not None and symbol in self.doc.universe.symbols:
            self._sel_reported.setdefault(event.ts, set()).add(symbol)

        df = self._candles.get(symbol)
        if df is None or len(df) < 2:
            return
        evaluator = self._evaluator_for(symbol)
        if evaluator is None:
            return
        i = len(df) - 1

        last_bar = self._flatten_eod() and self._is_last_bar_of_day(event.ts)

        position = self._positions.get(symbol)
        if position is not None:
            position.bars_held += 1
            time_stop = self.doc.exit.time_stop
            if time_stop and position.bars_held >= time_stop.bars:
                await self._submit_exit(symbol, position, event.close, reason="time")
                return
            # end-of-day flatten. Placed AFTER the time stop and BEFORE the
            # signal exit, matching the engine's ordering (its eod check lives
            # in the intraday-exit pass, which runs before bar-close rule
            # evaluation) so the recorded exit reason is the same one the
            # backtest would have attributed. Goes through `_submit_exit`
            # (purpose="exit"), so it closes a short with `buy_to_cover` and
            # cancels the resting stop/target/scale-out orders on fill exactly
            # as the discretionary flatten does.
            stale = self._stale_session(position) and self._flatten_eod()
            if last_bar or stale:
                await self._submit_exit(symbol, position, event.close,
                                        reason="eod" if last_bar else "eod_late")
                return
            # a regime gate carrying `exit_when` also CLOSES positions. Checked
            # BEFORE the document's own exit signal so the recorded reason is
            # attributable to the gate — identical ordering and identical reason
            # string (`"market_filter"`) to the engine's exit pass.
            if await self._market_exit(event.ts, position.side):
                await self._submit_exit(symbol, position, event.close,
                                        reason="market_filter")
                return
            # `exit_signal` picks `exit.signal_short` for a short (falling back
            # to `exit.signal` when the document doesn't define one) — passing
            # the side is what makes `exit.signal_short` reachable at all.
            if evaluator.exit_signal(symbol, i, position.side):
                await self._submit_exit(symbol, position, event.close, reason="signal")
                return
            # scale-IN add, then trailing maintenance LAST, on a position this
            # bar did not exit — same ordering as the backtest engine, whose add
            # block also sits after the stop check and before the trail update.
            await self._maybe_pyramid(symbol, position, event)
            await self._maintain_trail(symbol, position, event.high, event.low)
            return

        # ---- entry path ---------------------------------------------------
        # Without `selection` this symbol's entry is decided here and now, which
        # is byte-for-byte the behaviour before A3 and the majority case.
        if self.doc.selection is None:
            await self._try_entry(symbol, event, df, evaluator, i, last_bar)
            return
        # With `selection` the decision needs the whole universe's bar for this
        # timestamp before it can rank — see `_record_selection_bar`.
        await self._record_selection_bar(symbol, event, last_bar)

    async def _log_blocked(self, symbol: str, reason: str, **detail) -> None:
        """AUDIT C3 — a signal that fires and produces no order used to be
        indistinguishable, from the event log, from no signal at all.

        Only reached when the document's rule genuinely FIRED and something else
        stopped the order, which is exactly the situation the user cannot
        currently explain. Quiet bars log nothing, so the volume is bounded by
        real signals rather than by bars × symbols.

        Logged at INFO: a fired-but-blocked signal is a notable event, not a
        diagnostic. The backtest engine has the same blind spot, so a run's event
        log is now strictly more informative than its backtest."""
        await self._log_event("entry_blocked", {"symbol": symbol, "reason": reason,
                                               **detail},
                              level="info")

    async def _try_entry(self, symbol: str, event: BarEvent, df: pd.DataFrame,
                         evaluator: StrategyEvaluator, i: int, last_bar: bool) -> None:
        """The per-symbol entry gates and side loop, unchanged from before A3 and
        extracted only so the `selection` barrier can call it at flush time with
        the same arguments a live bar would have produced."""
        if self.state != "RUNNING" or symbol in self._pending_entries:
            return
        if symbol in self._positions:
            # reachable only via the barrier: a position can open between a bar
            # being buffered and the flush that ranks it
            return
        # Per-symbol and session gates. Collected rather than returned on
        # immediately (audit C3): a blocked bar is only worth an event if the
        # document's rule ACTUALLY FIRED on it, so the decision is deferred until
        # after the side loop below. Every one of these is a pure predicate, so
        # evaluating them before the signal or after it gives the same outcome.
        # Order is the engine's: per-symbol counters, then the entry window, then
        # the last-bar-of-day block — `flatten_eod` does not only CLOSE at the
        # close, it also refuses to OPEN on the bar it would immediately have to
        # flatten, which is the difference between a lever that squares the book
        # and one that churns a trade per day for nothing.
        blocked = self._entry_block_reason(symbol, i)
        if blocked is None and not self._in_entry_window(event.ts):
            blocked = "entry_window"
        if blocked is None and last_bar:
            blocked = "flatten_eod_last_bar"

        side = await self._firing_side(symbol, event, evaluator, i)
        if side is None:
            # genuinely no signal — there is nothing for the user to explain, and
            # logging every quiet bar would bury the log (81 symbols × ~390 bars
            # a day on the largest running strategy)
            return
        if blocked is not None:
            await self._log_blocked(symbol, blocked, side=side)
            return
        await self._submit_entry(symbol, event, df, evaluator, i, side)

    async def _firing_side(self, symbol: str, event: BarEvent,
                           evaluator: StrategyEvaluator, i: int) -> Side | None:
        """Which side this document would OPEN on this bar, or None.

        Long is checked FIRST and the first side to pass both the regime gate and
        its own rule wins: a symbol holds at most one position (Entry's schema
        docstring), so a document whose long and short rules both fire on the
        same bar takes the long — identical precedence to the backtest engine,
        which is the point of doing it the same way here. The regime gate is
        ANDed in BEFORE the document's own rule, also as in the engine, which is
        why this cannot be reduced to "whichever side's signal is true"."""
        for side in ("long", "short"):
            if side == "short" and not self._short_allowed(symbol):
                continue
            if not await self._market_ok(event.ts, side):
                continue
            if evaluator.entry_signal(symbol, i, side):
                return side
        return None

    def _short_allowed(self, symbol: str) -> bool:
        """Can this run open a SHORT in `symbol`? The explicit `risk.short`
        opt-in is deliberately separate from the presence of `entry.short`, so a
        short rule can never start trading through a config accident — same
        three-part test the backtest engine applies (`engine.short_allowed`).
        The gateway re-checks all of this (plus account type) independently;
        this is the cheap local pre-filter, not the safety boundary."""
        if not self.doc.entry.short:
            return False
        # OPTIONS ARE THE EXCEPTION, and it matters — the same carve-out the
        # engine needed. On an options document a "short" signal BUYS PUTS
        # (plan/16 D2): long premium, no margin, no borrow, loss capped at the
        # debit. Gating that behind stock-borrow machinery would take zero
        # trades and log no reason, which is how this was found in the engine.
        if self._opt is not None:
            return self.doc.expression.short is not None
        risk = self.doc.risk.short
        return (risk.enabled
                and symbol.upper() not in {s.upper() for s in risk.hard_to_borrow})

    def _entry_block_reason(self, symbol: str, i: int) -> str | None:
        """Which per-SYMBOL entry gate blocks this bar, or None if none does.

        Deliberately side-agnostic: `max_entries_per_day` and `cooldown_bars`
        limit how often a symbol is traded, not how often it is traded in one
        direction, so a symbol that has spent its allowance going long cannot
        then spend it again going short.

        Returns the REASON rather than a bool (audit C3) so the event log can say
        which lever stopped a trade — "no signal" and "blocked by cooldown" are
        very different things to the user reading it, and used to look identical."""
        if self._entries_today.get(symbol, 0) >= self.doc.entry.max_entries_per_day:
            return "max_entries_per_day"
        cooldown = self._cooldown_until_bar.get(symbol)
        if cooldown is not None and i < cooldown:
            return "cooldown"
        return None

    def _entry_allowed(self, symbol: str, i: int) -> bool:
        """Boolean form of `_entry_block_reason`, kept for readability at call
        sites that do not report a reason."""
        return self._entry_block_reason(symbol, i) is None

    # ---- market-regime gate (market_filter) --------------------------------
    def _append_mf_bar(self, event: BarEvent) -> None:
        """Append/replace one reference-symbol bar. Same shape as `_append_bar`
        but against `_mf_candles`, and it invalidates the compiled-gate memo."""
        ts = pd.Timestamp(event.ts, unit="ms", tz="UTC")
        row = pd.DataFrame(
            {"open": [event.open], "high": [event.high], "low": [event.low],
             "close": [event.close], "volume": [event.volume]}, index=[ts])
        df = self._mf_candles
        if df is None or df.empty:
            self._mf_candles = row
        elif ts in df.index:
            df.loc[ts, ["open", "high", "low", "close", "volume"]] = \
                [event.open, event.high, event.low, event.close, event.volume]
        else:
            self._mf_candles = pd.concat([df, row])
        self._mf_eval_cache = None

    def _mf_at(self, ts_ms: int) -> tuple[MarketFilterEval | None, int]:
        """(compiled gate, reference row index) for the traded bar at `ts_ms`.

        `j` is the reference bar at or BEFORE that timestamp — byte-for-byte the
        engine's `index.searchsorted(ts, side="right") - 1`, which is the whole
        reason the gate cannot look ahead: a reference bar stamped LATER than
        the traded bar is never consulted. Returns `(None, -1)` when there is no
        usable reference bar at all; callers decide what that means (entries
        fail closed, gate-driven exits do not fire)."""
        mf = self.doc.market_filter
        df = self._mf_candles
        if mf is None or df is None or df.empty:
            return None, -1
        ts = pd.Timestamp(ts_ms, unit="ms", tz="UTC")
        j = int(df.index.searchsorted(ts, side="right")) - 1
        if j < 0:
            return None, -1
        key = (len(df), float(df["close"].iloc[-1]))
        if self._mf_eval_cache is None or self._mf_eval_cache[0] != key:
            self._mf_eval_cache = (key, MarketFilterEval(mf, df))
        return self._mf_eval_cache[1], j

    async def _market_ok(self, ts_ms: int, side: Side) -> bool:
        """Does the regime gate permit an entry on `side` at this bar?

        Mirrors `engine.market_ok`. The load-bearing clause is the missing-data
        one: a configured filter with no reference bar FAILS CLOSED. Trading
        unfiltered because the SPY feed hiccuped is precisely the behaviour the
        gate exists to prevent, and it is what 5 leveraged-ETF strategies whose
        entire thesis IS the gate were doing live before this. Logged once per
        run (not once per bar) so the run log distinguishes a blocking gate from
        a broken one without drowning in it."""
        mf = self.doc.market_filter
        if mf is None:
            return True
        if mf.applies_to != "both" and mf.applies_to != side:
            return True
        ev, j = self._mf_at(ts_ms)
        if ev is None:
            if not self._mf_no_data_logged:
                self._mf_no_data_logged = True
                await self._log_event(
                    "market_filter_no_data",
                    {"symbol": mf.symbol, "bar": self.doc.timeframe.bar,
                     "effect": "all gated entries blocked until reference bars arrive"},
                    level="warn")
            return False
        return bool(ev.ok(j))

    async def _market_exit(self, ts_ms: int, side: Side) -> bool:
        """Does the gate's own `exit_when` fire for an open position?

        Mirrors `engine.market_exit`, including its asymmetry with `_market_ok`:
        missing reference data returns False here rather than failing closed.
        Closing a position on absent data would be an ACTION taken on no
        information, whereas refusing to open one is merely inaction — and the
        position keeps its resting broker-side stop/target either way.

        `async` only because the no-data path in `_market_ok` logs; kept
        symmetrical so both gate calls read the same at the call site."""
        mf = self.doc.market_filter
        if mf is None or mf.exit_when is None:
            return False
        if mf.applies_to != "both" and mf.applies_to != side:
            return False
        ev, j = self._mf_at(ts_ms)
        return False if ev is None else bool(ev.exit_now(j))

    # ---- maintenance margin / forced buy-in (audit A8) ----------------------
    def _exit_pending(self, symbol: str) -> bool:
        """Is an exit order for `symbol` already working at the broker?

        The single-exit-per-bar paths do not need this (each returns immediately
        after submitting), but the margin sweep runs across every short on every
        bar, so without it a cover that has not filled yet would be re-submitted
        on the following bar."""
        return any(ctx.purpose == "exit" and ctx.symbol == symbol
                   for ctx in self._orders.values())

    async def _check_maintenance_margin(self) -> None:
        """Forced buy-in: cover EVERY short when account equity falls below
        `risk.short.maintenance_margin_pct` of short market value.

        Mirrors `backtest/engine.py`'s step 3b. This is the unbounded-loss side
        of the book: a runaway short must be closed rather than left to report
        fantasy equity, and until now the engine did that and the live/paper
        runtime did not — so a short that would have been force-covered in the
        backtest ran on live with nothing but its `exit.stop_loss` between it and
        an unbounded loss.

        Equity is computed the engine's way, `cash + Σlong(qty × px) −
        Σshort(qty × px)`, from the broker's cash (which already includes short
        proceeds, so the short leg is subtracted as the liability to buy back)
        and the runner's own bar-close marks. Deliberately NOT
        `account["equity"]`: SimBroker computes that from live quotes, and the
        check has to compare the same numbers the backtest compared.

        Two deliberate differences from the engine, both conservative:
         * the engine evaluates once per timestamp; the runner evaluates on every
           bar of every symbol. Same condition, checked more often.
         * the engine gates on `risk.short.enabled`. That gate is redundant —
           a short can only be opened while the opt-in is on — and it would stop
           protecting a still-open short the moment a user edited the opt-in off,
           so it is not reproduced here. There is no case the engine covers and
           this does not.

        A broker failure while reading cash SKIPS the check rather than guessing
        at equity; it is logged, and the next bar retries."""
        if self.state not in ("RUNNING", "PAUSED"):
            return
        shorts = sorted(s for s, p in self._positions.items() if p.side == "short")
        if not shorts:
            return
        try:
            account = await self.broker.get_account()
        except Exception as e:  # noqa: BLE001 — a check that cannot run must not kill the run
            log.warning("maintenance_margin_check_skipped", run_id=self.run_id, error=str(e))
            return
        equity = float(account.get("cash") or 0.0)
        short_notional = 0.0
        for symbol, position in self._positions.items():
            px = self._last_price.get(symbol) or position.avg_price
            if position.side == "long":
                equity += position.qty * px
            else:
                equity -= position.qty * px
                short_notional += position.qty * px
        threshold = short_notional * self.doc.risk.short.maintenance_margin_pct / 100
        if short_notional <= 0 or equity >= threshold:
            return
        await self._log_event(
            "risk", {"event": "margin_call", "equity": round(equity, 2),
                     "short_notional": round(short_notional, 2),
                     "maintenance_threshold": round(threshold, 2),
                     "covering": shorts}, level="warn")
        for symbol in shorts:
            if self._exit_pending(symbol):
                continue
            position = self._positions.get(symbol)
            if position is None:
                continue
            await self._submit_exit(symbol, position,
                                    self._last_price.get(symbol) or position.avg_price,
                                    reason="margin_call")

    # ---- cross-sectional selection (audit A3) -------------------------------
    def _evaluator_for(self, symbol: str) -> StrategyEvaluator | None:
        """A `StrategyEvaluator` compiled on `symbol`'s current frame, memoised.

        Keyed on (frame length, last close) so an appended bar and an in-place
        replacement of the newest bar both invalidate it. Returns None when the
        frame is too short to evaluate. Before A3 this compile happened inline in
        `_handle_bar` and was thrown away; `selection` needs every symbol's
        metric at ranking time, so keeping the compile is what makes ranking
        free rather than quadratic."""
        df = self._candles.get(symbol)
        if df is None or len(df) < 2:
            return None
        key = (len(df), float(df["close"].iloc[-1]))
        hit = self._eval_cache.get(symbol)
        if hit is not None and hit[0] == key:
            return hit[1]
        evaluator = StrategyEvaluator(self.doc)
        evaluator.compile_symbol(symbol, df)
        self._eval_cache[symbol] = (key, evaluator)
        return evaluator

    async def _record_selection_bar(self, symbol: str, event: BarEvent,
                                    last_bar: bool) -> None:
        """Buffer this symbol as an entry candidate for `event.ts`.

        Nothing is decided here. `selection` is a CROSS-SECTIONAL rule: whether
        this symbol may be entered depends on how the rest of the universe
        scored on the same bar, and when the first symbol's bar for a timestamp
        arrives none of the others have reported it yet. Ranking at that instant
        would score one fresh symbol against 80 stale ones — a different bug
        wearing the fix's clothes."""
        candidates, _ = self._sel_candidates.setdefault(event.ts, ({}, last_bar))
        candidates[symbol] = event

    async def _flush_selection_if_ready(self, ts_ms: int) -> None:
        """Resolve any timestamp whose ranking can now be decided.

        Two triggers, and between them the barrier can neither decide early nor
        wait forever:

        1. **Complete** — every universe symbol has reported a bar at `ts`. This
           is the normal case and fires within seconds of the bar closing, so
           entries are submitted at essentially the same time as before A3.
        2. **Superseded** — a bar for a LATER timestamp arrived, which proves
           `ts` is over and some symbol is simply never going to print a bar for
           it (a halt, a thin name, a dropped subscription). Ranking then uses
           the symbols that DID report, which is exactly what the engine does:
           its `selected_at` scores `index_of[sym].get(ts)` and skips any symbol
           with no bar at that timestamp. So a forced flush is not a degraded
           approximation of the engine — it is the same set.

        Deliberately NO wall-clock timer. Trigger 2 already bounds the wait at
        one bar, and the engine cannot enter on a bar with no successor either
        (its entry pass requires `i < len(df) - 1`), so "no next bar ever
        arrives, no entry happens" is the backtest's own behaviour rather than a
        live-only stall. That removes an asyncio task from the shutdown path,
        which is worth more than shaving a few seconds off a rare edge case."""
        for stale in sorted(t for t in self._sel_candidates if t < ts_ms):
            await self._flush_selection(stale)
        for stale in [t for t in self._sel_reported if t < ts_ms]:
            self._sel_reported.pop(stale, None)
        if ts_ms in self._sel_candidates and \
                self._sel_reported.get(ts_ms, set()) >= set(self.doc.universe.symbols):
            await self._flush_selection(ts_ms)

    async def _flush_selection(self, ts_ms: int) -> None:
        """Rank the universe at `ts_ms` and run the entry path for the winners.

        Candidates are visited in SYMBOL-NAME order, never arrival order: with
        `max_open_positions` binding, whichever eligible symbol is tried first
        gets the slot, so arrival order would make the run irreproducible and
        its paper results non-comparable to its own backtest."""
        self._sel_reported.pop(ts_ms, None)
        entry = self._sel_candidates.pop(ts_ms, None)
        if entry is None:
            return
        candidates, last_bar = entry
        eligible = self._selected_at(ts_ms)
        ts = pd.Timestamp(ts_ms, unit="ms", tz="UTC")
        for symbol in sorted(candidates):
            if eligible is not None and symbol not in eligible:
                continue    # not in this bar's top-N by the selection metric
            df = self._candles.get(symbol)
            evaluator = self._evaluator_for(symbol)
            if df is None or evaluator is None:
                continue
            # The index of the BUFFERED bar, not the newest one: a flush forced
            # by a later bar must still evaluate the rule on the bar that was
            # ranked, exactly as the engine evaluates at its own `i`.
            i = int(df.index.get_indexer([ts])[0])
            if i < 0:
                continue
            await self._try_entry(symbol, candidates[symbol], df, evaluator, i, last_bar)

    def _selected_at(self, ts_ms: int) -> set[str] | None:
        """Symbols eligible to ENTER on this bar, or None when unrestricted.

        Mirrors `engine.selected_at` exactly, including the NaN skip (`v == v`
        rejects warm-up bars rather than ranking them as zero), the empty-set
        return when nothing scored, and the symbol-name tiebreak that makes a
        run deterministic. Consulted for ENTRIES ONLY — a position already open
        in a symbol that has since dropped out of the top-N is NOT closed, again
        matching the engine, whose `eligible` check sits in the entry branch."""
        sel = self.doc.selection
        if sel is None:
            return None
        ts = pd.Timestamp(ts_ms, unit="ms", tz="UTC")
        scored: list[tuple[float, str]] = []
        for symbol in self.doc.universe.symbols:
            df = self._candles.get(symbol)
            evaluator = self._evaluator_for(symbol)
            if df is None or df.empty or evaluator is None:
                continue
            i = int(df.index.get_indexer([ts])[0])
            if i < 0:
                continue    # no bar at this timestamp — the engine skips it too
            try:
                value = evaluator.sym(symbol).operand_at(sel.metric, i)
            except (KeyError, IndexError):
                continue
            if value == value:  # noqa: PLR0124 — NaN check, matches the engine
                scored.append((value, symbol))
        if not scored:
            return set()
        scored.sort(key=lambda t: (-t[0], t[1]) if sel.order == "desc" else (t[0], t[1]))
        return {s for _, s in scored[:sel.top_n]}

    # ---- session clock (session.entry_window / session.flatten_eod) ---------
    @property
    def _intraday(self) -> bool:
        """`session.entry_window` and `session.flatten_eod` are INTRADAY-ONLY
        levers, exactly as in the backtest engine (`engine.intraday`): lint
        warns that a window has no effect on daily bars and rejects
        `flatten_eod` on them outright, so honouring either on a 1d document
        would enforce something the user was told is inert."""
        return self.doc.timeframe.bar != "1d"

    def _in_entry_window(self, ts_ms: int) -> bool:
        """Byte-for-byte the backtest engine's `in_entry_window`: compare the
        bar's ET wall-clock "HH:MM" against the inclusive [start, end] range.
        String comparison is valid because both sides are zero-padded 24h
        HH:MM. No window (or a daily document) means always open."""
        ew = self.doc.session.entry_window
        if not self._intraday or ew is None:
            return True
        local = pd.Timestamp(ts_ms, unit="ms", tz="UTC").tz_convert(ET)
        return ew.start <= f"{local.hour:02d}:{local.minute:02d}" <= ew.end

    def _is_last_bar_of_day(self, ts_ms: int) -> bool:
        """Is the bar STARTING at `ts_ms` the final bar of its session?

        PARITY NOTE — this is the one place the live runtime cannot reproduce
        the backtest engine's method. The engine (`is_last_bar_of_day`) looks
        at the NEXT row of the frame and compares ET dates: it knows the
        future. A live runner sees bars arrive one at a time and cannot know
        whether another one is coming, so it decides from the clock instead: a
        bar is the last of the day when its CLOSE (start + one bar) reaches
        the 16:00 ET regular-session close. For a normal RTH day the two
        definitions select exactly the same bar (the 15:45->16:00 bar of a 15m
        document, the 15:59->16:00 bar of a 1m one).

        They diverge on EARLY-CLOSE days (the 13:00 ET half sessions around
        Thanksgiving/Christmas/July 4th): the engine flattens on the 13:00 bar
        because that is where the data stops, while this rule waits for a
        16:00 bar that never arrives. There is no market calendar in the app
        to consult, and inventing a hardcoded holiday list would risk
        flattening at 13:00 on a FULL day. Instead `_maybe_roll_day` sweeps
        any position still open when a new ET session begins (exit reason
        `eod_late`), so a missed early close costs one overnight hold rather
        than an unbounded one — and is loud in the event log."""
        if not self._intraday:
            return False
        close_ms = ts_ms + BAR_MS[self.doc.timeframe.bar]
        local = pd.Timestamp(close_ms, unit="ms", tz="UTC").tz_convert(ET)
        return (local.hour, local.minute) >= SESSION_CLOSE_ET

    def _flatten_eod(self) -> bool:
        return self._intraday and self.doc.session.flatten_eod

    def _stale_session(self, position: _LivePosition) -> bool:
        """Has this position survived into a LATER ET session than the one it
        was last seen in? Then `flatten_eod` should already have closed it and
        did not — an early-close day (see `_is_last_bar_of_day`), a paused run,
        a rejected eod exit, or a crash across the close. Swept on the first bar
        of the new session with exit reason `eod_late` rather than left to run:
        holding overnight is precisely what the lever forbids.

        Stamps the session on first sight, so a position opened today can never
        read as stale today."""
        if position.session_day is None:
            position.session_day = self._day
            return False
        return position.session_day != self._day

    def _append_bar(self, symbol: str, event: BarEvent) -> None:
        ts = pd.Timestamp(event.ts, unit="ms", tz="UTC")
        df = self._candles.get(symbol)
        if df is None or df.empty:
            self._candles[symbol] = pd.DataFrame(
                {"open": [event.open], "high": [event.high], "low": [event.low],
                 "close": [event.close], "volume": [event.volume]}, index=[ts])
            return
        if ts in df.index:
            df.loc[ts, ["open", "high", "low", "close", "volume"]] = \
                [event.open, event.high, event.low, event.close, event.volume]
        else:
            new_row = pd.DataFrame(
                {"open": [event.open], "high": [event.high], "low": [event.low],
                 "close": [event.close], "volume": [event.volume]}, index=[ts])
            self._candles[symbol] = pd.concat([df, new_row])

    def _maybe_roll_day(self, ts_ms: int) -> None:
        day = pd.Timestamp(ts_ms, unit="ms", tz="UTC").tz_convert(ET).date()
        if self._day is not None and day != self._day:
            self._realized_pnl_today = 0.0
            self._orders_today = 0
            self._entries_today.clear()
        self._day = day

    # ---- entries ------------------------------------------------------------
    async def _submit_entry(self, symbol: str, event: BarEvent, df: pd.DataFrame,
                            evaluator: StrategyEvaluator, i: int,
                            side: Side = "long") -> None:
        # One position per symbol, enforced at the point of submission as well
        # as in `_handle_bar`: holding a long and a short in the same name
        # simultaneously is rejected outright (Entry's schema docstring), and it
        # is precisely the accident a two-sided document invites.
        if symbol in self._positions or symbol in self._pending_entries:
            return
        account = await self.broker.get_account()
        self._account_type = str(account.get("account_type", "") or "").upper()
        equity, cash = account.get("equity", 0.0), account.get("cash", 0.0)
        entry_px = event.close
        stop_hint = None
        sl = self.doc.exit.stop_loss
        if self.doc.sizing.type == "risk_based" and sl:
            atr_value = evaluator.atr_value(symbol, sl.atr_indicator, i) if sl.type == "atr_multiple" else None
            # a short's stop is ABOVE entry; sizing takes the DISTANCE, so the
            # side only has to be right here for the distance to be right
            stop_hint = compute_stop_price(sl, entry_px, atr_value, side=side, kind="stop")
        # AUDIT A8a — CORRECTED. The audit recorded this as "short entry size
        # capped by cash, not margin ... conservative direction, undersizes
        # shorts". Reading `engine.sized_qty` shows the opposite: the engine ALSO
        # caps a short's notional by available cash (it passes the same
        # `available_cash` argument for both sides, then checks margin
        # separately), so capping by cash is already engine-equivalent and is not
        # the divergence. The real one is what "available" means. The engine
        # passes `max(cash - reserved_margin, 0.0)`, holding back the margin
        # committed to open shorts for as long as they are open; the runner
        # passed the broker's RAW cash, which for a short includes the sale
        # proceeds. So the runner OVERSIZED relative to the backtest whenever a
        # short was already open — the dangerous direction, not the conservative
        # one. `_short_margin_committed` restores the engine's meaning.
        available = max(cash - self._short_margin_committed(), 0.0)
        qty = compute_position_size(self.doc.sizing, entry_px, stop_hint, equity, available)
        if side == "short":
            # the engine clamps a short to `max_short_notional` rather than
            # letting it be rejected outright; the gateway also enforces the
            # limit, but clamping here is what makes live take the same (smaller)
            # size the backtest took instead of taking nothing
            cap = self.doc.risk.short.max_short_notional
            if qty * entry_px > cap:
                qty = int(cap // entry_px)
        if qty <= 0:
            await self._log_blocked(symbol, "qty_zero", side=side,
                                    entry_px=round(entry_px, 4),
                                    equity=round(equity, 2), available=round(available, 2))
            return

        # ---- options: express the signal as a contract (plan/16 §9.2) -------
        # `entry_px` is the UNDERLYING price and stays that way: stop/target/
        # trail levels are underlying prices under the default stop_basis, so
        # every downstream calculation is unchanged. Only the instrument being
        # bought and the sizing arithmetic differ.
        if self._opt is not None:
            await self._submit_option_entry(symbol, event, evaluator, i, side,
                                            entry_px, equity, cash)
            return

        order_type, limit_px = "market", None
        spec = self.doc.orders.entry
        if spec.type == "limit":
            order_type, limit_px = "limit", entry_px
        elif spec.type == "marketable_limit":
            # marketable = cross the spread, so the offset goes UP to buy and
            # DOWN to sell short (mirrors the backtest engine)
            off = (spec.offset_bps or 10) / 10_000
            order_type = "limit"
            limit_px = entry_px * (1 + off) if side == "long" else entry_px * (1 - off)

        req = OrderIntentRequest(run_id=self.run_id, symbol=symbol,
                                 side=_entry_order_side(side), qty=qty,
                                 order_type=order_type, purpose="entry", limit_px=limit_px,
                                 reference_price=entry_px, reference_price_age_s=0.0,
                                 max_wait_s=self._order_wait_s())
        result = await self.gateway.submit(req, self.broker, self.risk, self.global_risk)
        if not result.accepted:
            await self._log_event("order", {"event": "entry_rejected", "symbol": symbol,
                                            "side": side, "reason": result.reason})
            return
        self._orders_today += 1
        self._entries_today[symbol] = self._entries_today.get(symbol, 0) + 1
        self._pending_entries.add(symbol)
        self._orders[result.broker_order_id] = _PendingIntent(
            result.intent_id, result.broker_order_id, "entry", symbol, side=side)
        await self._log_event("order", {"event": "entry_submitted", "symbol": symbol,
                                        "side": side, "order_side": req.side, "qty": qty,
                                        "broker_order_id": result.broker_order_id})

    async def _submit_option_entry(self, symbol: str, event: BarEvent,
                                   evaluator: StrategyEvaluator, i: int, side: Side,
                                   underlying_px: float, equity: float,
                                   cash: float) -> None:
        """Select a contract, verify a REAL market exists, size it, and send a
        LIMIT order.

        Every failure here logs a run event with a machine-readable reason.
        A strategy that stops trading must always be able to say why — the
        equity research campaigns lost real time to silent zero-trade
        conditions, and options add many more ways to hit one."""
        ts_ms = event.ts
        side_expr = right_for_side(self.doc.expression, side)
        atr_val = None
        if side_expr is not None and side_expr.strike.by == "atr_multiple":
            atr_val = evaluator.atr_value(symbol, side_expr.strike.atr_indicator, i)

        chosen = await self._opt.select(symbol, side, ts_ms, atr=atr_val)
        if isinstance(chosen, Rejection):
            # `why`, not `reason`: _log_blocked already takes `reason`
            # positionally and a duplicate keyword raises.
            await self._log_blocked(symbol, "contract_selection_rejected",
                                    side=side, why=chosen.reason,
                                    detail=chosen.detail)
            return

        contract = chosen.contract
        quote = self._opt.quote(contract.symbol) or chosen.quote
        ok, why = self._opt.quote_is_tradeable(quote)
        if not ok:
            # NEVER fall back to a modelled price for a live order. A limit
            # derived from Black-Scholes rather than the actual book could sit
            # nowhere near where the contract trades.
            await self._log_blocked(symbol, "option_quote_unusable",
                                    side=side, contract=contract.symbol, why=why)
            return

        # Size off the ASK — what will actually be paid — not the mid.
        entry_premium = transact_price(
            quote, "open", self.doc.expression.pricing.fill_aggression)
        sizing = compute_contract_qty(
            self.doc.sizing, self.doc.risk.options, entry_premium,
            contract.multiplier, quote.greeks.delta, underlying_px, equity,
            max(cash, 0.0),
            premium_at_risk_open=self._opt.premium_at_risk(self._positions),
            commission_per_contract=0.65)
        if sizing.qty <= 0:
            await self._log_blocked(symbol, "qty_zero", side=side,
                                    contract=contract.symbol,
                                    bound_by=sizing.bound_by,
                                    premium=round(entry_premium, 4),
                                    equity=round(equity, 2))
            return

        req = OrderIntentRequest(
            run_id=self.run_id, symbol=contract.symbol,
            side=_option_entry_side(side), qty=sizing.qty,
            # LIMIT always. An option market order on a wide book can fill
            # catastrophically far from mid; the gateway refuses them too.
            order_type="limit", purpose="entry", limit_px=round(entry_premium, 2),
            reference_price=quote.mid, reference_price_age_s=0.0,
            asset_type="OPTION", multiplier=contract.multiplier,
            underlying=symbol, delta=quote.greeks.delta,
            quote_bid=quote.bid, quote_ask=quote.ask,
            underlying_price=underlying_px, max_wait_s=self._order_wait_s())
        result = await self.gateway.submit(req, self.broker, self.risk, self.global_risk)
        if not result.accepted:
            await self._log_event("order", {"event": "entry_rejected", "symbol": symbol,
                                            "contract": contract.symbol,
                                            "side": side, "reason": result.reason})
            return

        self._orders_today += 1
        self._entries_today[symbol] = self._entries_today.get(symbol, 0) + 1
        self._pending_entries.add(symbol)
        ctx = _PendingIntent(result.intent_id, result.broker_order_id, "entry",
                             symbol, side=side)
        ctx.contract_symbol = contract.symbol
        ctx.multiplier = contract.multiplier
        ctx.underlying_px = underlying_px
        ctx.quote_iv = quote.iv
        ctx.quote_delta = quote.greeks.delta
        self._orders[result.broker_order_id] = ctx
        await self._opt.subscribe([contract.symbol])
        # A marketable limit fills essentially at once in reality; ticking the
        # paper broker right here is what reproduces that instead of leaving
        # the order resting forever (see _tick_option).
        await self._tick_option(contract.symbol)
        await self._log_event("order", {
            "event": "entry_submitted", "symbol": symbol,
            "contract": contract.symbol, "contract_label": contract.describe(),
            "side": side, "order_side": req.side, "qty": sizing.qty,
            "limit_px": req.limit_px, "delta": round(quote.greeks.delta, 4),
            "iv": round(quote.iv, 4), "dte": chosen.expiry_dte,
            "premium": round(entry_premium * sizing.qty * contract.multiplier, 2),
            "sized_by": sizing.bound_by,
            "broker_order_id": result.broker_order_id})

    # ---- exits ----------------------------------------------------------------
    async def _submit_exit(self, symbol: str, position: _LivePosition, ref_price: float,
                           reason: str) -> None:
        if position.is_option:
            await self._submit_option_exit(symbol, position, ref_price, reason)
            return
        req = OrderIntentRequest(run_id=self.run_id, symbol=symbol,
                                 side=_close_order_side(position.side), qty=position.qty,
                                 order_type="market", purpose="exit", reference_price=ref_price,
                                 reference_price_age_s=0.0, max_wait_s=10)
        result = await self.gateway.submit(req, self.broker, self.risk, self.global_risk)
        if not result.accepted:
            await self._log_event("order", {"event": "exit_rejected", "symbol": symbol,
                                            "side": position.side, "reason": result.reason})
            return
        self._orders_today += 1
        self._orders[result.broker_order_id] = _PendingIntent(
            result.intent_id, result.broker_order_id, "exit", symbol, side=position.side)
        await self._log_event("order", {"event": "exit_submitted", "symbol": symbol,
                                        "side": position.side, "order_side": req.side,
                                        "qty": position.qty, "exit_reason": reason,
                                        "broker_order_id": result.broker_order_id})

    async def _submit_option_exit(self, symbol: str, position: _LivePosition,
                                  ref_price: float, reason: str) -> None:
        """Close a contract with a LIMIT order at the bid.

        If no usable quote exists the exit is NOT abandoned — a position that
        cannot be closed is the worst outcome here. The order still goes out at
        the last known premium with a wide allowance, and the failure is logged
        loudly so it is visible rather than silent."""
        quote = self._opt.quote(position.contract_symbol)
        if quote is not None:
            limit_px = transact_price(
                quote, "close", self.doc.expression.pricing.fill_aggression)
            bid, ask = quote.bid, quote.ask
        else:
            # Last resort: price off what we paid, marked well down so the order
            # is marketable rather than resting uselessly above the book.
            limit_px = max(position.premium_paid * 0.5, 0.01)
            bid = ask = None
            await self._log_event(
                "order",
                {"event": "option_exit_without_quote", "symbol": symbol,
                 "contract": position.contract_symbol,
                 "note": "no live quote; exiting at a conservative limit"},
                level="warning")

        req = OrderIntentRequest(
            run_id=self.run_id, symbol=position.contract_symbol,
            side="sell_to_close", qty=position.qty, order_type="limit",
            purpose="exit", limit_px=round(limit_px, 2),
            reference_price=quote.mid if quote else position.premium_paid,
            reference_price_age_s=0.0, asset_type="OPTION",
            multiplier=position.multiplier, underlying=symbol,
            quote_bid=bid, quote_ask=ask, underlying_price=ref_price, max_wait_s=10)
        result = await self.gateway.submit(req, self.broker, self.risk, self.global_risk)
        if not result.accepted:
            await self._log_event("order", {"event": "exit_rejected", "symbol": symbol,
                                            "contract": position.contract_symbol,
                                            "reason": result.reason}, level="warning")
            return
        self._orders_today += 1
        self._orders[result.broker_order_id] = _PendingIntent(
            result.intent_id, result.broker_order_id, "exit", symbol,
            side=position.side)
        await self._log_event("order", {
            "event": "exit_submitted", "symbol": symbol,
            "contract": position.contract_symbol, "side": position.side,
            "order_side": req.side, "qty": position.qty, "exit_reason": reason,
            "limit_px": req.limit_px,
            "broker_order_id": result.broker_order_id})
        await self._tick_option(position.contract_symbol)

    # ---- option lifecycle (plan/16 §9.3) --------------------------------------
    async def _check_option_expiry(self, ts_ms: int) -> None:
        """Force-close any contract at or past `close_at_dte`.

        Runs on every bar. Never hold through expiration (plan/16 D9): gamma
        explodes, spreads widen and assignment mechanics take over — none of
        which this system models."""
        if self._opt is None:
            return
        for symbol in list(self._positions):
            pos = self._positions[symbol]
            if not pos.is_option or pos.contract_expiry is None:
                continue
            if self._exit_pending(symbol):
                continue
            dte = self._opt.dte(pos.contract_expiry, ts_ms)
            if dte <= self.doc.expression.exit.close_at_dte:
                ref = self._last_close(symbol) or pos.underlying_entry_px
                await self._log_event("position", {
                    "event": "dte_close", "symbol": symbol,
                    "contract": pos.contract_symbol, "dte": dte,
                    "close_at_dte": self.doc.expression.exit.close_at_dte})
                await self._submit_exit(symbol, pos, ref, "dte_close")

    async def _check_option_underlying_stops(self, event: BarEvent) -> None:
        """Evaluate UNDERLYING-based stops and targets for option positions.

        This is the in-process half of plan/16 §9.4. For a SHARE position the
        protective stop rests at the broker and triggers without us; for an
        option position whose stop is expressed in underlying terms there is no
        such order type — no broker accepts "sell this call if SPY drops below
        580" — so the trigger has to be evaluated here, on each bar, against
        the underlying's own high/low.

        The comparisons are deliberately the same ones the backtest engine
        makes (bar low breaches a long's stop, bar high breaches its target,
        mirrored for a short), so live and backtest fire on identical bars.
        What differs is only that this runs in our process, which is why the
        premium backstop rests at the broker as well."""
        if self._opt is None:
            return
        pos = self._positions.get(event.symbol)
        if pos is None or not pos.is_option:
            return
        if self._exit_pending(event.symbol):
            return
        if self.doc.expression.exit.stop_basis != "underlying":
            return

        is_long = pos.side == "long"
        # the trail, when armed, tightens the stop the same way it does for
        # shares — it is an underlying level in both cases
        stop = pos.stop_px
        if pos.trail_px is not None:
            stop = (max(stop, pos.trail_px) if stop is not None else pos.trail_px) \
                if is_long else \
                (min(stop, pos.trail_px) if stop is not None else pos.trail_px)

        if stop is not None:
            breached = event.low <= stop if is_long else event.high >= stop
            if breached:
                await self._log_event("position", {
                    "event": "underlying_stop_hit", "symbol": event.symbol,
                    "contract": pos.contract_symbol, "stop_px": round(stop, 4),
                    "bar_low": event.low, "bar_high": event.high})
                await self._submit_exit(event.symbol, pos, event.close, "stop")
                return

        tp = self.doc.exit.take_profit
        if tp is not None:
            atr_value = self._atr_value_for(event.symbol, tp)
            target = compute_stop_price(tp, pos.underlying_entry_px, atr_value,
                                        side=pos.side, kind="target")
            if target is not None:
                reached = event.high >= target if is_long else event.low <= target
                if reached:
                    await self._log_event("position", {
                        "event": "underlying_target_hit", "symbol": event.symbol,
                        "contract": pos.contract_symbol,
                        "target_px": round(target, 4)})
                    await self._submit_exit(event.symbol, pos, event.close, "target")

    async def _tick_held_options(self) -> None:
        """Push a fresh quote for every held contract, so a resting premium
        backstop can trigger in paper the way it would at the broker."""
        if self._opt is None:
            return
        for pos in list(self._positions.values()):
            if pos.is_option:
                await self._tick_option(pos.contract_symbol)

    async def _check_option_risk(self) -> None:
        """Portfolio delta/vega caps, checked per bar like maintenance margin."""
        if self._opt is None or not self._positions:
            return
        breaches = self._opt.breaches(self._positions)
        if breaches:
            await self._log_event("risk", {"event": "option_risk_breach",
                                           "breaches": breaches}, level="warning")

    def _last_close(self, symbol: str) -> float | None:
        df = self._candles.get(symbol)
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])

    async def _tick_option(self, contract_symbol: str | None) -> None:
        """Feed a contract's live quote into a PAPER broker.

        RunManager drives `process_tick` from LEVELONE_EQUITIES for the run's
        underlyings, but option contracts quote on LEVELONE_OPTIONS and are
        never in that set — so without this a paper option order (always a
        LIMIT, per plan/16 §11.3) would sit WORKING forever and no position
        would ever open or close. Called right after every option order is
        submitted, and on every bar for held contracts so a resting premium
        backstop can actually trigger.

        No-op for a live broker: SchwabBroker does not implement process_tick,
        and a live run's fills come from its own polling/ACCT_ACTIVITY path."""
        if self._opt is None or contract_symbol is None:
            return
        if getattr(self.broker, "mode", "") != "paper":
            return
        tick = getattr(self.broker, "process_tick", None)
        if tick is None:
            return
        q = self._opt.quote(contract_symbol)
        if q is None:
            return
        await tick(contract_symbol, q.bid, q.ask, q.mid)

    # ---- broker events (fills/cancels/rejects) ---------------------------------
    async def _handle_broker_event(self, event: BrokerEvent) -> None:
        ctx = self._orders.get(event.broker_order_id)
        if ctx is None:
            return
        if event.kind == "fill":
            self._orders.pop(event.broker_order_id, None)
            if ctx.purpose == "entry":
                await self._on_entry_filled(event, ctx)
            elif ctx.purpose == "pyramid":
                await self._on_add_filled(event, ctx)
            elif ctx.purpose == "scale_out":
                await self._on_scale_out_filled(event, ctx)
            elif ctx.purpose in ("stop", "target"):
                await self._on_position_closed(event, ctx, cancel_siblings_of=ctx.purpose)
            elif ctx.purpose == "exit":
                await self._on_position_closed(event, ctx, cancel_siblings_of=None)
        elif event.kind in ("canceled", "rejected"):
            self._orders.pop(event.broker_order_id, None)
            if ctx.purpose == "entry":
                self._pending_entries.discard(ctx.symbol)
            elif ctx.purpose == "pyramid":
                # the ladder is left exactly where it was, so the same level can
                # trigger again on a later bar
                self._pending_adds.discard(ctx.symbol)
            await self._log_event("order", {"event": event.kind, "symbol": ctx.symbol,
                                            "purpose": ctx.purpose, "reason": event.reason,
                                            "broker_order_id": event.broker_order_id})

    async def _on_entry_filled(self, event: BrokerEvent, ctx: _PendingIntent) -> None:
        if ctx.contract_symbol is not None:
            await self._on_option_entry_filled(event, ctx)
            return
        fill = event.fill
        side = ctx.side
        position = _LivePosition(symbol=ctx.symbol, qty=fill.qty, avg_price=fill.price,
                                 opened_at=int(fill.ts * 1000), side=side,
                                 original_qty=fill.qty,
                                 high_water=fill.price, low_water=fill.price)
        self._init_trail(position, ctx.symbol, fill.price)
        self._init_pyramid(position, ctx.symbol, fill.price, fill.qty)
        self._positions[ctx.symbol] = position
        self._pending_entries.discard(ctx.symbol)
        await self._log_event("fill", {"symbol": ctx.symbol, "side": side,
                                       "order_side": _entry_order_side(side),
                                       "qty": fill.qty, "price": fill.price})
        await self._upsert_position_row(position)

        e = self.doc.exit
        # Scale-out levels are sized off the entry fill and placed FIRST, so
        # the final take_profit below can claim only the shares left over —
        # the resting orders must never sum to more than the position.
        levels = compute_scale_levels(
            e.scale_outs, fill.price, int(fill.qty),
            lambda spec: self._atr_value_for(ctx.symbol, spec), side=side)
        scaled_shares = sum(shares for _, shares in levels)

        # Direction is a PARAMETER, not a fork (strategy/stops.py): for a short
        # the stop rests ABOVE the fill and the target BELOW it, and both are
        # buy_to_cover orders rather than sells.
        if e.stop_loss:
            atr_value = self._atr_value_for(ctx.symbol, e.stop_loss)
            px = compute_stop_price(e.stop_loss, fill.price, atr_value, side=side, kind="stop")
            position.stop_broker_order_id = await self._place_protective(
                ctx.symbol, fill.qty, "stop", px, fill.price, side=side)
            position.stop_px = px
        for level_px, shares in levels:
            oid = await self._place_protective(
                ctx.symbol, shares, "scale_out", level_px, fill.price, side=side)
            if oid is not None:
                position.scale_order_qty[oid] = shares
        if e.take_profit:
            atr_value = self._atr_value_for(ctx.symbol, e.take_profit)
            px = compute_stop_price(e.take_profit, fill.price, atr_value,
                                    side=side, kind="target")
            position.target_broker_order_id = await self._place_protective(
                ctx.symbol, fill.qty - scaled_shares, "target", px, fill.price, side=side)
        await self._upsert_position_row(position)

        if (e.stop_loss and position.stop_broker_order_id is None) or \
                (e.take_profit and position.target_broker_order_id is None):
            # A configured protective order failed to place — this is a
            # serious condition (plan 06: stops must always rest at the
            # broker). Raise so the outer loop marks ERROR and halts rather
            # than continuing with an under-protected position.
            raise RuntimeError(f"failed to place a configured protective order for {ctx.symbol}")

    async def _on_option_entry_filled(self, event: BrokerEvent,
                                      ctx: _PendingIntent) -> None:
        """Build the option position and place its BROKER-SIDE BACKSTOP.

        That backstop is the whole reason this method is not a branch inside
        `_on_entry_filled`. Under the default `stop_basis: underlying` there is
        no order type that says "sell this call if SPY drops below 580" — the
        underlying stop must be monitored in-process, and therefore depends on
        this app staying alive. `max_loss_pct_of_premium` rests at the broker
        as a premium stop so that a crash leaves the position capped rather
        than unmanaged (plan/16 §9.4). Combined with the fact that a long
        option cannot lose more than its premium, that is what makes the
        in-process stop acceptable at all."""
        fill = event.fill
        side = ctx.side
        occ = parse_occ(ctx.contract_symbol)
        position = _LivePosition(
            symbol=ctx.symbol, qty=fill.qty, avg_price=fill.price,
            opened_at=int(fill.ts * 1000), side=side, original_qty=fill.qty,
            high_water=ctx.underlying_px, low_water=ctx.underlying_px,
            contract_symbol=ctx.contract_symbol, contract_right=occ.right,
            contract_strike=float(occ.strike), contract_expiry=occ.expiry,
            multiplier=ctx.multiplier, premium_paid=fill.price,
            entry_iv=ctx.quote_iv, entry_delta=ctx.quote_delta,
            underlying_entry_px=ctx.underlying_px)
        # Stops/targets/trails are UNDERLYING levels under the default basis,
        # so they are initialised from the underlying price exactly as they are
        # for a share position — this is decision D1 paying off again.
        self._init_trail(position, ctx.symbol, ctx.underlying_px)
        self._positions[ctx.symbol] = position
        self._pending_entries.discard(ctx.symbol)
        await self._log_event("fill", {
            "symbol": ctx.symbol, "contract": ctx.contract_symbol,
            "side": side, "order_side": _option_entry_side(side),
            "qty": fill.qty, "price": fill.price,
            "premium": round(fill.price * fill.qty * ctx.multiplier, 2)})
        await self._upsert_position_row(position)

        e = self.doc.exit
        if e.stop_loss:
            # The in-process underlying stop level. Nothing rests at the broker
            # for this one; `_handle_bar` evaluates it each bar.
            atr_value = self._atr_value_for(ctx.symbol, e.stop_loss)
            position.stop_px = compute_stop_price(
                e.stop_loss, ctx.underlying_px, atr_value, side=side, kind="stop")

        backstop_px = self._opt.max_loss_stop_px(fill.price)
        if backstop_px is not None:
            position.stop_broker_order_id = await self._place_option_backstop(
                ctx.symbol, position, fill.qty, backstop_px)
        await self._upsert_position_row(position)

        if backstop_px is not None and position.stop_broker_order_id is None:
            # Same invariant as the equity path: a configured protective order
            # that fails to place is a serious condition, not a warning.
            raise RuntimeError(
                f"failed to place the option loss backstop for {ctx.symbol}")

    async def _place_option_backstop(self, symbol: str, position: _LivePosition,
                                     qty: float, stop_px: float) -> str | None:
        """A resting premium STOP that caps the loss at the broker."""
        req = OrderIntentRequest(
            run_id=self.run_id, symbol=position.contract_symbol,
            side="sell_to_close", qty=qty, order_type="stop", purpose="stop",
            stop_px=stop_px, reference_price=position.premium_paid,
            reference_price_age_s=0.0, asset_type="OPTION",
            multiplier=position.multiplier, underlying=symbol, max_wait_s=10)
        result = await self.gateway.submit(req, self.broker, self.risk,
                                           self.global_risk)
        if not result.accepted:
            await self._log_event("order", {
                "event": "option_backstop_rejected", "symbol": symbol,
                "contract": position.contract_symbol, "reason": result.reason},
                level="error")
            return None
        self._orders[result.broker_order_id] = _PendingIntent(
            result.intent_id, result.broker_order_id, "stop", symbol,
            side=position.side, contract_symbol=position.contract_symbol,
            multiplier=position.multiplier)
        await self._log_event("order", {
            "event": "option_backstop_placed", "symbol": symbol,
            "contract": position.contract_symbol, "stop_px": stop_px,
            "pct_of_premium": self.doc.expression.exit.max_loss_pct_of_premium,
            "broker_order_id": result.broker_order_id})
        return result.broker_order_id

    # ---- short buying power (audit A8a) --------------------------------------
    def _short_margin_committed(self) -> float:
        """Margin committed to currently-open shorts, the engine's
        `reserved_margin`.

        The engine reserves `notional × initial_margin_pct / 100` at the entry
        price when a short opens and holds it until the short closes, so that
        reserve is not spendable on the next position. It marks the reserve at
        ENTRY price and never re-marks it, which is why this uses `avg_price`
        rather than the last trade — re-marking would be a different (and more
        aggressive) rule than the one the backtest ran."""
        pct = self.doc.risk.short.initial_margin_pct / 100
        return sum(position.qty * position.avg_price * pct
                   for position in self._positions.values()
                   if position.side == "short")

    # ---- orders.unfilled_timeout_sec (audit A7) ------------------------------
    def _order_wait_s(self) -> int:
        """`orders.unfilled_timeout_sec`, clamped to the gateway's 30s ceiling.

        The clamp itself is defensible — `max_wait_s` is how long the gateway
        will BLOCK waiting on the rate limiter, and blocking a runner's event
        loop for 300s would stall every other symbol it trades. The SILENCE was
        not: a user who configured 300s got 30s with no warning and no log line,
        so the field looked honoured. It is now logged once per run, naming both
        numbers, and `orders.exit` is reported as unimplemented by the
        live-support gap list rather than being quietly ignored."""
        return min(self.doc.orders.unfilled_timeout_sec, ORDER_WAIT_CEILING_S)

    # ---- pyramiding / scale-in (entry.pyramid, audit A6) ---------------------
    def _init_pyramid(self, position: _LivePosition, symbol: str,
                      entry_px: float, qty: float) -> None:
        """Arm the first add from the original entry's fill.

        Mirrors the engine's post-fill block: `add_qty` is a fraction of the
        ORIGINAL entry size, and the first add triggers a favourable `spacing`
        move from this fill — `kind="target"` because the add level sits on the
        profitable side, which is what makes this scale-IN rather than
        averaging down."""
        py = self.doc.entry.pyramid
        if py is None:
            return
        position.add_qty = int(qty * py.size_pct / 100)
        if position.add_qty <= 0:
            return
        position.next_add_px = compute_stop_price(
            py.spacing, entry_px, self._atr_value_for(symbol, py.spacing),
            side=position.side, kind="target")

    async def _maybe_pyramid(self, symbol: str, position: _LivePosition,
                             event: BarEvent) -> None:
        """Submit a scale-IN add if this bar reached the next add level.

        Engine parity notes:
         * the level is reached on a TOUCH (the bar's high for a long, its low
           for a short), the same test the engine applies;
         * only ONE add per bar, whereas the engine's `while` loop can consume
           several levels within a single bar. The runner cannot: each add's
           level is derived from the PREVIOUS add's actual fill price, and live
           that fill has not happened yet. Taking one add per bar is the
           conservative reading and the only one that keeps the spacing honest;
         * a bar that exits the position never also adds to it — every exit path
           above returns before reaching here.

        One further interaction worth knowing about: equal-sized adds are
        IDENTICAL intents, so two of them inside the gateway's
        `DUPLICATE_WINDOW_S` (10s) are suppressed as duplicates. Bars are at
        least a minute apart, so this cannot bite a real run — but it does mean
        a test cannot drive two adds back to back.

        The affordability check the engine performs inline is left to the
        gateway and the broker: `max_single_order_notional`, buying power and
        (for a short) the margin requirement are all re-checked there, and a
        rejected add simply leaves the ladder where it was."""
        py = self.doc.entry.pyramid
        if py is None or position.next_add_px is None or position.add_qty <= 0:
            return
        if position.adds_done >= py.max_adds or symbol in self._pending_adds:
            return
        level = position.next_add_px
        reached = event.high >= level if position.side == "long" else event.low <= level
        if not reached:
            return
        req = OrderIntentRequest(
            run_id=self.run_id, symbol=symbol, side=_entry_order_side(position.side),
            qty=position.add_qty, order_type="market", purpose="pyramid",
            reference_price=event.close, reference_price_age_s=0.0,
            max_wait_s=self._order_wait_s())
        result = await self.gateway.submit(req, self.broker, self.risk, self.global_risk)
        if not result.accepted:
            await self._log_event("order", {"event": "pyramid_rejected", "symbol": symbol,
                                            "side": position.side, "level": round(level, 4),
                                            "reason": result.reason})
            return
        self._orders_today += 1
        self._pending_adds.add(symbol)
        self._orders[result.broker_order_id] = _PendingIntent(
            result.intent_id, result.broker_order_id, "pyramid", symbol, side=position.side)
        await self._log_event("order", {"event": "pyramid_submitted", "symbol": symbol,
                                        "side": position.side, "qty": position.add_qty,
                                        "level": round(level, 4),
                                        "add_number": position.adds_done + 1,
                                        "broker_order_id": result.broker_order_id})

    async def _on_add_filled(self, event: BrokerEvent, ctx: _PendingIntent) -> None:
        """Merge a filled add into the position and re-base its protection.

        The position becomes ONE holding at the volume-weighted average price,
        and the stop/target are recomputed from that new average — the
        conservative choice the engine makes too: leaving the original stop in
        place would have a now-more-distant stop protecting a much larger
        position. Scale-out levels are deliberately NOT re-derived, matching the
        engine, which leaves its `scale_levels` untouched on an add."""
        fill = event.fill
        self._pending_adds.discard(ctx.symbol)
        position = self._positions.get(ctx.symbol)
        if position is None:
            # the position closed between submitting the add and its fill; the
            # add is now an unintended naked position, so flatten it rather than
            # adopt it
            await self._log_event("order", {"event": "pyramid_orphaned",
                                            "symbol": ctx.symbol, "qty": fill.qty},
                                  level="warn")
            return
        total = position.qty + fill.qty
        position.avg_price = (position.avg_price * position.qty
                              + fill.price * fill.qty) / total
        position.qty = total
        position.original_qty += fill.qty
        position.adds_done += 1
        py = self.doc.entry.pyramid
        position.next_add_px = (
            compute_stop_price(py.spacing, fill.price,
                               self._atr_value_for(ctx.symbol, py.spacing),
                               side=position.side, kind="target")
            if py is not None and position.adds_done < py.max_adds else None)
        await self._log_event("fill", {"symbol": ctx.symbol, "side": position.side,
                                       "event": "pyramid_filled", "qty": fill.qty,
                                       "price": fill.price,
                                       "new_qty": position.qty,
                                       "new_avg_price": round(position.avg_price, 4),
                                       "adds_done": position.adds_done})
        self._rebase_trail(position, ctx.symbol)
        await self._rebase_protective(ctx.symbol, position)
        await self._upsert_position_row(position)

    def _rebase_trail(self, position: _LivePosition, symbol: str) -> None:
        """Re-derive the trail distance and activation gate from the new average
        entry, WITHOUT disarming a trail that has already armed — the engine's
        rule exactly. An armed trail is re-seeded a distance from the new
        average; an unarmed one keeps resting at no price."""
        if self.doc.exit.trailing_stop is None:
            return
        was_armed = position.trail_armed
        self._init_trail(position, symbol, position.avg_price)
        if was_armed:
            position.trail_armed = True
            if position.trail_dist is not None:
                position.trail_px = (position.avg_price - position.trail_dist
                                     if position.side == "long"
                                     else position.avg_price + position.trail_dist)

    async def _rebase_protective(self, symbol: str, position: _LivePosition) -> None:
        """Cancel and re-place the resting stop and target for the new size and
        the new average price. Same non-atomic caveat as
        `_replace_protective_stop`, and the same invariant: a configured
        protective order that fails to re-place halts the run rather than
        leaving a larger position under-protected."""
        e = self.doc.exit
        if e.stop_loss:
            px = compute_stop_price(e.stop_loss, position.avg_price,
                                    self._atr_value_for(symbol, e.stop_loss),
                                    side=position.side, kind="stop")
            await self._replace_protective_stop(symbol, position, px)
        old_target = position.target_broker_order_id
        if old_target is not None:
            self._orders.pop(old_target, None)
            await self.broker.cancel(old_target)
            position.target_broker_order_id = None
        if e.take_profit:
            px = compute_stop_price(e.take_profit, position.avg_price,
                                    self._atr_value_for(symbol, e.take_profit),
                                    side=position.side, kind="target")
            scaled = sum(position.scale_order_qty.values())
            position.target_broker_order_id = await self._place_protective(
                symbol, position.qty - scaled, "target", px, position.avg_price,
                count_toward_daily=False, side=position.side)
            if position.target_broker_order_id is None:
                raise RuntimeError(
                    f"failed to re-place the take-profit for {symbol} after a scale-in")

    # ---- trailing stop --------------------------------------------------------
    def _init_trail(self, position: _LivePosition, symbol: str, entry_px: float) -> None:
        """Seed trail state from `exit.trailing_stop` at entry (or, on recovery,
        from a reconstructed entry price)."""
        spec = self.doc.exit.trailing_stop
        if spec is None:
            return
        side = _position_side(position)
        t = compute_stop_price(spec, entry_px, self._atr_value_for(symbol, spec),
                               side=side, kind="stop")
        if t is None:            # atr_multiple still warming up
            return
        position.trail_dist = abs(entry_px - t)
        act = spec.activate_at
        position.trail_activate_px = compute_trail_activation_px(
            spec, entry_px,
            self._atr_value_for(symbol, act) if act is not None else None, side=side)
        position.trail_armed = position.trail_activate_px is None
        if not position.trail_armed:
            # a gated trail rests at NO price until it arms — only stop_loss
            # protects until then (same as the backtest engine)
            position.trail_px = None
        else:
            position.trail_px = (entry_px - position.trail_dist if side == "long"
                                 else entry_px + position.trail_dist)

    async def _recover_trail(self, symbol: str, position: _LivePosition) -> None:
        """Rebuild trailing-stop state for a position recovered from its
        `positions` DB row after a process crash, and amend the resting stop to
        the reconstructed trail level.

        The row carries symbol/qty/avg_price/opened_at and nothing else — the
        high-water mark and whether the trail had already armed are NOT
        journaled. The reconstruction chosen here, and why:

        * `trail_dist` is re-derived from `exit.trailing_stop` against the
          recovered `avg_price`, exactly as `_init_trail` does at entry. The
          distance is a pure function of the spec and the entry price, so this
          is exact, not an approximation.
        * The high/low-water mark is recovered from the candle history the
          runner has ALREADY loaded during warm-up (`self._candles[symbol]`),
          restricted to bars at or after `position.opened_at`. That data is
          genuinely available at recovery time and yields the TRUE water mark
          the pre-crash process would have seen, so the rebuilt trail sits
          where it belonged rather than where a guess would put it.
        * If there is no usable history (no frame, or nothing since
          `opened_at`) it falls back to `avg_price` — i.e. treats the trail as
          never having moved off entry. That protects LESS aggressively than
          reality, which is the safe direction: it can only ever leave the stop
          further away than it truly was, never invent a tighter stop than the
          data supports (a fabricated tighter stop would be an immediate
          unwanted exit).
        * Arming is recomputed from that reconstructed water mark via
          `trail_is_armed`, so it is never *assumed* armed. Because
          `high_water`/`low_water` are the real excursion, a trail whose
          activation price was demonstrably reached stays armed — the
          reconstruction can't silently disarm it.

        Failure to place the resulting stop raises, the same invariant
        `_recover_position` and `_on_entry_filled` hold: a position that is
        configured to carry a stop and doesn't is a halt-the-run condition."""
        if self.doc.exit.trailing_stop is None:
            return
        side = _position_side(position)
        self._init_trail(position, symbol, position.avg_price)
        if position.trail_dist is None:
            return                      # atr_multiple still warming up
        high, low = self._water_marks_since(symbol, position.opened_at, position.avg_price)
        position.high_water, position.low_water = high, low
        if not position.trail_armed:
            position.trail_armed = trail_is_armed(
                position.trail_activate_px, high, low, side=side)
            if not position.trail_armed:
                await self._log_event(
                    "recovery", {"event": "trail_not_armed", "symbol": symbol,
                                 "activate_px": position.trail_activate_px,
                                 "high_water": high, "low_water": low})
                return
        position.trail_px = compute_trail_px(
            None, position.trail_dist, high, low, side=side)
        await self._log_event(
            "recovery", {"event": "trail_reconstructed", "symbol": symbol,
                         "trail_px": round(position.trail_px, 4),
                         "high_water": high, "low_water": low,
                         "from_history": high != position.avg_price or low != position.avg_price})
        new_stop = self._tighter_stop(position.stop_px, position.trail_px, side)
        if self._stop_moved(position.stop_px, new_stop, side):
            await self._replace_protective_stop(symbol, position, new_stop)

    def _water_marks_since(self, symbol: str, opened_at_ms: int,
                           fallback_px: float) -> tuple[float, float]:
        """(high_water, low_water) over the loaded candle history from
        `opened_at_ms` onward, never worse than `fallback_px` (the entry price
        is itself part of the excursion). Falls back to the entry price when
        there is no usable history — see `_recover_trail` for why that
        direction is the safe one."""
        df = self._candles.get(symbol)
        if df is None or df.empty:
            return fallback_px, fallback_px
        try:
            window = df[df.index >= pd.Timestamp(opened_at_ms, unit="ms", tz="UTC")]
        except TypeError:               # tz-naive index — compare naively
            window = df[df.index >= pd.Timestamp(opened_at_ms, unit="ms")]
        if window.empty:
            return fallback_px, fallback_px
        return (max(fallback_px, float(window["high"].max())),
                min(fallback_px, float(window["low"].min())))

    @staticmethod
    def _tighter_stop(stop_px: float | None, trail_px: float, side: Side) -> float:
        """The stop that rests at the broker: the TIGHTER of the fixed stop_loss
        and the trail — highest for a long, lowest for a short."""
        if stop_px is None:
            return trail_px
        return max(stop_px, trail_px) if side == "long" else min(stop_px, trail_px)

    @staticmethod
    def _stop_moved(old_px: float | None, new_px: float, side: Side) -> bool:
        """Has the ratchet actually moved the resting stop? A long's stop only
        ever rises, a short's only ever falls, so "moved" is direction-aware."""
        if old_px is None:
            return True
        return new_px > old_px + 1e-6 if side == "long" else new_px < old_px - 1e-6

    async def _maintain_trail(self, symbol: str, position: _LivePosition,
                              high: float, low: float) -> None:
        """Bar-close trail maintenance: update the high-water mark, arm the
        trail if the activation level was reached, and AMEND the resting stop
        order when the ratchet moves it up.

        Called AFTER this bar's exit checks, mirroring the backtest engine's
        ordering (engine.py: "trailing update AFTER exit checks"), so the bar
        that arms a trail can never also be stopped out by it."""
        if position.trail_dist is None:
            return
        side = _position_side(position)
        position.high_water = max(position.high_water, high)
        position.low_water = min(position.low_water, low)
        if not position.trail_armed:
            position.trail_armed = trail_is_armed(
                position.trail_activate_px, position.high_water, position.low_water, side=side)
            if not position.trail_armed:
                return
            await self._log_event("order", {"event": "trail_armed", "symbol": symbol,
                                            "activate_px": position.trail_activate_px})
        position.trail_px = compute_trail_px(
            position.trail_px, position.trail_dist,
            position.high_water, position.low_water, side=side)

        new_stop = self._tighter_stop(position.stop_px, position.trail_px, side)
        if not self._stop_moved(position.stop_px, new_stop, side):
            return                              # ratchet hasn't moved it
        await self._replace_protective_stop(symbol, position, new_stop)

    async def _replace_protective_stop(self, symbol: str, position: _LivePosition,
                                       new_px: float) -> None:
        """Cancel the resting stop and place a replacement at `new_px`.

        NOT atomic: there is a window between the cancel and the new placement
        in which the position rests unprotected. That is accepted for SimBroker
        (whose `replace` is itself cancel-then-place); a real broker adapter
        should use a native replace instead. If the re-place fails we raise, on
        the same invariant as `_on_entry_filled` — a position that is supposed
        to carry a stop and doesn't is a halt-the-run condition, never
        something to continue past."""
        old_id = position.stop_broker_order_id
        if old_id is not None:
            self._orders.pop(old_id, None)   # not a real exit; don't treat the cancel as one
            await self.broker.cancel(old_id)
        position.stop_broker_order_id = None
        new_id = await self._place_protective(symbol, position.qty, "stop", new_px,
                                              position.avg_price, count_toward_daily=False,
                                              side=position.side)
        if new_id is None:
            raise RuntimeError(
                f"failed to re-place the trailing protective stop for {symbol} — "
                f"position is unprotected")
        position.stop_broker_order_id = new_id
        position.stop_px = new_px
        await self._log_event("order", {"event": "trail_stop_moved", "symbol": symbol,
                                        "side": position.side, "stop_px": round(new_px, 4),
                                        "broker_order_id": new_id})
        await self._upsert_position_row(position)

    def _atr_value_for(self, symbol: str, spec) -> float | None:
        """ATR at the most recent available bar for an atr_multiple stop/target
        spec. Called at FILL time (not signal time), where no evaluator is in
        scope — a real bug (fixed here) had this always pass atr_value=None,
        which made every atr_multiple protective order price out as None and
        then crash the runner via the "protective order must place" invariant
        in `_on_entry_filled`, on literally the first fill of any strategy
        configured with an atr_multiple stop_loss/take_profit. No test used
        atr_multiple for the runner, only percent/fixed_price_offset, so it
        shipped unnoticed."""
        if spec is None or spec.type != "atr_multiple":
            return None
        df = self._candles.get(symbol)
        if df is None or df.empty:
            return None
        evaluator = StrategyEvaluator(self.doc)
        evaluator.compile_symbol(symbol, df)
        return evaluator.atr_value(symbol, spec.atr_indicator, len(df) - 1)

    async def _place_protective(self, symbol: str, qty: float, purpose: str,
                                px: float | None, ref_price: float,
                                count_toward_daily: bool = True,
                                side: Side = "long") -> str | None:
        """`side` is the POSITION's side. Every protective order CLOSES the
        position, so a long's rest as `sell` and a short's as `buy_to_cover` —
        which also means the gateway's cover clamp sees them, and SimBroker
        triggers a short's stop on STRENGTH rather than weakness."""
        if px is None:  # e.g. atr_multiple with NaN ATR during warm-up
            await self._log_event("order", {"event": "protective_skipped", "symbol": symbol,
                                            "purpose": purpose, "side": side,
                                            "reason": "no price (warm-up?)"},
                                  level="warning")
            return None
        is_stop = purpose == "stop"
        order_type = "stop" if is_stop else "limit"   # target/scale_out rest as limits
        req = OrderIntentRequest(
            run_id=self.run_id, symbol=symbol, side=_close_order_side(side), qty=qty,
            order_type=order_type,
            purpose=purpose, stop_px=px if is_stop else None,
            limit_px=None if is_stop else px,
            reference_price=ref_price, reference_price_age_s=0.0, max_wait_s=10)
        result = await self.gateway.submit(req, self.broker, self.risk, self.global_risk)
        if not result.accepted:
            await self._log_event("order", {"event": f"{purpose}_rejected", "symbol": symbol,
                                            "side": side, "reason": result.reason},
                                  level="error")
            return None
        # a trailing amendment replaces protection that already exists — it adds
        # no new exposure, and counting it would let a ratcheting stop exhaust
        # `risk.max_daily_orders` and thereby BLOCK a genuine exit
        if count_toward_daily:
            self._orders_today += 1
        self._orders[result.broker_order_id] = _PendingIntent(
            result.intent_id, result.broker_order_id, purpose, symbol)
        # AUDIT C2 — only FAILURES used to log, so the run event log a user reads
        # in RunDetail.tsx never once confirmed that a position was protected.
        # Absence of a `protective_skipped` line is not evidence a stop rests at
        # the broker; this is. `_submit_entry` has always logged its own success,
        # so the asymmetry was the bug.
        await self._log_event("order", {"event": "protective_placed", "symbol": symbol,
                                        "purpose": purpose, "side": side, "qty": qty,
                                        "price": round(px, 4),
                                        "order_type": order_type,
                                        "order_side": req.side,
                                        "broker_order_id": result.broker_order_id})
        return result.broker_order_id

    async def _on_scale_out_filled(self, event: BrokerEvent, ctx: _PendingIntent) -> None:
        """A resting partial take-profit filled. The position stays OPEN with
        the remainder, so unlike every other exit path this must not clear
        position state or start the re-entry cooldown.

        The protective stop is re-placed at the reduced quantity: it was sized
        for the full position, and leaving it would mean a subsequent stop
        trigger tries to sell more shares than are still held — a broker
        rejection at exactly the moment protection is needed. Cancel-then-place
        (rather than `replace`) keeps the same failure semantics as the entry
        path: if the resized stop can't be placed, raise, which marks the run
        ERROR rather than continuing under-protected."""
        position = self._positions.get(ctx.symbol)
        if position is None:
            return
        fill = event.fill
        position.scale_order_qty.pop(event.broker_order_id, None)
        position.qty -= fill.qty
        realized = _realized_pnl(position.side, position.avg_price, fill.price, fill.qty)
        self._realized_pnl_today += realized
        await self._log_event("fill", {"symbol": ctx.symbol, "side": position.side,
                                       "order_side": _close_order_side(position.side),
                                       "qty": fill.qty,
                                       "price": fill.price, "reason": "scale_out",
                                       "remaining_qty": position.qty,
                                       "realized_pnl": round(realized, 2)})

        if position.qty <= 0:  # defensive: the ladder always leaves a remainder
            await self._on_position_closed(event, ctx, cancel_siblings_of=None)
            return

        old_stop_id = position.stop_broker_order_id
        if old_stop_id:
            try:
                await self.broker.cancel(old_stop_id)
            except Exception as e:  # noqa: BLE001 — best-effort; the re-place below is what matters
                log.warning("scale_out_stop_cancel_failed", symbol=ctx.symbol,
                           broker_order_id=old_stop_id, error=str(e))
            self._orders.pop(old_stop_id, None)
            sl = self.doc.exit.stop_loss
            atr_value = self._atr_value_for(ctx.symbol, sl)
            px = compute_stop_price(sl, position.avg_price, atr_value,
                                    side=position.side, kind="stop")
            position.stop_broker_order_id = await self._place_protective(
                ctx.symbol, position.qty, "stop", px, position.avg_price, side=position.side)
            if position.stop_broker_order_id is None:
                raise RuntimeError(
                    f"failed to re-place the resized protective stop for {ctx.symbol}")
        await self._upsert_position_row(position)

    async def _on_position_closed(self, event: BrokerEvent, ctx: _PendingIntent,
                                  cancel_siblings_of: str | None) -> None:
        position = self._positions.get(ctx.symbol)
        if position is None:
            return
        fill = event.fill
        sibling_ids: list[str] = []
        if cancel_siblings_of == "stop" and position.target_broker_order_id:
            sibling_ids.append(position.target_broker_order_id)
        elif cancel_siblings_of == "target" and position.stop_broker_order_id:
            sibling_ids.append(position.stop_broker_order_id)
        elif cancel_siblings_of is None:  # discretionary exit — cancel BOTH
            sibling_ids = [oid for oid in
                          (position.stop_broker_order_id, position.target_broker_order_id) if oid]
        # Any scale-out level that hasn't filled yet is stale once the position
        # is gone — leaving it resting would sell shares that are no longer
        # held. Cancel them on EVERY full-close path, not just discretionary
        # exits (a stop or final target closing the position leaves the
        # unfilled lower/upper levels behind just the same).
        sibling_ids += list(position.scale_order_qty)
        for oid in sibling_ids:
            try:
                await self.broker.cancel(oid)
            except Exception as e:  # noqa: BLE001 — best-effort cleanup, don't crash the runner
                log.warning("sibling_cancel_failed", symbol=ctx.symbol, broker_order_id=oid,
                           error=str(e))
            self._orders.pop(oid, None)

        if position.is_option:
            # A long option's P&L is (exit - entry) x qty x MULTIPLIER, and it
            # is always the long-side formula: both a call and a put position
            # are BOUGHT, so `side` describes the SIGNAL direction, not the
            # cash flow. Passing `side` to _realized_pnl here would invert the
            # sign on every put trade.
            realized = (fill.price - position.avg_price) * fill.qty * position.multiplier
        else:
            realized = _realized_pnl(position.side, position.avg_price, fill.price, fill.qty)
        self._realized_pnl_today += realized
        await self._log_event("fill", {
            "symbol": ctx.symbol, "side": position.side,
            "order_side": ("sell_to_close" if position.is_option
                           else _close_order_side(position.side)),
            "qty": fill.qty, "price": fill.price, "reason": ctx.purpose,
            **({"contract": position.contract_symbol,
                "multiplier": position.multiplier} if position.is_option else {}),
            "realized_pnl": round(realized, 2)})
        self._positions.pop(ctx.symbol, None)
        if position.is_option and self._opt is not None:
            await self._opt.unsubscribe([position.contract_symbol])
        df = self._candles.get(ctx.symbol)
        bar_i = (len(df) - 1) if df is not None else 0
        self._cooldown_until_bar[ctx.symbol] = bar_i + self.doc.entry.cooldown_bars
        await self._delete_position_row(position.contract_symbol or ctx.symbol)

    # ---- persistence ------------------------------------------------------------
    async def _log_event(self, kind: str, payload: dict, level: str = "info") -> None:
        await self.db.execute(
            "INSERT INTO run_events (run_id, ts, level, kind, payload_json) VALUES (?,?,?,?,?)",
            (self.run_id, int(time.time() * 1000), level, kind, json.dumps(payload)))

    async def _update_run_state(self) -> None:
        await self.db.execute("UPDATE runs SET state=? WHERE id=?", (self.state, self.run_id))

    async def _upsert_position_row(self, position: _LivePosition) -> None:
        """Direction is written EXPLICITLY to `positions.side` — the sign of
        `qty` is no longer how a short survives a restart (a reader outside
        `runtime/` saw a negative share count with nothing explaining it).

        `qty` is nonetheless still stored SIGNED, deliberately: recovery.py's
        `_reconstruct_cash` derives paper cash from `sum(qty * avg_price)`
        (a short's opening fill ADDS proceeds, which a negative cost basis
        expresses for free) and `SimBroker.seed_position` takes a signed
        quantity. Both live outside this lane's files; the sign is now
        redundant belt-and-braces rather than the encoding, and
        `_recover_position` prefers the column."""
        signed_qty = position.qty if position.side == "long" else -position.qty
        # OPTION positions are stored under their CONTRACT symbol, not the
        # underlying: the `positions` primary key is (run_id, symbol), and a
        # strategy can hold contracts on the same underlying across different
        # expirations over its life. `underlying` carries the ticker so recovery
        # and the UI can group back to it.
        row_symbol = position.contract_symbol or position.symbol
        expiry_ms = (expiry_to_ms(position.contract_expiry)
                     if position.contract_expiry is not None else None)
        await self.db.execute(
            """INSERT INTO positions (run_id, symbol, qty, avg_price, opened_at,
               stop_order_id, target_order_id, side, asset_type, underlying,
               multiplier, expiry, premium_paid, underlying_entry_px)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id, symbol) DO UPDATE SET qty=excluded.qty,
                 avg_price=excluded.avg_price, stop_order_id=excluded.stop_order_id,
                 target_order_id=excluded.target_order_id, side=excluded.side,
                 asset_type=excluded.asset_type, underlying=excluded.underlying,
                 multiplier=excluded.multiplier, expiry=excluded.expiry,
                 premium_paid=excluded.premium_paid,
                 underlying_entry_px=excluded.underlying_entry_px""",
            (self.run_id, row_symbol, signed_qty, position.avg_price, position.opened_at,
             position.stop_broker_order_id, position.target_broker_order_id, position.side,
             "OPTION" if position.is_option else "EQUITY", position.symbol,
             position.multiplier, expiry_ms,
             position.premium_paid if position.is_option else None,
             position.underlying_entry_px if position.is_option else None))

    async def _delete_position_row(self, symbol: str) -> None:
        await self.db.execute("DELETE FROM positions WHERE run_id=? AND symbol=?",
                              (self.run_id, symbol))
