"""Option chain snapshot store — SQLite-backed, mirroring marketdata/store.py.

Holds the only real historical option data this system will ever have. Schwab
serves no option price history, so every row here exists because the recorder
captured it live; nothing can backfill it after the fact. That asymmetry drives
two design choices:

  * writes are idempotent and never destructive (a re-run of a snapshot
    overwrites the same (symbol, ts) rather than duplicating or failing);
  * reads are tolerant — `read_chain` returns the NEAREST snapshot within a
    tolerance rather than requiring an exact timestamp match, because a
    backtest bar at 16:00 and a snapshot at 15:50 describe the same market.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass

import pandas as pd

from ..db import Database
from .contracts import Contract, ContractQuote, Greeks
from .expiry import Expiration, expiry_to_ms, ms_to_expiry
from .symbology import canonical_underlying, is_occ, parse_occ

#: How far from a requested timestamp a snapshot may be and still be considered
#: to describe that moment. 4h covers "the bar closed at 16:00, the snapshot was
#: taken at 15:50" and same-session open/close snapshots, without ever letting
#: yesterday's chain answer for today.
DEFAULT_TOLERANCE_MS = 4 * 3600 * 1000

#: How far FORWARD a read may reach when no snapshot exists at or before the
#: requested time. Deliberately far tighter than the backward tolerance, because
#: the two directions are not symmetric: reaching backward uses information that
#: existed at the time, while reaching forward uses information that did not.
#:
#: Under EOD-only recording the forward reach had to be wide or a 10:00 bar
#: found nothing at all. That is exactly the look-ahead that made intraday
#: backtests indefensible — the 10:00 bar was priced from the 15:50 chain, which
#: knows how the day turned out. Now that intraday snapshots exist, the honest
#: answer for a bar with no prior snapshot is "no data", not "use the future".
FORWARD_TOLERANCE_MS = 15 * 60 * 1000

_COLUMNS = (
    "underlying, ts, symbol, expiry, strike, opt_right, bid, ask, last, mark, "
    "bid_size, ask_size, volume, open_interest, iv, delta, gamma, theta, vega, rho, "
    "underlying_px, dte, multiplier, is_non_standard, quote_ts"
)


@dataclass
class SnapshotRow:
    """One contract observation as written by the recorder. Deliberately flat
    and primitive-typed — it is a DB row, not a domain object."""
    underlying: str
    ts: int
    symbol: str
    expiry: int
    strike: float
    opt_right: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    volume: int | None = None
    open_interest: int | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    underlying_px: float | None = None
    dte: int | None = None
    multiplier: float = 100.0
    is_non_standard: int = 0
    quote_ts: int | None = None

    def as_tuple(self) -> tuple:
        return (self.underlying, self.ts, self.symbol, self.expiry, self.strike,
                self.opt_right, self.bid, self.ask, self.last, self.mark,
                self.bid_size, self.ask_size, self.volume, self.open_interest,
                self.iv, self.delta, self.gamma, self.theta, self.vega, self.rho,
                self.underlying_px, self.dte, self.multiplier,
                self.is_non_standard, self.quote_ts)


class OptionChainStore:
    def __init__(self, db: Database):
        self._db = db

    # ---- writes ---------------------------------------------------------
    async def upsert_snapshot(self, rows: list[SnapshotRow]) -> int:
        """Idempotent by (symbol, ts). Returns rows written."""
        if not rows:
            return 0
        placeholders = ",".join(["?"] * 25)
        await self._db.conn.executemany(
            f"INSERT INTO option_chain_snapshots ({_COLUMNS}) VALUES ({placeholders}) "
            f"ON CONFLICT(symbol, ts) DO UPDATE SET "
            f"bid=excluded.bid, ask=excluded.ask, last=excluded.last, mark=excluded.mark, "
            f"bid_size=excluded.bid_size, ask_size=excluded.ask_size, "
            f"volume=excluded.volume, open_interest=excluded.open_interest, "
            f"iv=excluded.iv, delta=excluded.delta, gamma=excluded.gamma, "
            f"theta=excluded.theta, vega=excluded.vega, rho=excluded.rho, "
            f"underlying_px=excluded.underlying_px, dte=excluded.dte, "
            f"quote_ts=excluded.quote_ts",
            [r.as_tuple() for r in rows],
        )
        await self._db.conn.commit()
        return len(rows)

    async def record_snapshot_run(self, underlying: str, ts: int, *, requested: int,
                                  written: int, rejected: int,
                                  reject_reasons: dict[str, int],
                                  underlying_px: float | None = None,
                                  error: str | None = None) -> None:
        await self._db.execute(
            "INSERT INTO option_snapshot_runs (underlying, ts, requested, written, "
            "rejected, reject_reasons_json, underlying_px, error) VALUES (?,?,?,?,?,?,?,?)",
            (underlying, ts, requested, written, rejected,
             json.dumps(reject_reasons), underlying_px, error),
        )

    async def upsert_expirations(self, underlying: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        now = int(time.time() * 1000)
        await self._db.conn.executemany(
            "INSERT INTO option_expirations (underlying, expiry, expiration_type, "
            "settlement_type, standard, option_roots, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(underlying, expiry) DO UPDATE SET "
            "expiration_type=excluded.expiration_type, "
            "settlement_type=excluded.settlement_type, standard=excluded.standard, "
            "option_roots=excluded.option_roots, updated_at=excluded.updated_at",
            [(underlying, r["expiry"], r.get("expiration_type"), r.get("settlement_type"),
              int(r.get("standard", 1)), r.get("option_roots"), now) for r in rows],
        )
        await self._db.conn.commit()
        return len(rows)

    # ---- reads ----------------------------------------------------------
    async def snapshot_ts_near(self, underlying: str, ts: int,
                               tolerance_ms: int = DEFAULT_TOLERANCE_MS,
                               forward_ms: int = FORWARD_TOLERANCE_MS) -> int | None:
        """The snapshot timestamp nearest `ts`, or None.

        Prefers the most recent snapshot at or before `ts`, reaching back up to
        `tolerance_ms`. Only if none exists does it reach FORWARD, and then by
        at most `forward_ms` — using a later snapshot to price an earlier bar is
        look-ahead, so the forward reach is a narrow allowance for clock skew
        between a bar close and the snapshot job that follows it, not a general
        fallback. See FORWARD_TOLERANCE_MS."""
        row = await self._db.fetch_one(
            "SELECT MAX(ts) t FROM option_chain_snapshots "
            "WHERE underlying=? AND ts<=? AND ts>=?",
            (underlying, ts, ts - tolerance_ms),
        )
        if row and row["t"] is not None:
            return int(row["t"])
        row = await self._db.fetch_one(
            "SELECT MIN(ts) t FROM option_chain_snapshots "
            "WHERE underlying=? AND ts>? AND ts<=?",
            (underlying, ts, ts + min(forward_ms, tolerance_ms)),
        )
        return int(row["t"]) if row and row["t"] is not None else None

    async def read_chain(self, underlying: str, ts: int,
                         tolerance_ms: int = DEFAULT_TOLERANCE_MS,
                         forward_ms: int = FORWARD_TOLERANCE_MS) -> pd.DataFrame:
        """The whole chain as recorded at (or nearest to) `ts`. Empty frame if
        no snapshot is within tolerance."""
        snap_ts = await self.snapshot_ts_near(underlying, ts, tolerance_ms, forward_ms)
        if snap_ts is None:
            return pd.DataFrame()
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM option_chain_snapshots WHERE underlying=? AND ts=?",
            (underlying, snap_ts),
        )
        return pd.DataFrame([dict(r) for r in rows])

    async def read_contract(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """The recorded price series for ONE contract — the closest thing to
        historical option bars that will ever exist here."""
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM option_chain_snapshots "
            f"WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
            (symbol, start_ms, end_ms),
        )
        return pd.DataFrame([dict(r) for r in rows])

    async def read_quote(self, symbol: str, ts: int,
                         tolerance_ms: int = DEFAULT_TOLERANCE_MS) -> ContractQuote | None:
        """One recorded quote as a domain object, or None."""
        if not is_occ(symbol):
            return None
        row = await self._db.fetch_one(
            f"SELECT {_COLUMNS} FROM option_chain_snapshots "
            f"WHERE symbol=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
            (symbol, ts, ts - tolerance_ms),
        )
        if row is None:
            return None
        return row_to_quote(dict(row))

    async def read_expirations(self, underlying: str, as_of_ms: int) -> list[Expiration]:
        """Recorded expirations still in the future as of `as_of_ms`.

        Returns [] when nothing was recorded — the caller (expression.py) then
        falls back to `expiry.generate_expirations`, which is the historical
        path. Deliberately NOT filtered by when the row was recorded: an
        expiration date is a fact about the contract, not about our recording,
        so a row captured today correctly describes an expiry that also existed
        last month."""
        rows = await self._db.fetch_all(
            "SELECT expiry, expiration_type, settlement_type, standard "
            "FROM option_expirations WHERE underlying=? AND expiry>=? ORDER BY expiry",
            (underlying, as_of_ms),
        )
        out: list[Expiration] = []
        for r in rows:
            out.append(Expiration(
                expiry=ms_to_expiry(r["expiry"]),
                expiration_type=r["expiration_type"] or "S",
                standard=bool(r["standard"]),
                settlement_type=r["settlement_type"] or "P"))
        return out

    # ---- coverage / diagnostics ----------------------------------------
    async def coverage(self, underlying: str) -> dict | None:
        row = await self._db.fetch_one(
            "SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n, COUNT(DISTINCT ts) snaps, "
            "COUNT(DISTINCT expiry) expiries FROM option_chain_snapshots WHERE underlying=?",
            (underlying,),
        )
        if not row or not row["n"]:
            return None
        return {
            "underlying": underlying,
            "first_ts": row["a"], "last_ts": row["b"],
            "rows": row["n"], "snapshots": row["snaps"], "expiries": row["expiries"],
            "mean_contracts_per_snapshot": round(row["n"] / row["snaps"], 1) if row["snaps"] else 0,
        }

    async def all_coverage(self) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT underlying, MIN(ts) a, MAX(ts) b, COUNT(*) n, COUNT(DISTINCT ts) snaps, "
            "COUNT(DISTINCT expiry) expiries FROM option_chain_snapshots "
            "GROUP BY underlying ORDER BY underlying"
        )
        return [{
            "underlying": r["underlying"], "first_ts": r["a"], "last_ts": r["b"],
            "rows": r["n"], "snapshots": r["snaps"], "expiries": r["expiries"],
            "mean_contracts_per_snapshot": round(r["n"] / r["snaps"], 1) if r["snaps"] else 0,
        } for r in rows]

    # ---- streamed ticks -------------------------------------------------
    async def insert_ticks(self, rows: list[dict]) -> int:
        """Batch-append streamed quotes.

        INSERT OR IGNORE, not REPLACE: (symbol, ts) collisions mean two quotes
        carried the same Schwab quote-time, and the first one written is as good
        as the second. REPLACE would rewrite a page for no gain, on the hottest
        write path in the app."""
        if not rows:
            return 0
        await self._db.conn.executemany(
            "INSERT OR IGNORE INTO option_tick_quotes ("
            "symbol, ts, underlying, bid, ask, mark, last, bid_size, ask_size, "
            "volume, open_interest, iv, delta, gamma, theta, vega, rho, underlying_px"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["symbol"], r["ts"], r["underlying"], r.get("bid"), r.get("ask"),
              r.get("mark"), r.get("last"), r.get("bid_size"), r.get("ask_size"),
              r.get("volume"), r.get("open_interest"), r.get("iv"), r.get("delta"),
              r.get("gamma"), r.get("theta"), r.get("vega"), r.get("rho"),
              r.get("underlying_px")) for r in rows],
        )
        await self._db.conn.commit()
        return len(rows)

    async def read_ticks(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """One contract's streamed quote series — the high-resolution version of
        `read_contract`."""
        rows = await self._db.fetch_all(
            "SELECT * FROM option_tick_quotes WHERE symbol=? AND ts>=? AND ts<=? "
            "ORDER BY ts", (symbol, start_ms, end_ms))
        return pd.DataFrame([dict(r) for r in rows])

    async def read_ticks_for_underlying(self, underlying: str, start_ms: int,
                                        end_ms: int) -> list[dict]:
        """Every recorded quote for one underlying's contracts in a window.

        Plain dicts, not a DataFrame: this crosses a process boundary into the
        backtest pool, and only four columns are read there."""
        rows = await self._db.fetch_all(
            "SELECT symbol, ts, bid, ask, mark FROM option_tick_quotes "
            "WHERE underlying=? AND ts>=? AND ts<=? ORDER BY symbol, ts",
            ((underlying or "").strip().upper().lstrip("$"), start_ms, end_ms))
        return [dict(r) for r in rows]

    async def prune_ticks(self, before_ms: int) -> int:
        cur = await self._db.conn.execute(
            "DELETE FROM option_tick_quotes WHERE ts < ?", (before_ms,))
        await self._db.conn.commit()
        return cur.rowcount or 0

    async def tick_coverage(self, days: int = 7) -> list[dict]:
        """Rows and distinct contracts per underlying per day. The volume number
        to watch: this table grows far faster than the snapshot table."""
        cutoff = int(time.time() * 1000) - days * 86_400_000
        rows = await self._db.fetch_all(
            "SELECT underlying, date(ts/1000,'unixepoch','localtime') d, "
            "COUNT(*) n, COUNT(DISTINCT symbol) contracts, "
            "MIN(ts) a, MAX(ts) b FROM option_tick_quotes WHERE ts>=? "
            "GROUP BY underlying, d ORDER BY d DESC, underlying", (cutoff,))
        return [{"underlying": r["underlying"], "date": r["d"], "rows": r["n"],
                 "contracts": r["contracts"], "first_ts": r["a"], "last_ts": r["b"]}
                for r in rows]

    async def tick_summary(self) -> dict:
        """Totals across the whole tick table: how much, and how far back.

        Separate from `tick_coverage`, which is windowed by `days` — the depth
        of history is exactly the thing a windowed query cannot tell you."""
        row = await self._db.fetch_one(
            "SELECT COUNT(*) n, COUNT(DISTINCT symbol) contracts, "
            "COUNT(DISTINCT underlying) underlyings, MIN(ts) a, MAX(ts) b, "
            "COUNT(DISTINCT date(ts/1000,'unixepoch','localtime')) days "
            "FROM option_tick_quotes")
        if not row or not row["n"]:
            return {"rows": 0, "contracts": 0, "underlyings": 0,
                    "first_ts": None, "last_ts": None, "days": 0}
        return {"rows": row["n"], "contracts": row["contracts"],
                "underlyings": row["underlyings"], "first_ts": row["a"],
                "last_ts": row["b"], "days": row["days"]}

    async def intraday_coverage(self, underlying: str,
                                days: int = 30) -> list[dict]:
        """Snapshots per SESSION DAY, with the first and last slot of each.

        The number that decides whether an intraday backtest is defensible. A
        day showing `snapshots: 1` is a day whose only chain is the 15:50 one,
        and any intraday result over that day is the synthetic pricer talking,
        not recorded data — this is what makes that visible per-day instead of
        being averaged away by `coverage`."""
        cutoff = int(time.time() * 1000) - days * 86_400_000
        rows = await self._db.fetch_all(
            "SELECT date(ts/1000, 'unixepoch', 'localtime') d, "
            "COUNT(DISTINCT ts) snaps, MIN(ts) a, MAX(ts) b, COUNT(*) n "
            "FROM option_chain_snapshots WHERE underlying=? AND ts>=? "
            "GROUP BY d ORDER BY d DESC",
            (underlying, cutoff),
        )
        return [{"date": r["d"], "snapshots": r["snaps"], "rows": r["n"],
                 "first_ts": r["a"], "last_ts": r["b"]} for r in rows]

    async def recent_runs(self, limit: int = 50) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT underlying, ts, requested, written, rejected, reject_reasons_json, "
            "underlying_px, error FROM option_snapshot_runs ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["reject_reasons"] = json.loads(d.pop("reject_reasons_json") or "{}")
            out.append(d)
        return out

    async def underlyings(self) -> list[str]:
        rows = await self._db.fetch_all(
            "SELECT DISTINCT underlying FROM option_chain_snapshots ORDER BY underlying")
        return [r["underlying"] for r in rows]


# ---- row <-> domain -------------------------------------------------------

def row_to_quote(row: dict) -> ContractQuote | None:
    """Build a `ContractQuote` from a stored row. Returns None for a row whose
    OCC symbol will not parse, rather than raising — a single corrupt row must
    not take down a whole backtest."""
    sym = row.get("symbol") or ""
    if not is_occ(sym):
        return None
    occ = parse_occ(sym)
    contract = Contract(
        underlying=canonical_underlying(occ.underlying), expiry=occ.expiry, right=occ.right,
        strike=occ.strike, multiplier=float(row.get("multiplier") or 100.0),
        is_non_standard=bool(row.get("is_non_standard")))
    bid = _f(row.get("bid"))
    ask = _f(row.get("ask"))
    mark = _f(row.get("mark"))
    mid = mark if mark else ((bid + ask) / 2 if (bid or ask) else 0.0)
    return ContractQuote(
        contract=contract, ts=int(row["ts"]), mid=mid, bid=bid, ask=ask,
        underlying_px=_f(row.get("underlying_px")),
        iv=_f(row.get("iv")),
        greeks=Greeks(delta=_f(row.get("delta")), gamma=_f(row.get("gamma")),
                      theta=_f(row.get("theta")), vega=_f(row.get("vega")),
                      rho=_f(row.get("rho"))),
        volume=row.get("volume"), open_interest=row.get("open_interest"),
        source="recorded")


def _f(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0  # NaN -> 0.0


def contract_to_row(q: ContractQuote, underlying: str, ts: int) -> SnapshotRow:
    c = q.contract
    return SnapshotRow(
        underlying=underlying, ts=ts, symbol=c.symbol,
        expiry=expiry_to_ms(c.expiry), strike=float(c.strike), opt_right=c.right,
        bid=q.bid, ask=q.ask, mark=q.mid, iv=q.iv,
        delta=q.greeks.delta, gamma=q.greeks.gamma, theta=q.greeks.theta,
        vega=q.greeks.vega, rho=q.greeks.rho,
        underlying_px=q.underlying_px, volume=q.volume, open_interest=q.open_interest,
        multiplier=c.multiplier, is_non_standard=int(c.is_non_standard),
        dte=(c.expiry - dt.datetime.fromtimestamp(ts / 1000).date()).days,
    )
