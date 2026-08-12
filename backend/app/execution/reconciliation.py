"""ReconciliationEngine — "trust Schwab, not our journal" (docs/plan/
06-execution-safety.md §Reconciliation). Runs periodically (main.py wires it
into the scheduler, every 5min during market hours) and compares local live
state against actual broker truth. Any mismatch trips the kill switch
(OrderGateway.activate_kill_switch) and pauses every active live run — the
same fail-safe posture as every other check in this app: unknown/inconsistent
state degrades toward "flat and stopped", never toward "keep going and hope."

Account scoping (audit B2). Comparisons are scoped PER SCHWAB ACCOUNT, using
`runs.account_hash` (persisted by manager.start_live()). This engine used to
compare local state against the SUM of positions across every connected
account, justified by "there are zero live runs, so the sums are always
empty". That premise expired the moment a live run existed alongside
more than one connected account, and summing was wrong in both directions:
  * a position held in a DIFFERENT account than the live run's entered the
    sum and could trip the kill switch — pausing a real live strategy for
    something it never traded; and, worse,
  * two genuine discrepancies of opposite sign in different accounts netted
    to zero and MASKED a real break.

Unattributed live runs (`runs.account_hash IS NULL` — runs armed before this
column was persisted, or a run row this engine cannot find at all) get the
fail-safe treatment, never a silent fall back to sum-everything and never a
skip:
  * exactly ONE connected Schwab account -> attribution is unambiguous (the
    only account it could possibly be), so the run is reconciled against
    that account and a warning is logged;
  * TWO OR MORE connected accounts -> attribution is genuinely unknowable
    here, so this trips (`unattributed_live_run`) rather than guessing. The
    run is STILL compared, against the all-account union, so any additional
    detail is reported too — but the trip stands on the ambiguity alone.
Same posture as every other check in this app: unknown/inconsistent state
degrades toward "flat and stopped", never toward "keep going and hope." The
fix is to re-arm the run so it records its account_hash."""

from __future__ import annotations

import json
import time

from ..db import Database
from ..logging import get_logger
from ..schwab.client import SchwabClient
from .gateway import OrderGateway

log = get_logger("reconciliation")

_TERMINAL_ORDER_STATUSES = {"CANCELED", "REJECTED", "EXPIRED", "FILLED"}
_POSITION_EPSILON = 1e-6
_DAY_MS = 86_400_000
# Schwab's order history endpoint only serves ~60 days back; asking for more
# is an error, not a bigger answer.
_MAX_ORDER_LOOKBACK_DAYS = 55


class ReconciliationEngine:
    def __init__(self, db: Database, schwab: SchwabClient, run_manager, gateway: OrderGateway):
        self._db = db
        self._schwab = schwab
        self._run_manager = run_manager
        self._gateway = gateway

    async def run_once(self) -> dict:
        report: dict = {"ts": int(time.time() * 1000), "trips": [], "clean": True}

        await self._check_unresolved_unknown_orders(report)

        live_run_symbols = self._run_manager.live_run_symbols()

        hashes = await self._schwab.get_account_numbers()
        account_hashes = [h["hashValue"] for h in hashes]
        # Resolve every live run to the account it trades in BEFORE comparing
        # anything — see the module docstring for the unattributed rules.
        run_accounts = await self._resolve_run_accounts(
            report, list(live_run_symbols), account_hashes)

        # Per-account universes: only symbols an account's own live runs trade
        # are "ours" to compare there.
        universe_by_account: dict[str | None, set[str]] = {}
        for run_id, syms in live_run_symbols.items():
            universe_by_account.setdefault(run_accounts.get(run_id), set()).update(syms)
        local_by_account = self._local_live_positions(run_accounts)

        broker_positions_by_account: dict[str, dict[str, float]] = {}
        broker_open_by_account: dict[str, set[str]] = {}
        broker_open_order_ids: set[str] = set()
        # id -> (account_hash, raw order) for every order in the window,
        # terminal ones included: an order the broker can definitively account
        # for is repairable journal drift, not a "trust Schwab" violation.
        broker_orders: dict[str, tuple[str, dict]] = {}
        snapshots: list[dict] = []
        from_iso, to_iso = await self._order_window()
        for account_hash in account_hashes:
            account = await self._schwab.get_account(account_hash, positions=True)
            acct = account.get("securitiesAccount", {})
            per_account = broker_positions_by_account.setdefault(account_hash, {})
            for pos in acct.get("positions", []) or []:
                sym = pos.get("instrument", {}).get("symbol")
                if not sym:
                    continue
                qty = (pos.get("longQuantity", 0) or 0) - (pos.get("shortQuantity", 0) or 0)
                per_account[sym] = per_account.get(sym, 0.0) + qty
            bal = acct.get("currentBalances", {}) or {}
            snapshots.append({
                "account_hash": account_hash,
                "equity": bal.get("equity", bal.get("liquidationValue")),
                "cash": bal.get("cashBalance", bal.get("totalCash")),
                "buying_power": bal.get("buyingPower", bal.get("cashAvailableForTrading")),
                "positions": acct.get("positions", []),
            })

            for o in await self._schwab.get_orders(account_hash, from_iso, to_iso):
                if o.get("orderId") is None:
                    continue
                oid = str(o["orderId"])
                broker_orders[oid] = (account_hash, o)
                if o.get("status") not in _TERMINAL_ORDER_STATUSES:
                    broker_open_order_ids.add(oid)
                    broker_open_by_account.setdefault(account_hash, set()).add(oid)

        self._check_positions(report, universe_by_account, local_by_account,
                              broker_positions_by_account)
        await self._check_open_orders(report, broker_open_order_ids, broker_orders,
                                      broker_open_by_account, account_hashes)
        await self._write_snapshots(report["ts"], snapshots)
        await self._db.audit("system", "reconciliation", report)

        if not report["clean"]:
            reasons = ",".join(t["reason"] for t in report["trips"])
            await self._gateway.activate_kill_switch(
                actor="system", reason=f"reconciliation_mismatch:{reasons}")
            for run_id in live_run_symbols:
                try:
                    await self._run_manager.pause(run_id)
                except Exception as e:  # noqa: BLE001 — one bad pause must never block others
                    log.error("reconciliation_pause_failed", run_id=run_id, error=str(e))
            log.warning("reconciliation_mismatch", trips=report["trips"])
        else:
            log.info("reconciliation_clean", accounts=len(hashes),
                    live_runs=len(live_run_symbols))

        return report

    async def _order_window(self) -> tuple[str, str]:
        """Broker order-history window: today, extended back far enough to
        cover the OLDEST local order we still believe is open.

        Querying only today (what this did originally) meant any local order
        left SUBMITTED/WORKING past midnight could never appear in the
        broker's response — so it was structurally guaranteed to be reported
        `missing_at_broker` forever, re-tripping the kill switch every 5
        minutes with no way to clear it from the UI. Ask about the orders we
        actually intend to compare against."""
        now_ms = int(time.time() * 1000)
        row = await self._db.fetch_one(
            "SELECT MIN(submitted_at) AS oldest FROM orders WHERE mode='live' "
            "AND status IN ('SUBMITTED','WORKING') AND submitted_at IS NOT NULL")
        oldest = (row["oldest"] if row else None) or now_ms
        from_ms = max(min(oldest, now_ms), now_ms - _MAX_ORDER_LOOKBACK_DAYS * _DAY_MS)
        return (time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime(from_ms / 1000)),
                time.strftime("%Y-%m-%dT23:59:59.000Z", time.gmtime(now_ms / 1000)))

    async def _resolve_run_accounts(self, report: dict, run_ids: list[str],
                                    account_hashes: list[str]) -> dict[str, str | None]:
        """{run_id: account_hash} for every active live run, or None where the
        account is unknowable. See the module docstring for the fail-safe
        rules; the short version is that a NULL account_hash resolves to the
        sole connected account when there is exactly one, and otherwise trips
        `unattributed_live_run` and resolves to None (compared against the
        all-account union, but already tripped on the ambiguity)."""
        resolved: dict[str, str | None] = {}
        if not run_ids:
            return resolved
        rows = await self._db.fetch_all(
            "SELECT id, account_hash FROM runs WHERE id IN "
            f"({','.join('?' * len(run_ids))})", tuple(run_ids))
        stored = {r["id"]: r["account_hash"] for r in rows}

        sole = account_hashes[0] if len(account_hashes) == 1 else None
        unattributed: list[str] = []
        for run_id in run_ids:
            account_hash = stored.get(run_id)
            if account_hash:
                resolved[run_id] = account_hash
                continue
            unattributed.append(run_id)
            resolved[run_id] = sole
            if sole is not None:
                log.warning("reconciliation_unattributed_run_single_account",
                            run_id=run_id, account_hash=sole)

        if unattributed and sole is None:
            report["clean"] = False
            report["trips"].append({
                "reason": "unattributed_live_run",
                "run_ids": sorted(unattributed),
                "connected_accounts": len(account_hashes)})
            log.error("reconciliation_unattributed_live_run",
                      run_ids=sorted(unattributed), accounts=len(account_hashes))
        return resolved

    def _local_live_positions(
            self, run_accounts: dict[str, str | None]) -> dict[str | None, dict[str, float]]:
        totals: dict[str | None, dict[str, float]] = {}
        for status in self._run_manager.list_active():
            if status["mode"] != "live":
                continue
            per_account = totals.setdefault(run_accounts.get(status["run_id"]), {})
            for symbol, pos in status["positions"].items():
                per_account[symbol] = per_account.get(symbol, 0.0) + pos["qty"]
        return totals

    async def _check_unresolved_unknown_orders(self, report: dict) -> None:
        rows = await self._db.fetch_all(
            """SELECT oi.id AS intent_id, oi.run_id FROM order_intents oi
               JOIN orders o ON o.intent_id = oi.id
               WHERE oi.status='UNKNOWN' AND o.mode='live'""")
        if rows:
            report["clean"] = False
            report["trips"].append({
                "reason": "unknown_order_unresolved",
                "intent_ids": [r["intent_id"] for r in rows]})

    def _check_positions(self, report: dict,
                         universe_by_account: dict[str | None, set[str]],
                         local_by_account: dict[str | None, dict[str, float]],
                         broker_positions_by_account: dict[str, dict[str, float]]) -> None:
        # Per plan 06 §Reconciliation: broker positions outside every live
        # strategy's symbol set are manually-traded/untracked and IGNORED
        # (e.g. a live-probe test order) — only compare within the union of
        # active live runs' configured universes. Scoped per account (audit
        # B2): a symbol is compared only against the broker balance of the
        # account whose own live runs trade it, so a holding in another
        # account neither trips it nor cancels out a real break there.
        for account_hash, symbols in universe_by_account.items():
            local = local_by_account.get(account_hash, {})
            broker = self._broker_view(broker_positions_by_account, account_hash)
            for symbol in symbols:
                local_qty = local.get(symbol, 0.0)
                broker_qty = broker.get(symbol, 0.0)
                if abs(local_qty - broker_qty) > _POSITION_EPSILON:
                    report["clean"] = False
                    report["trips"].append({
                        "reason": "position_mismatch", "symbol": symbol,
                        "account_hash": account_hash,
                        "local_qty": local_qty, "broker_qty": broker_qty})

    @staticmethod
    def _broker_view(broker_positions_by_account: dict[str, dict[str, float]],
                     account_hash: str | None) -> dict[str, float]:
        """Broker position balances an unattributed run is compared against:
        the all-account union. Only reachable once `unattributed_live_run`
        has already tripped — this exists so the report still carries the
        symbol-level detail, NOT as a quiet fallback."""
        if account_hash is not None:
            return broker_positions_by_account.get(account_hash, {})
        union: dict[str, float] = {}
        for per_account in broker_positions_by_account.values():
            for sym, qty in per_account.items():
                union[sym] = union.get(sym, 0.0) + qty
        return union

    async def _check_open_orders(self, report: dict, broker_open_order_ids: set[str],
                                 broker_orders: dict[str, tuple[str, dict]],
                                 broker_open_by_account: dict[str, set[str]],
                                 account_hashes: list[str]) -> None:
        # Broker order ids are globally unique and can never net out across
        # accounts, so `missing_locally` stays a union check: an id Schwab
        # reports open that our journal has never heard of is a break wherever
        # it lives. `missing_at_broker` IS account-scoped (audit B2) — an order
        # we booked to account X must be open in account X, not merely open
        # somewhere.
        rows = await self._db.fetch_all(
            "SELECT o.schwab_order_id AS oid, r.account_hash AS account_hash "
            "FROM orders o LEFT JOIN runs r ON r.id = o.run_id "
            "WHERE o.mode='live' AND o.status IN ('SUBMITTED','WORKING')")
        sole = account_hashes[0] if len(account_hashes) == 1 else None
        local_open_ids: set[str] = set()
        missing_at_broker: set[str] = set()
        for r in rows:
            oid = r["oid"]
            if not oid:
                continue
            local_open_ids.add(oid)
            account_hash = r["account_hash"] or sole
            expected = (broker_open_by_account.get(account_hash, set())
                        if account_hash is not None else broker_open_order_ids)
            if oid not in expected:
                missing_at_broker.add(oid)
        missing_locally = broker_open_order_ids - local_open_ids

        # "Trust Schwab, not our journal" cuts both ways: if the broker
        # reports the order in a terminal state, Schwab's answer IS the truth
        # and our stale row is what's wrong — repair it instead of tripping.
        # Only ids Schwab can't account for at all remain a genuine mismatch.
        repaired: list[dict] = []
        for oid in sorted(missing_at_broker):
            entry = broker_orders.get(oid)
            if entry is None:
                continue
            status = entry[1].get("status")
            if status not in _TERMINAL_ORDER_STATUSES:
                continue
            await self._db.execute(
                "UPDATE orders SET status=?, closed_at=COALESCE(closed_at,?), "
                "raw_last_json=? WHERE schwab_order_id=?",
                (status, int(time.time() * 1000), json.dumps(entry[1]), oid))
            repaired.append({"schwab_order_id": oid, "status": status})
            log.warning("reconciliation_order_repaired", schwab_order_id=oid, status=status)
        if repaired:
            report["repaired_orders"] = repaired
            missing_at_broker -= {r["schwab_order_id"] for r in repaired}

        if missing_locally or missing_at_broker:
            report["clean"] = False
            report["trips"].append({
                "reason": "open_order_mismatch",
                "missing_locally": sorted(missing_locally),
                "missing_at_broker": sorted(missing_at_broker)})

    async def _write_snapshots(self, ts: int, snapshots: list[dict]) -> None:
        # account_snapshots.ts is the sole PRIMARY KEY (one row per point in
        # time, no per-account dimension in the schema) — aggregate every
        # connected account into a single row rather than colliding on ts.
        if not snapshots:
            return
        total_equity = sum(s["equity"] for s in snapshots if s["equity"] is not None) or None
        total_cash = sum(s["cash"] for s in snapshots if s["cash"] is not None) or None
        total_bp = sum(s["buying_power"] for s in snapshots if s["buying_power"] is not None) or None
        await self._db.execute(
            """INSERT INTO account_snapshots (ts, equity, cash, buying_power, positions_json)
               VALUES (?,?,?,?,?)""",
            (ts, total_equity, total_cash, total_bp, json.dumps(snapshots)))
