"""SQLite access. One aiosqlite connection per app, WAL mode, schema applied
idempotently at startup. Kept deliberately thin — pandas/raw SQL over ORM."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        await self._migrate()

    async def _migrate(self) -> None:
        """`executescript(_SCHEMA)` is CREATE TABLE IF NOT EXISTS only — it
        never alters an already-created table. Guarded, idempotent
        migrations for schema changes after a table first shipped go here.
        No migration framework; this app has needed a couple so far (Phase 6
        slice 2's runs.account_hash; slice 4's promotions nullability)."""
        async with self.conn.execute("PRAGMA table_info(runs)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if "account_hash" not in cols:
            await self.conn.execute("ALTER TABLE runs ADD COLUMN account_hash TEXT")
            await self.conn.commit()

        # positions.side (audit A5a): a live position's direction used to
        # survive a restart only as the SIGN of qty, which is invisible to any
        # reader outside runtime/. Without this migration a restarted runner's
        # position INSERT fails with "no such column: side" — i.e. it is a
        # restart blocker for paper and live runs, not a cosmetic addition.
        # qty stays signed as belt-and-braces so a pre-migration row (which
        # defaults to 'long' here) still recovers as a short; retiring the sign
        # recovery.py's SELECT and _reconstruct_cash now read it (audit A5c).
        async with self.conn.execute("PRAGMA table_info(positions)") as cur:
            pos_cols = {row["name"] for row in await cur.fetchall()}
        if "side" not in pos_cols:
            await self.conn.execute(
                "ALTER TABLE positions ADD COLUMN side TEXT NOT NULL DEFAULT 'long'")
            await self.conn.commit()

        # backtests originally identified their strategy only by
        # strategy_version_hash. content_hash EXCLUDES meta (see
        # strategy/hashing.py), so a copied or renamed strategy is
        # hash-identical to its original and the hash -> name lookup picked an
        # arbitrary one of them — a run of "EMA Crossover Trend Long v1" could
        # list itself as "…v3". Record the strategy that actually ran.
        # NULL on rows written before this migration; those still fall back to
        # the (ambiguous) hash lookup.
        async with self.conn.execute("PRAGMA table_info(backtests)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if "strategy_id" not in cols:
            await self.conn.execute("ALTER TABLE backtests ADD COLUMN strategy_id TEXT")
            await self.conn.commit()

        await self._migrate_options_columns()
        await self._migrate_promotions_nullable()

    async def _migrate_options_columns(self) -> None:
        """Options columns on pre-existing tables (docs/plan/16).

        These tables shipped before options existed, so `CREATE TABLE IF NOT
        EXISTS` will not add the columns to a database that already has them.
        Every column is nullable or defaulted to its equity value, so existing
        rows keep their exact meaning and nothing needs backfilling — an
        untouched equity row reads as EQUITY/multiplier 1, which is what it is.
        """
        additions = {
            "order_intents": [
                ("asset_type", "TEXT NOT NULL DEFAULT 'EQUITY'"),
                ("underlying", "TEXT"),
                ("multiplier", "REAL NOT NULL DEFAULT 1"),
            ],
            "backtest_trades": [
                ("detail_json", "TEXT"),
            ],
            "positions": [
                ("asset_type", "TEXT NOT NULL DEFAULT 'EQUITY'"),
                ("underlying", "TEXT"),
                ("multiplier", "REAL NOT NULL DEFAULT 1"),
                ("expiry", "INTEGER"),
                ("premium_paid", "REAL"),
                ("underlying_entry_px", "REAL"),
            ],
        }
        for table, cols in additions.items():
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                have = {row["name"] for row in await cur.fetchall()}
            for name, ddl in cols:
                if name not in have:
                    await self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        await self.conn.commit()

    async def _migrate_promotions_nullable(self) -> None:
        """backtest_run_id/paper_run_id were originally NOT NULL — a
        strategy could only be promoted to live with both a completed
        backtest and prior paper-trading evidence. override_gate=True can
        now bypass those requirements entirely (explicit user choice, Phase
        6 slice 4), so a promotion can legitimately have neither. SQLite has
        no ALTER COLUMN to drop a NOT NULL constraint — recreate the table.
        Guarded on the columns already being nullable so this is a no-op on
        every startup after the first."""
        async with self.conn.execute("PRAGMA table_info(promotions)") as cur:
            info = {row["name"]: row["notnull"] for row in await cur.fetchall()}
        if info.get("backtest_run_id") == 0 and info.get("paper_run_id") == 0:
            return
        await self.conn.executescript("""
            CREATE TABLE promotions_new (
                id TEXT PRIMARY KEY, strategy_version_hash TEXT NOT NULL,
                backtest_run_id TEXT, paper_run_id TEXT,
                gate_snapshot_json TEXT NOT NULL, confirmed_at INTEGER NOT NULL
            );
            INSERT INTO promotions_new SELECT * FROM promotions;
            DROP TABLE promotions;
            ALTER TABLE promotions_new RENAME TO promotions;
        """)
        await self.conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetch_one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    # -- audit log (append-only) ------------------------------------------
    async def audit(self, actor: str, category: str, payload: dict[str, Any]) -> None:
        await self.execute(
            "INSERT INTO audit_log (ts, actor, category, payload_json) VALUES (?,?,?,?)",
            (int(time.time() * 1000), actor, category, json.dumps(payload)),
        )

    # -- app_state key-value ----------------------------------------------
    async def state_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetch_one("SELECT value_json FROM app_state WHERE key=?", (key,))
        return json.loads(row["value_json"]) if row else default

    async def state_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO app_state (key, value_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
            "updated_at=excluded.updated_at",
            (key, json.dumps(value), int(time.time() * 1000)),
        )
