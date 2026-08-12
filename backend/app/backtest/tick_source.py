"""Recorded option quotes as a backtest price source.

The synthetic pricer answers "what SHOULD this contract be worth"; this answers
"what WAS it quoted at". Where a recording exists the second is strictly better,
and where it does not this module returns None so the caller falls back
explicitly rather than silently — a backtest that cannot say which of its fills
were observed and which were modelled is not defensible, so the split is
counted and reported in `OptionsRuntime.assumptions()`.

Two rules, both load-bearing:

  * BACKWARD ONLY. A quote is visible to a bar only if it was recorded at or
    before that bar's timestamp. Reaching forward — even by seconds — imports
    information the strategy could not have had, and on an option that is
    repricing fast (which is exactly when it matters) that is a large and
    flattering error. `options/store.py` documents the same asymmetry for
    chain snapshots.

  * A MAX AGE. Beyond it the answer is None, not the last known price. Option
    quotes go stale in a way equity marks do not: a contract that has not
    quoted in twenty minutes has an ask that no one will honour, and treating
    it as live manufactures fills at prices that never existed.

Data arrives as plain arrays (not a DB handle) because backtests execute in a
ProcessPoolExecutor: everything crosses a pickle boundary, so the ticks are
loaded once in the async layer and shipped in with the candles.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

#: How stale a recorded quote may be and still price a bar. Deliberately much
#: tighter than the chain-snapshot tolerance: this source exists precisely to
#: capture fast repricing, so a wide window here would defeat its purpose while
#: appearing to improve coverage.
DEFAULT_MAX_AGE_MS = 60_000


@dataclass
class ContractTicks:
    """One contract's recorded series, as parallel sorted arrays."""
    ts: list[int] = field(default_factory=list)
    bid: list[float] = field(default_factory=list)
    ask: list[float] = field(default_factory=list)
    mark: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.ts)


@dataclass
class TickQuoteSource:
    """Recorded ticks for a whole backtest, keyed by OCC symbol."""

    by_symbol: dict[str, ContractTicks] = field(default_factory=dict)
    max_age_ms: int = DEFAULT_MAX_AGE_MS
    hits: int = 0
    misses: int = 0

    @classmethod
    def from_rows(cls, rows, *, max_age_ms: int = DEFAULT_MAX_AGE_MS) -> "TickQuoteSource":
        """Build from row dicts (symbol, ts, bid, ask, mark), any order."""
        by_symbol: dict[str, ContractTicks] = {}
        for r in rows:
            sym = r.get("symbol")
            ts = r.get("ts")
            bid, ask = r.get("bid"), r.get("ask")
            if not sym or ts is None or bid is None or ask is None:
                continue
            by_symbol.setdefault(sym, ContractTicks()).ts.append(int(ts))
            c = by_symbol[sym]
            c.bid.append(float(bid))
            c.ask.append(float(ask))
            mark = r.get("mark")
            c.mark.append(float(mark) if mark is not None
                          else (float(bid) + float(ask)) / 2)
        for c in by_symbol.values():
            if c.ts != sorted(c.ts):
                order = sorted(range(len(c.ts)), key=lambda i: c.ts[i])
                c.ts = [c.ts[i] for i in order]
                c.bid = [c.bid[i] for i in order]
                c.ask = [c.ask[i] for i in order]
                c.mark = [c.mark[i] for i in order]
        return cls(by_symbol=by_symbol, max_age_ms=max_age_ms)

    def __len__(self) -> int:
        return sum(len(c) for c in self.by_symbol.values())

    @property
    def contracts(self) -> int:
        return len(self.by_symbol)

    def quote(self, symbol: str, ts_ms: int) -> tuple[float, float, float] | None:
        """(bid, ask, mark) as last recorded at or before `ts_ms`, or None."""
        c = self.by_symbol.get(symbol)
        if c is None or not c.ts:
            self.misses += 1
            return None
        # rightmost index with ts <= ts_ms
        i = bisect.bisect_right(c.ts, ts_ms) - 1
        if i < 0 or (self.max_age_ms and ts_ms - c.ts[i] > self.max_age_ms):
            self.misses += 1
            return None
        self.hits += 1
        return c.bid[i], c.ask[i], c.mark[i]

    def extremes(self, symbol: str, start_ms: int, end_ms: int
                 ) -> tuple[float, float] | None:
        """(min mark, max mark) recorded inside (start_ms, end_ms].

        The intrabar path. A bar's open/close pair says nothing about what the
        premium did in between, and a premium stop that is only checked at the
        close is a stop that never fires on the move it exists to catch — the
        position is simply marked back down at the close as if nothing happened.
        Returns None when the window holds no recorded quote, which the caller
        must treat as "unknown", never as "unchanged"."""
        c = self.by_symbol.get(symbol)
        if c is None or not c.ts:
            return None
        lo = bisect.bisect_right(c.ts, start_ms)
        hi = bisect.bisect_right(c.ts, end_ms)
        if lo >= hi:
            return None
        window = c.mark[lo:hi]
        return min(window), max(window)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "contracts": self.contracts,
            "ticks": len(self),
            "quote_hits": self.hits,
            "quote_misses": self.misses,
            "hit_rate_pct": round(self.hits / total * 100, 2) if total else None,
            "max_age_ms": self.max_age_ms,
        }
