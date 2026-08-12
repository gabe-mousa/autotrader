"""Crash/restart recovery (docs/plan/06-execution-safety.md §Crash/restart
recovery), scoped to what's actually meaningful for Phase 5 — SimBroker, not
SchwabBroker.

Why this differs from the plan's recipe: the plan's recovery sequence (fetch
broker positions/orders, reconcile the journal against them) assumes an
EXTERNAL broker that keeps its own state across a crash. SimBroker doesn't —
it's a plain in-process object, and killing the app destroys it along with
every resting order it was tracking. There is no external truth to reconcile
against for paper. What IS durable is the `positions` table (last known
qty/avg_price per symbol, updated on every fill) and `run_events` (an
append-only fill/order log) — so paper recovery reconstructs a plausible
in-process state FROM those tables instead of reconciling against a live
broker. This is a cheap paper-mode stand-in for real reconciliation, not a
replacement for it: Phase 6 (live) must implement the plan's actual recipe
against Schwab, and cannot reuse this file as-is.

Recovery sequence, run once at startup before `RunManager` is used for
anything else:

1. Find runs left non-terminal (`state IN ('RUNNING','PAUSED')`,
   `stopped_at IS NULL`). A clean `RunManager.stop()` always sets both, so
   anything still marked active here means the process died without one. Live
   runs additionally include `SUSPENDED_AUTH`: that state is produced BY this
   module when reconciliation could not be completed safely, and its usual
   cause (an expired refresh token) is usually fixed by the time of the next
   restart — so every restart retries it, rather than leaving a real live
   position stranded until someone notices the button.
2. Each dangling `mode='live'` run is RECONCILED against Schwab and re-adopted
   (`_recover_one_live_run`), and only falls back to `SUSPENDED_AUTH` when that
   cannot be done safely. This is the plan's actual recipe (fetch broker
   positions/orders, reconcile the journal against them) rather than paper's
   reconstruct-from-DB stand-in, and nothing about it is a guess: every number
   it adopts comes from Schwab.

   What "safely" rules out, always fail-closed to `SUSPENDED_AUTH` with the
   reason journaled:
     * the run has no `account_hash` and more than one Schwab account is
       connected — which account it traded in is unknowable, and reconciling
       against the wrong one would mean adopting someone else's positions
       (the sole-account case resolves unambiguously and is allowed, with a
       warning, matching `execution/reconciliation.py`'s rule);
     * ANY Schwab call fails (expired refresh token, outage) — the state is
       unknown, so the run stays suspended and can be reconnected from the UI
       once auth is back;
     * a protective order cannot be established for a position that is
       configured to have one — the same halt-the-run invariant a live fill
       obeys. Note that an ADOPTED protective order needs no placement at all
       and so is unaffected by this (or by the kill switch, which blocks every
       new placement including protective ones); it is only a position whose
       stop genuinely died while we were down that can fail here.

   Two properties of live recovery that paper recovery does not need:
     * Positions come from SCHWAB, not from our `positions` table. The journal
       supplies only what Schwab does not know (`opened_at`, and the strategy's
       own notion of side); quantity is always the broker's. A position the
       journal has and Schwab does not was closed while we were down (its stop
       filled) and is dropped; a position Schwab has and the journal does not
       is adopted at Schwab's average price so it is at least protected and
       flattenable.
     * Resting protective orders are ADOPTED, not re-placed. A Schwab DAY order
       survives our process; re-placing it would rest two stops against one
       position and oversell on exit. See `SchwabBroker.adopt`.

   A recovered live run ALWAYS comes back PAUSED, whatever it was doing before
   the crash, and its `resume_policy` is deliberately NOT consulted: `start_live`
   hardcodes `require_confirm` and nothing in the app can set a live run to
   anything else, so honouring the column would only create a path for
   `flatten_on_restart` to liquidate real positions unattended at startup. Recovery restores the app's ability to SEE, protect and flatten
   real positions; letting real-money order entry resume itself unattended is a
   separate decision that still requires the explicit `activate_live()`
   confirmation (`RunManager.recover_live`).
3. For each dangling PAPER run:
   a. Every still-`SUBMITTED` `order_intents` row for it is orphaned — the
      SimBroker instance (and the order it refers to) no longer exists.
      Marked `UNKNOWN` (never resubmitted/retried, audit-logged). If that
      order had actually reached a fill in a counterfactual sense, its
      symbol is simply not treated as "pending" going forward — a documented
      v1 gap (see `RunManager.recover_paper`'s caller contract below), fine
      for paper (no real money), NOT fine to carry into live untouched.
   b. Cash is reconstructed EXACTLY (not approximated) from SimBroker's own
      fill accounting: starting_equity + (sum of every realized_pnl this run
      has ever logged) − (cost basis of its currently-open positions). See
      `_reconstruct_cash` for the derivation.
   c. Today's risk-relevant counters (`orders_today`, `entries_today` per
      symbol, `realized_pnl_today`) are reconstructed from `order_intents`/
      `run_events` since local (America/New_York) midnight. Skipping this
      would silently hand a strategy that had already tripped
      max_daily_loss or max_entries_per_day a fresh daily allowance on every
      restart — a real risk-limit bypass, not a cosmetic gap.
   d. `RunManager.recover_paper` rebuilds the run's `StrategyRunner` and a
      fresh `SimBroker` seeded with (b)/(c), and re-places resting
      protective stop/target orders for every recovered position (the old
      ones vanished with the old SimBroker) — recomputed from the
      strategy's OWN stop_loss/take_profit config against the position's
      avg_price, the exact formula a real fill uses. A SHORT position recovers
      too: direction is read from the explicit `positions.side` column
      (audit A5c), so the rebuilt stop rests ABOVE the entry and the target
      BELOW it, both as buy_to_cover orders. `qty` is ALSO still stored signed,
      which is what `SimBroker.seed_position` wants (SimBroker is signed
      internally) and what `_reconstruct_cash` reads for its cost basis — but
      the sign is now belt-and-braces rather than the encoding. Both readers
      treat "side says short OR qty is negative" as short, because the
      migration that added the column defaulted existing rows to 'long'
      (`db.py::_migrate`): a legacy short therefore has side='long' AND a
      negative qty, and trusting the column alone would silently flip it to a
      long and place its protective stop on the wrong side of the market.
   e. Resume per `resume_policy`: `auto_resume` (paper's default) resumes
      into its pre-crash RUNNING/PAUSED state; `require_confirm` always
      resumes PAUSED regardless of pre-crash state (mirrors the plan's live
      default, reusable for paper too since the column is user-settable);
      `flatten_on_restart` starts the run just long enough to flatten every
      recovered position at the last known price, then stops it.
4. One run's recovery failing (e.g. an atr_multiple stop with NaN ATR from
   too little warm-up history) flips ONLY that run to `ERROR` and never
   blocks recovering the others — same fault isolation the live event loop
   already gives each running strategy."""

from __future__ import annotations

import asyncio
import json
import time

import pandas as pd

from ..broker.schwab import SchwabBroker
from ..db import Database
from ..logging import get_logger
from .manager import RunManager

log = get_logger("recovery")

FLATTEN_TIMEOUT_S = 5.0
FLATTEN_POLL_S = 0.05
_POSITION_EPSILON = 1e-6


def _today_start_ms() -> int:
    return int(pd.Timestamp.now(tz="America/New_York").normalize()
              .tz_convert("UTC").timestamp() * 1000)


def _sum_realized_pnl(rows: list) -> float:
    total = 0.0
    for r in rows:
        payload = json.loads(r["payload_json"])
        if "realized_pnl" in payload:
            total += payload["realized_pnl"]
    return total


async def _reconstruct_today_counters(db: Database, run_id: str) -> dict:
    """Today's risk counters from the journal. The status filter is
    "everything that actually reached the broker" — the runner increments
    `_orders_today` on acceptance, so an order that has since FILLED or been
    CANCELED still spent its slot. FILLED/CANCELED only ever appear on a live
    run's intents (settled by `_settle_orphaned_live_intents` from Schwab's own
    answer); leaving them out would hand a live run that had already tripped
    max_daily_orders or max_entries_per_day a fresh allowance on every
    restart. REJECTED is excluded on purpose: it never reached the market."""
    today_start = _today_start_ms()
    intents = await db.fetch_all(
        "SELECT symbol, purpose FROM order_intents WHERE run_id=? AND ts>=? "
        "AND status IN ('SUBMITTED','UNKNOWN','FILLED','CANCELED')", (run_id, today_start))
    entries_today: dict[str, int] = {}
    for r in intents:
        if r["purpose"] == "entry":
            entries_today[r["symbol"]] = entries_today.get(r["symbol"], 0) + 1
    fills_today = await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id=? AND kind='fill' AND ts>=?",
        (run_id, today_start))
    return {"orders_today": len(intents), "entries_today": entries_today,
           "realized_pnl_today": _sum_realized_pnl(fills_today)}


def _signed_qty(position: dict) -> float:
    """Cost-basis-signed quantity for a `positions` row: negative for a short.

    A row is short when its `side` column says so OR its stored qty is negative
    — see `_reconstruct_cash` for why both tests are needed. Kept as one helper
    so the two readers in this module cannot drift apart."""
    qty = float(position["qty"])
    is_short = str(position.get("side") or "").lower() == "short" or qty < 0
    return -abs(qty) if is_short else abs(qty)


async def _reconstruct_cash(db: Database, run_id: str, starting_equity: float,
                            positions: list[dict]) -> float:
    """SimBroker's own cash accounting: a buy fill subtracts qty*price, a
    sell fill adds qty*price. For a closed round trip the net cash effect
    across both fills equals its realized_pnl exactly (sell notional − buy
    notional). For a still-open position, only the buy happened, so its net
    cash effect is -cost_basis (qty*avg_price) with no offsetting sell yet.
    Summing over the run's whole history: cash_now = starting_cash +
    sum(realized_pnl of every closed round trip) − sum(cost basis of
    currently-open positions). Exact, not an approximation, GIVEN the
    fill-level accounting SimBroker actually performs.

    Works for SHORTS because an open short's opening fill ADDED proceeds to cash
    rather than removing a cost basis, which a NEGATIVE cost basis expresses for
    free. The realized_pnl of a closed short round trip is likewise its exact net
    cash effect (sale proceeds minus cover cost), so both terms carry direction
    on their own.

    Direction comes from `positions.side` (audit A5c) rather than purely from the
    sign of `qty`, with the sign kept as a fallback for rows written before that
    column existed — the migration defaults them to 'long' while their qty is
    still negative, so "side says short OR qty is negative" is the only test that
    is correct for both vintages. Reading the column matters because it is now
    the authoritative field: a row whose side says short must produce a negative
    cost basis even if some future writer stores an unsigned qty."""
    all_fills = await db.fetch_all(
        "SELECT payload_json FROM run_events WHERE run_id=? AND kind='fill'", (run_id,))
    realized_total = _sum_realized_pnl(all_fills)
    # An OPTION position's cost basis is premium x qty x MULTIPLIER. Omitting
    # the multiplier here would under-report the cash a position consumed by
    # 100x and hand the recovered run a wildly inflated balance to trade on —
    # the failure would look like free money rather than an error.
    cost_basis_open = sum(_signed_qty(p) * p["avg_price"] * _multiplier_of(p)
                          for p in positions)
    return starting_equity + realized_total - cost_basis_open


def _multiplier_of(position: dict) -> float:
    """Contract multiplier for a position row; 1.0 for equities and for rows
    written before options existed."""
    try:
        return float(position.get("multiplier") or 1.0)
    except (TypeError, ValueError):
        return 1.0


async def _suspend_live_run(db: Database, run_id: str, reason: str, actor: str = "system") -> None:
    """Fail-closed exit for a live run recovery cannot safely complete. The run
    keeps `stopped_at IS NULL` so the UI's Reconnect action can retry it once
    the reason is gone (auth restored, account re-attributed) — SUSPENDED_AUTH
    means "the app has lost its grip on this run", not "this run is over"."""
    await db.execute("UPDATE runs SET state='SUSPENDED_AUTH' WHERE id=?", (run_id,))
    await db.audit(actor, "recovery",
                   {"run_id": run_id, "event": "live_run_suspended", "reason": reason})
    log.error("live_run_suspended_at_recovery", run_id=run_id, reason=reason)


async def _resolve_account_hash(run_manager: RunManager, row) -> str:
    """The Schwab account a live run trades in. `runs.account_hash` when it is
    recorded; otherwise the sole connected account if there is exactly one.
    Raises when attribution is genuinely ambiguous — reconciling a live run
    against the wrong account would adopt positions it never traded. Same rule
    as `execution/reconciliation.py`'s `_resolve_run_accounts`."""
    stored = row["account_hash"] if "account_hash" in row.keys() else None
    if stored:
        return stored
    accounts = await run_manager._schwab.get_account_numbers()
    if len(accounts) == 1:
        sole = accounts[0]["hashValue"]
        log.warning("live_recovery_unattributed_run_single_account",
                    run_id=row["run_id"], account_hash=sole)
        return sole
    raise ValueError(
        f"live run has no account_hash and {len(accounts)} Schwab accounts are connected — "
        "which account it traded in cannot be determined; re-arm the run to record it")


def _order_px(order: dict) -> float | None:
    """The price a resting protective order will actually trigger/fill at."""
    for key in ("stopPrice", "price"):
        value = order.get(key)
        if value is not None:
            return float(value)
    return None


async def _reconcile_live_positions(db: Database, run_id: str, universe: set[str],
                                    broker_positions: dict[str, dict]) -> tuple[list[dict], dict]:
    """Positions to re-adopt, as `positions`-row-shaped dicts, plus a report.

    Schwab is the authority on WHAT is held and HOW MUCH; the journal supplies
    only `opened_at` and the strategy's own side label. Both directions of
    disagreement are resolved toward the broker and reported, never silently:
    a journal row Schwab does not confirm is dropped (its exit filled while we
    were down), and a holding Schwab confirms that the journal missed is
    adopted at the broker's average price so it ends up protected rather than
    invisible. Only symbols in the run's own universe are considered — anything
    else in the account is manual/other-strategy and none of this run's
    business (same scoping rule as reconciliation)."""
    journal = {r["symbol"]: dict(r) for r in await db.fetch_all(
        "SELECT symbol, qty, avg_price, opened_at, side, stop_order_id, target_order_id "
        "FROM positions WHERE run_id=?", (run_id,))}
    report = {"closed_while_down": [], "adopted_from_broker": [], "qty_corrected": [],
              "outside_universe": []}
    now_ms = int(time.time() * 1000)
    recovered: list[dict] = []

    for symbol in sorted(universe | journal.keys()):
        broker_qty = float((broker_positions.get(symbol) or {}).get("qty") or 0.0)
        row = journal.get(symbol)
        if symbol not in universe:
            # A journal row for a symbol no longer in the strategy's universe
            # (the YAML was edited while the run was down). Not adoptable — the
            # runner has no bars or subscription for it — so report it and
            # leave the row alone rather than dropping a real position from the
            # record. Reconciliation will keep flagging it, which is correct.
            log.error("live_recovery_position_outside_universe", run_id=run_id, symbol=symbol,
                      broker_qty=broker_qty)
            report["outside_universe"].append(
                {"symbol": symbol, "journal_qty": float(row["qty"]) if row else 0.0,
                 "broker_qty": broker_qty})
            continue
        if abs(broker_qty) < _POSITION_EPSILON:
            if row is not None:
                await db.execute("DELETE FROM positions WHERE run_id=? AND symbol=?",
                                 (run_id, symbol))
                report["closed_while_down"].append(
                    {"symbol": symbol, "journal_qty": float(row["qty"])})
                log.warning("live_recovery_position_closed_while_down", run_id=run_id,
                            symbol=symbol, journal_qty=float(row["qty"]))
            continue

        side = "short" if broker_qty < 0 else "long"
        signed_qty = broker_qty
        if row is None:
            avg_price = float((broker_positions.get(symbol) or {}).get("avg_price") or 0.0)
            if avg_price <= 0:
                raise ValueError(
                    f"Schwab reports a {symbol} position of {broker_qty} that this run has no "
                    "record of, and no average price to protect it from — reconcile by hand")
            recovered.append({"symbol": symbol, "qty": signed_qty, "avg_price": avg_price,
                              "opened_at": now_ms, "side": side,
                              "stop_order_id": None, "target_order_id": None})
            report["adopted_from_broker"].append(
                {"symbol": symbol, "qty": broker_qty, "avg_price": avg_price})
            log.warning("live_recovery_position_adopted_from_broker", run_id=run_id,
                        symbol=symbol, qty=broker_qty, avg_price=avg_price)
            continue

        if abs(float(row["qty"]) - signed_qty) > _POSITION_EPSILON:
            report["qty_corrected"].append({"symbol": symbol, "journal_qty": float(row["qty"]),
                                            "broker_qty": broker_qty})
            log.warning("live_recovery_position_qty_corrected", run_id=run_id, symbol=symbol,
                        journal_qty=float(row["qty"]), broker_qty=broker_qty)
        recovered.append({**row, "qty": signed_qty, "side": side})

    return recovered, report


async def _adopted_protective_orders(db: Database, run_id: str, positions: list[dict],
                                     open_orders: dict[str, dict]) -> dict[str, dict]:
    """`{symbol: {"stop"|"target": spec}}` for the protective orders journaled
    against these positions that Schwab confirms are STILL RESTING.

    An id Schwab does not report as open is deliberately omitted: it filled,
    was canceled or expired while we were down, so the runner must place a
    fresh one instead of adopting a dead id. `intent_id` comes from the
    `orders` journal so the adopted order's fill still routes to the right
    exit path in the runner."""
    adopted: dict[str, dict] = {}
    for position in positions:
        for purpose, column in (("stop", "stop_order_id"), ("target", "target_order_id")):
            oid = position.get(column)
            if not oid or str(oid) not in open_orders:
                continue
            order = open_orders[str(oid)]
            link = await db.fetch_one(
                "SELECT intent_id FROM orders WHERE schwab_order_id=? AND run_id=?",
                (str(oid), run_id))
            adopted.setdefault(position["symbol"], {})[purpose] = {
                "broker_order_id": str(oid),
                "intent_id": link["intent_id"] if link else None,
                "px": _order_px(order),
                "qty": float(order.get("quantity") or 0) or abs(float(position["qty"])),
                "filled_qty": float(order.get("filledQuantity") or 0),
            }
    return adopted


async def _settle_orphaned_live_intents(db: Database, run_id: str, schwab,
                                        account_hash: str, open_orders: dict[str, dict]) -> dict:
    """Close the books on this run's still-`SUBMITTED` intents using Schwab's
    answer for each one, instead of paper's blanket `UNKNOWN`.

    Blanket-UNKNOWN would be actively harmful here: reconciliation trips the
    kill switch on any unresolved UNKNOWN live intent, so a restart would
    guarantee a tripped kill switch and force the user to hand-resolve every
    order the restart interrupted — including the ones Schwab can account for
    perfectly. An order that is still resting stays SUBMITTED (it is alive and
    being adopted); a terminal one is recorded as what it actually became; only
    an order Schwab cannot account for AT ALL becomes UNKNOWN, which is what
    that status is for.

    A fill that landed while we were down is journaled as a `fill` run_event so
    it appears in the run's history, deliberately WITHOUT a `realized_pnl` key:
    the true P&L of an exit we never saw cannot be derived here, and inventing
    one would corrupt every downstream sum that reads that key."""
    report = {"settled": [], "unknown": []}
    rows = await db.fetch_all(
        """SELECT oi.id AS intent_id, oi.symbol, oi.purpose, o.schwab_order_id AS oid
           FROM order_intents oi LEFT JOIN orders o ON o.intent_id = oi.id
           WHERE oi.run_id=? AND oi.status='SUBMITTED'""", (run_id,))
    for row in rows:
        oid = row["oid"]
        if oid and str(oid) in open_orders:
            continue                      # still working at Schwab — leave it alone
        order = None
        if oid:
            try:
                order = await schwab.get_order(account_hash, str(oid))
            except Exception as e:  # noqa: BLE001 — a 404 means Schwab has no such order
                log.warning("live_recovery_order_lookup_failed", run_id=run_id,
                            schwab_order_id=oid, error=str(e))
        if order is None:
            await db.execute("UPDATE order_intents SET status='UNKNOWN' WHERE id=?",
                             (row["intent_id"],))
            report["unknown"].append({"intent_id": row["intent_id"], "symbol": row["symbol"]})
            continue

        status = str(order.get("status") or "")
        filled = float(order.get("filledQuantity") or 0)
        intent_status = ("FILLED" if status == "FILLED" else
                         "REJECTED" if status == "REJECTED" else
                         "CANCELED" if status in ("CANCELED", "EXPIRED") else "UNKNOWN")
        await db.execute("UPDATE order_intents SET status=? WHERE id=?",
                         (intent_status, row["intent_id"]))
        await db.execute(
            "UPDATE orders SET status=?, closed_at=COALESCE(closed_at,?), raw_last_json=? "
            "WHERE schwab_order_id=?",
            (status or intent_status, int(time.time() * 1000), json.dumps(order), str(oid)))
        if filled > 0:
            await db.execute(
                "INSERT INTO run_events (run_id, ts, level, kind, payload_json) "
                "VALUES (?,?,?,?,?)",
                (run_id, int(time.time() * 1000), "warning", "fill", json.dumps({
                    "symbol": row["symbol"], "qty": filled,
                    "purpose": row["purpose"], "broker_order_id": str(oid),
                    "source": "settled_at_recovery",
                    "note": "filled at Schwab while the app was down; realized P&L for it "
                            "cannot be derived and is NOT included in this run's totals"})))
        report["settled"].append({"intent_id": row["intent_id"], "symbol": row["symbol"],
                                  "status": status, "filled_qty": filled})
        log.warning("live_recovery_intent_settled", run_id=run_id, intent_id=row["intent_id"],
                    schwab_order_id=oid, status=status, filled_qty=filled)
    return report


async def _recover_one_live_run(db: Database, run_manager: RunManager, row,
                                actor: str = "system") -> dict:
    """Reconcile one dangling LIVE run against Schwab and re-adopt it PAUSED.
    Raises on anything that makes safe recovery impossible; the caller
    suspends the run with the reason (see this module's docstring, step 2)."""
    run_id, slug = row["run_id"], row["slug"]
    account_hash = await _resolve_account_hash(run_manager, row)
    doc = run_manager._strategy_files.load(slug)
    universe = set(doc.universe.symbols)

    # Schwab truth, read through a throwaway broker (it places nothing and
    # starts no poller — the run's own broker is built by recover_live).
    probe = SchwabBroker(run_manager._schwab, account_hash)
    broker_positions = await probe.get_positions_detail()
    open_orders = await probe.get_open_orders()

    positions, position_report = await _reconcile_live_positions(
        db, run_id, universe, broker_positions)
    adopted = await _adopted_protective_orders(db, run_id, positions, open_orders)
    # Counters BEFORE settling, so an intent whose status is about to change
    # still counts toward today's allowance (see _reconstruct_today_counters).
    today_counters = await _reconstruct_today_counters(db, run_id)
    intent_report = await _settle_orphaned_live_intents(
        db, run_id, run_manager._schwab, account_hash, open_orders)

    await run_manager.recover_live(run_id, slug, account_hash, positions, today_counters,
                                   adopted_protective=adopted)

    result = {"run_id": run_id, "mode": "live", "account_hash": account_hash,
              "positions": len(positions),
              "adopted_protective_orders": sum(len(v) for v in adopted.values()),
              "resume_policy": row["resume_policy"], "started_paused": True,
              **position_report, **intent_report}
    await db.audit(actor, "recovery", {"event": "live_run_recovered", **result})
    log.warning("live_run_recovered", run_id=run_id, slug=slug, positions=len(positions),
                adopted_protective_orders=result["adopted_protective_orders"])
    return result


async def _wait_until_flat(run_manager: RunManager, run_id: str,
                           timeout_s: float = FLATTEN_TIMEOUT_S,
                           poll_s: float = FLATTEN_POLL_S) -> bool:
    """Poll for `flatten()`'s market sell(s) to actually clear the runner's
    in-memory positions. `flatten()` only enqueues a control message — it
    does not wait for the resulting fill(s), which themselves complete
    asynchronously after SimBroker's own `sim_latency_ms`. Calling `stop()`
    before they land would silently orphan the position: `stop()` drops the
    run's SimBroker/runner references unconditionally, and once dropped,
    any fill event still in flight has nowhere left to be processed (the
    runner's own queue is never drained again)."""
    runner = run_manager._runners.get(run_id)
    if runner is None:
        return True
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not runner._positions:
            return True
        await asyncio.sleep(poll_s)
    return not runner._positions


async def recover_at_startup(db: Database, run_manager: RunManager,
                             sim_latency_ms: float = 250, sim_slippage_bps: float = 1.0,
                             flatten_timeout_s: float = FLATTEN_TIMEOUT_S) -> dict:
    """Call once at process startup, before `run_manager` is used for
    anything else (no run is active yet, so every non-terminal row found
    here is dangling from an unclean shutdown). `sim_latency_ms`/
    `sim_slippage_bps`/`flatten_timeout_s` are exposed only so tests can
    recover with zero latency and a short flatten timeout; production
    callers should leave them at their defaults.

    Note on `flatten_on_restart`: its market sell needs a cached quote to
    fill (SimBroker's own requirement for any market order) — if the
    streamer hasn't delivered one yet this early in startup, the flatten
    times out and `_wait_until_flat` deliberately leaves the run RUNNING
    rather than falsely marking it STOPPED. It will actually flatten as
    soon as a quote arrives, or close via its still-resting protective stop
    in the meantime — either way the position stays protected."""
    summary: dict[str, list[str]] = {"recovered": [], "errored": [], "live_suspended": [],
                                     "live_recovered": []}

    live_dangling = await db.fetch_all(
        """SELECT r.id AS run_id, r.state, r.resume_policy, r.starting_equity,
                  r.account_hash, s.slug
           FROM runs r JOIN strategies s ON s.id = r.strategy_id
           WHERE r.mode='live' AND r.state IN ('RUNNING','PAUSED','SUSPENDED_AUTH')
             AND r.stopped_at IS NULL""")
    for row in live_dangling:
        run_id = row["run_id"]
        try:
            await _recover_one_live_run(db, run_manager, row, actor="system")
            summary["live_recovered"].append(run_id)
        except Exception as e:  # noqa: BLE001 — one bad run must never block the rest
            await _suspend_live_run(db, run_id, str(e))
            summary["live_suspended"].append(run_id)

    dangling = await db.fetch_all(
        """SELECT r.id AS run_id, r.state, r.resume_policy, r.starting_equity, s.slug
           FROM runs r JOIN strategies s ON s.id = r.strategy_id
           WHERE r.mode='paper' AND r.state IN ('RUNNING','PAUSED') AND r.stopped_at IS NULL""")

    for row in dangling:
        run_id = row["run_id"]
        try:
            await _recover_one_paper_run(db, run_manager, row, actor="system",
                                         sim_latency_ms=sim_latency_ms,
                                         sim_slippage_bps=sim_slippage_bps,
                                         flatten_timeout_s=flatten_timeout_s)
            summary["recovered"].append(run_id)
        except Exception as e:  # noqa: BLE001 — one bad run must never block the rest
            await db.execute("UPDATE runs SET state='ERROR' WHERE id=?", (run_id,))
            await db.audit("system", "recovery",
                           {"run_id": run_id, "event": "recovery_failed", "error": str(e)})
            summary["errored"].append(run_id)
            log.error("run_recovery_failed", run_id=run_id, error=str(e))

    return summary


async def _recover_one_paper_run(db: Database, run_manager: RunManager, row,
                                 actor: str = "system",
                                 sim_latency_ms: float = 250, sim_slippage_bps: float = 1.0,
                                 flatten_timeout_s: float = FLATTEN_TIMEOUT_S) -> dict:
    """Rebuild one dangling paper run's runner/SimBroker from durable DB
    state and resume it per its `resume_policy`. `row` must carry run_id,
    state, resume_policy, starting_equity and slug. Raises on failure; the
    caller decides whether to flip the row to ERROR (startup recovery does;
    the on-demand path re-raises to the API instead so the user sees why)."""
    run_id, slug = row["run_id"], row["slug"]
    positions = [dict(p) for p in await db.fetch_all(
        # asset_type/underlying/multiplier/premium_paid are needed for OPTION
        # rows: the in-memory position map is keyed by the UNDERLYING while the
        # row is keyed by the CONTRACT, and the multiplier must come from the
        # row rather than being assumed (plan/16 D10).
        "SELECT symbol, qty, avg_price, opened_at, side, asset_type, underlying, "
        "multiplier, expiry, premium_paid, underlying_entry_px FROM positions "
        "WHERE run_id=?", (run_id,))]
    today_counters = await _reconstruct_today_counters(db, run_id)
    cash = await _reconstruct_cash(db, run_id, row["starting_equity"] or 0.0, positions)
    await db.execute(
        "UPDATE order_intents SET status='UNKNOWN' WHERE run_id=? AND status='SUBMITTED'",
        (run_id,))

    policy = row["resume_policy"]
    start_paused = policy == "require_confirm" or row["state"] == "PAUSED"
    if policy == "flatten_on_restart":
        await run_manager.recover_paper(run_id, slug, cash, positions, today_counters,
                                        start_paused=False, sim_latency_ms=sim_latency_ms,
                                        sim_slippage_bps=sim_slippage_bps)
        await run_manager.flatten(run_id)
        if await _wait_until_flat(run_manager, run_id, timeout_s=flatten_timeout_s):
            await run_manager.stop(run_id)
        else:
            # Don't lie with a clean STOPPED when a position may still
            # be open — leave it RUNNING (protective stop/target AND
            # the flatten sell are all still resting; whichever fills
            # first closes it) rather than orphaning it silently.
            log.error("flatten_on_restart_timed_out", run_id=run_id)
            await db.audit(actor, "recovery",
                           {"run_id": run_id, "event": "flatten_on_restart_timed_out"})
    else:
        await run_manager.recover_paper(run_id, slug, cash, positions, today_counters,
                                        start_paused=start_paused,
                                        sim_latency_ms=sim_latency_ms,
                                        sim_slippage_bps=sim_slippage_bps)

    await db.audit(actor, "recovery", {
        "run_id": run_id, "event": "recovered", "positions": len(positions),
        "reconstructed_cash": cash, "resume_policy": policy})
    log.info("run_recovered", run_id=run_id, slug=slug, positions=len(positions),
             cash=round(cash, 2))
    return {"run_id": run_id, "positions": len(positions), "reconstructed_cash": cash,
            "resume_policy": policy,
            "started_paused": start_paused and policy != "flatten_on_restart"}


async def recover_run_now(db: Database, run_manager: RunManager, run_id: str,
                          sim_latency_ms: float = 250, sim_slippage_bps: float = 1.0,
                          flatten_timeout_s: float = FLATTEN_TIMEOUT_S) -> dict:
    """On-demand re-adoption of a single dangling run, paper or live, for the
    UI's "Reconnect run" action.

    Startup recovery (`recover_at_startup`) only ever looks at rows in
    RUNNING/PAUSED. A run that ERRORed — either during that very recovery
    pass (step 4) or in the live event loop — keeps `stopped_at IS NULL`
    but is never picked up again, so it dangles forever with force-stop as
    its only exit: it has no in-memory runner, so pause/resume/flatten all
    KeyError. This is the path back, running the exact same reconstruction
    as startup recovery against the same durable tables.

    A live run reaches here in SUSPENDED_AUTH (startup recovery could not
    reconcile it — most often an expired refresh token) and runs the identical
    `_recover_one_live_run` reconciliation against Schwab. It is the retry
    button for that: fix the reason, click again. A failed retry leaves it
    SUSPENDED_AUTH — retryable — rather than ERROR, and the reason is
    re-raised so the UI can show it. Raises ValueError with a user-facing
    reason when the run isn't eligible."""
    if run_id in run_manager._runners:
        raise ValueError("run is already active — no recovery needed")
    row = await db.fetch_one(
        """SELECT r.id AS run_id, r.mode, r.state, r.resume_policy, r.starting_equity,
                  r.account_hash, r.stopped_at, s.slug
           FROM runs r JOIN strategies s ON s.id = r.strategy_id WHERE r.id=?""", (run_id,))
    if row is None:
        raise KeyError(f"no run {run_id!r}")
    if row["stopped_at"] is not None:
        raise ValueError("run is already stopped — nothing to recover")
    if row["mode"] == "live":
        if row["state"] not in ("ERROR", "RUNNING", "PAUSED", "SUSPENDED_AUTH"):
            raise ValueError(f"run is {row['state']} — nothing to recover")
        try:
            return await _recover_one_live_run(db, run_manager, row, actor="user")
        except Exception as e:
            await _suspend_live_run(db, run_id, str(e), actor="user")
            raise
    if row["state"] not in ("ERROR", "RUNNING", "PAUSED"):
        raise ValueError(f"run is {row['state']} — nothing to recover")
    try:
        return await _recover_one_paper_run(db, run_manager, row, actor="user",
                                            sim_latency_ms=sim_latency_ms,
                                            sim_slippage_bps=sim_slippage_bps,
                                            flatten_timeout_s=flatten_timeout_s)
    except Exception as e:
        await db.execute("UPDATE runs SET state='ERROR' WHERE id=?", (run_id,))
        await db.audit("user", "recovery",
                       {"run_id": run_id, "event": "recovery_failed", "error": str(e)})
        log.error("run_recovery_failed", run_id=run_id, error=str(e))
        raise
