"""Walk-forward fold planning (docs/plan/14-optimizer.md §5).

This module is the anti-overfitting core of the optimizer. Its job is to
answer one question honestly: *given the data that actually exists on disk,
what is the most credible evaluation we can run — and is that credible enough
to run at all?*

Three rules it exists to enforce:

1. **The plan comes from MEASURED coverage, not a typed-in date range.** A
   user asking for 2020-2026 on 5m data (which only reaches back to
   2025-11-10) must not silently get an 8-month study labelled as a 6-year
   one. `detect_coverage` reads the candles table.

2. **A trailing slice is held out and never shown to the sampler.** Scored
   once, at the end, on the champion only. Not a selection criterion.

3. **Too little data is a refusal, not a warning.** 1m history is ~6 weeks;
   no honest walk-forward study fits in that. Better to say so than to emit
   an authoritative-looking number computed from noise.

Nothing here triggers a Schwab backfill: a 400-trial study must never fire
network requests. The user backfills on the Data page; the optimizer reads
what is there.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..strategy.timeframe import BAR_MS

# Fraction of the whole usable span reserved as a never-optimized holdout.
HOLDOUT_FRACTION = 0.2

# Hard minimums per slice. A test slice below this can't produce a
# statistically meaningful metric no matter how good the strategy is; a train
# slice below it makes the IS-OOS gap diagnostic meaningless.
MIN_TEST_BARS = 250
MIN_TRAIN_BARS = 750

# Default fold counts by bar size, from the coverage actually observed in this
# project's DB (see LIVING-DOC "Phase 8 checklist"). These are defaults, not
# limits -- `plan_folds` reduces them further if coverage can't support them.
DEFAULT_FOLDS: dict[str, int] = {
    "1d": 5, "1h": 3, "30m": 3, "15m": 3, "5m": 3, "1m": 3,
}

# Bars below which we refuse outright, regardless of fold arithmetic.
MIN_TOTAL_BARS = MIN_TRAIN_BARS + 2 * MIN_TEST_BARS


class InsufficientDataError(Exception):
    """Raised when measured coverage cannot support a credible study.

    Carries `detail` (a dict) so the API can return the coverage it found
    alongside the message -- "not enough data" with no numbers is a useless
    error for a user deciding what to backfill.
    """

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


@dataclass(frozen=True)
class Fold:
    """One walk-forward step. Timestamps are epoch-ms, half-open [start, end)."""

    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int

    # Sizes, carried alongside the timestamps so the UI never has to derive
    # them from (end - start) / bar_duration -- that arithmetic is wrong on a
    # real market timeline (weekends, holidays, overnight gaps), which is the
    # same trap that once put the holdout in the middle of the series.
    train_bars: int = 0
    test_bars: int = 0
    # Distinct SESSION DATES in the test slice: how many trading days each
    # out-of-sample leg actually covers. This is the number to reason about
    # when setting `min_trades_per_fold` -- "30 trades" means something very
    # different over 12 days than over 300.
    test_days: int = 0
    train_days: int = 0

    def to_json(self) -> dict:
        return {"index": self.index, "train_start": self.train_start,
                "train_end": self.train_end, "test_start": self.test_start,
                "test_end": self.test_end, "train_bars": self.train_bars,
                "test_bars": self.test_bars, "test_days": self.test_days,
                "train_days": self.train_days}


@dataclass(frozen=True)
class Plan:
    bar: str
    symbols: tuple[str, ...]
    # full measured coverage, before any splitting
    coverage_start: int
    coverage_end: int
    total_bars: int
    # the search region (everything before the holdout)
    search_start: int
    search_end: int
    holdout_start: int
    holdout_end: int
    holdout_bars: int
    folds: tuple[Fold, ...]
    mode: str                       # "rolling" | "anchored"
    warnings: tuple[str, ...] = ()
    per_symbol: dict = field(default_factory=dict)
    # trading days (distinct session dates), not calendar days
    total_days: int = 0
    holdout_days: int = 0
    bars_per_day: float = 0.0

    def to_json(self) -> dict:
        return {
            "bar": self.bar, "symbols": list(self.symbols),
            "coverage_start": self.coverage_start, "coverage_end": self.coverage_end,
            "total_bars": self.total_bars,
            "search_start": self.search_start, "search_end": self.search_end,
            "holdout_start": self.holdout_start, "holdout_end": self.holdout_end,
            "holdout_bars": self.holdout_bars,
            "folds": [f.to_json() for f in self.folds],
            "mode": self.mode, "warnings": list(self.warnings),
            "per_symbol": self.per_symbol,
            "total_days": self.total_days, "holdout_days": self.holdout_days,
            "bars_per_day": round(self.bars_per_day, 2),
        }


async def detect_coverage(db, symbols: list[str], bar: str) -> dict:
    """Measured candle coverage for a universe at a bar size, INTERSECTED
    across symbols — a multi-symbol study can only evaluate over the window
    where every symbol has data, otherwise early folds would quietly be
    single-symbol backtests wearing a multi-symbol label.

    Reads the `candles` table only. `1h` has no rows of its own (store.py
    aggregates it from 1m on read), so it is measured against its base
    timeframe.
    """
    from ..marketdata.store import base_tf

    stored_tf = base_tf(bar)
    per_symbol: dict[str, dict] = {}
    for sym in symbols:
        row = await db.fetch_one(
            "SELECT COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi FROM candles "
            "WHERE symbol=? AND tf=?", (sym, stored_tf))
        n = int(row["n"]) if row and row["n"] else 0
        per_symbol[sym] = {"bars": n, "start": row["lo"] if n else None,
                           "end": row["hi"] if n else None, "stored_tf": stored_tf}

    covered = [v for v in per_symbol.values() if v["bars"] > 0]
    if not covered or len(covered) != len(symbols):
        missing = [s for s, v in per_symbol.items() if v["bars"] == 0]
        raise InsufficientDataError(
            f"no {bar} candles stored for {', '.join(missing)} — backfill them on the Data "
            "page first (a study never backfills on its own; it would fire one request per "
            "trial)",
            {"per_symbol": per_symbol, "bar": bar})

    # Which symbol's history is the binding constraint. A single
    # recently-listed ticker in an otherwise deep universe silently truncates
    # the whole study to its own short life, and reporting only the
    # intersection makes that look like a data-provider limit rather than a
    # one-symbol problem the user can just fix. Found live (2026-07-26): a 9
    # symbol 30m strategy refused with "373 bars" because one symbol listed 6
    # weeks earlier, while the other 8 had 2288 bars each.
    start = max(v["start"] for v in covered)
    end = min(v["end"] for v in covered)
    limiter = max(per_symbol.items(), key=lambda kv: kv[1]["start"] or 0)[0]
    others = [v["bars"] for s, v in per_symbol.items() if s != limiter]
    limiting_note = None
    if others and min(others) > per_symbol[limiter]["bars"] * 1.5:
        limiting_note = (
            f"{limiter} is the limiting symbol: its {bar} history only starts "
            f"{_d(per_symbol[limiter]['start'])} ({per_symbol[limiter]['bars']} bars), while the "
            f"rest of the universe reaches back to {_d(min(v['start'] for s, v in per_symbol.items() if s != limiter))} "
            f"(~{min(others)} bars each). Removing {limiter} from this strategy's universe would "
            f"give the study roughly {min(others)} bars to work with."
        )
    if end <= start:
        raise InsufficientDataError(
            f"the {bar} coverage of these symbols does not overlap", {"per_symbol": per_symbol})

    # The ACTUAL bar timestamps in the intersected window, not a bar count.
    #
    # This matters more than it looks. Market data is full of gaps -- weekends,
    # holidays, and overnight for intraday bars -- so a bar count cannot be
    # converted back into a date by multiplying by the bar duration. Doing that
    # with 5032 daily SPY bars starting in 2006 lands in 2020, which would
    # silently drop six years of history AND put the "most recent 20%" holdout
    # somewhere in the middle of the series. Found exactly that way in an
    # end-to-end run against the real DB.
    #
    # One symbol's timeline is enough to index against: the fold boundaries are
    # timestamps, and every symbol is filtered by the same [start, end) window
    # when its frames are sliced.
    anchor = max(symbols, key=lambda s: per_symbol[s]["bars"])
    rows = await db.fetch_all(
        "SELECT DISTINCT ts FROM candles WHERE symbol=? AND tf=? AND ts>=? AND ts<=? "
        "ORDER BY ts", (anchor, stored_tf, start, end))
    timestamps = [int(r["ts"]) for r in rows]
    if not timestamps:
        raise InsufficientDataError(
            f"no {bar} candles in the overlapping window", {"per_symbol": per_symbol})

    return {"start": timestamps[0], "end": timestamps[-1], "bars": len(timestamps),
            "timestamps": timestamps, "bar": bar, "per_symbol": per_symbol,
            "limiting_note": limiting_note}


def plan_folds(coverage: dict, symbols: list[str], *, folds: int | None = None,
               mode: str = "rolling") -> Plan:
    """Turn measured coverage into a fold plan, or refuse.

    Layout (rolling, K folds) over the search region:

        |<---------------- search region (80%) --------------->|<- holdout ->|
        [ train 1 ][ test 1 ]
                   [ train 2 ][ test 2 ]
                              [ train 3 ][ test 3 ]

    Each fold's test slice is the window IMMEDIATELY AFTER its train slice, so
    every reported out-of-sample number is genuinely forward in time. In
    `anchored` mode every train slice starts at the search region's start
    instead of rolling forward (more data per fit, less regime independence).
    """
    if mode not in ("rolling", "anchored"):
        raise ValueError("mode must be 'rolling' or 'anchored'")

    bar = coverage["bar"]
    step = BAR_MS[bar]
    total_bars = int(coverage["bars"])
    warnings: list[str] = []

    limiting_note = coverage.get("limiting_note")
    if total_bars < MIN_TOTAL_BARS:
        msg = (f"only ~{total_bars} {bar} bars cover ALL symbols in this universe "
               f"({_d(coverage['start'])} → {_d(coverage['end'])}); a walk-forward study needs at "
               f"least {MIN_TOTAL_BARS}. Optimizing on this little data would fit noise and "
               "report it as an edge.")
        # Name the binding symbol when there is one -- otherwise this reads as
        # "your data provider is the problem" when the fix is one edit away.
        msg += (f" {limiting_note}" if limiting_note else
                " Backfill more history, or optimize on a slower bar size where you have depth.")
        raise InsufficientDataError(
            msg,
            {"total_bars": total_bars, "required_bars": MIN_TOTAL_BARS, "bar": bar,
             "coverage_start": coverage["start"], "coverage_end": coverage["end"],
             "limiting_note": limiting_note, "per_symbol": coverage.get("per_symbol", {})})

    holdout_bars = max(MIN_TEST_BARS, int(total_bars * HOLDOUT_FRACTION))
    search_bars = total_bars - holdout_bars
    if search_bars < MIN_TRAIN_BARS + MIN_TEST_BARS:
        raise InsufficientDataError(
            f"after reserving {holdout_bars} bars as an untouched holdout, only {search_bars} "
            f"{bar} bars remain to search over (need at least "
            f"{MIN_TRAIN_BARS + MIN_TEST_BARS}). Backfill more history first.",
            {"total_bars": total_bars, "holdout_bars": holdout_bars, "bar": bar})

    # How many folds the data can actually carry. Each fold consumes a test
    # slice; the first also needs a full train slice.
    max_folds = max(1, (search_bars - MIN_TRAIN_BARS) // MIN_TEST_BARS)
    requested = folds if folds is not None else DEFAULT_FOLDS.get(bar, 3)
    if requested < 1:
        raise ValueError("folds must be >= 1")
    k = min(requested, max_folds)
    if k < requested:
        warnings.append(
            f"asked for {requested} folds but the stored {bar} history only supports {k} "
            f"(each fold needs >= {MIN_TEST_BARS} out-of-sample bars)")
    if k < 3:
        warnings.append(
            f"only {k} out-of-sample fold(s) — consistency across folds is the main defence "
            "against overfitting here, so treat this study's result as weak evidence")

    # Split the search region into one train slice plus k test slices. The
    # even split is preferred when there's data to spare, but the train slice
    # has priority: a fit on too little history is a worse problem than a
    # slightly shorter out-of-sample leg, and `max_folds` above already
    # guaranteed MIN_TRAIN_BARS + k*MIN_TEST_BARS fits.
    test_bars = search_bars // (k + 1)
    train_bars = search_bars - k * test_bars
    if train_bars < MIN_TRAIN_BARS:
        test_bars = (search_bars - MIN_TRAIN_BARS) // k
        train_bars = search_bars - k * test_bars
    if test_bars < MIN_TEST_BARS or train_bars < MIN_TRAIN_BARS:  # pragma: no cover - guarded above
        raise InsufficientDataError(
            f"cannot fit {k} folds into {search_bars} {bar} bars", {"bar": bar})

    # Boundaries are read out of the REAL bar timestamps (see detect_coverage):
    # bar index -> timestamp, never bar-count arithmetic. `at(n)` maps a bar
    # index to the half-open boundary at that index, with the final boundary
    # one bar past the last real bar so the last bar is included.
    ts = coverage.get("timestamps")
    if not ts:  # pragma: no cover - detect_coverage always supplies these
        raise InsufficientDataError("coverage is missing bar timestamps", {"bar": bar})

    # Trading days per bar-index range: the count of DISTINCT session dates,
    # taken straight from the bar timestamps. Deliberately not derived from
    # the calendar span or from an assumed session length -- holidays, half
    # days and (for intraday bars) the overnight gap all make those wrong, and
    # this is the number the user sets `min_trades_per_fold` against.
    def days_between(lo: int, hi: int) -> int:
        import datetime as _dt
        return len({_dt.datetime.fromtimestamp(t / 1000, _dt.UTC).date()
                    for t in ts[lo:hi]})

    def at(n: int) -> int:
        if n >= len(ts):
            return int(ts[-1]) + step
        return int(ts[max(0, n)])

    total_days = days_between(0, total_bars)
    origin = at(0)
    search_start = origin
    search_end = at(search_bars)
    holdout_start, holdout_end = search_end, at(total_bars)

    fold_list: list[Fold] = []
    for i in range(k):
        test_lo = train_bars + i * test_bars
        test_hi = train_bars + (i + 1) * test_bars
        train_lo = 0 if mode == "anchored" else i * test_bars
        test_start, test_end = at(test_lo), at(test_hi)
        train_start = search_start if mode == "anchored" else at(train_lo)
        fold_list.append(Fold(
            index=i, train_start=train_start, train_end=test_start,
            test_start=test_start, test_end=test_end,
            train_bars=test_lo - train_lo, test_bars=test_hi - test_lo,
            test_days=days_between(test_lo, test_hi),
            train_days=days_between(train_lo, test_lo)))

    # Even when the study CAN run, a short-history symbol is worth flagging:
    # it caps the whole universe, so the user is optimizing over less data
    # than they think they have.
    if limiting_note:
        warnings.append(limiting_note)

    if bar in ("1m",):
        warnings.append(
            "1m history is short by nature (Schwab serves ~6 weeks) — a 1m study covers a "
            "handful of market regimes at most")

    return Plan(
        bar=bar, symbols=tuple(symbols),
        coverage_start=origin, coverage_end=int(coverage["end"]), total_bars=total_bars,
        search_start=search_start, search_end=search_end,
        holdout_start=holdout_start, holdout_end=holdout_end, holdout_bars=holdout_bars,
        folds=tuple(fold_list), mode=mode, warnings=tuple(warnings),
        per_symbol=coverage.get("per_symbol", {}),
        total_days=total_days, holdout_days=days_between(search_bars, total_bars),
        bars_per_day=(total_bars / total_days) if total_days else 0.0,
    )


def _d(ms: int | None) -> str:
    import datetime as _dt
    if not ms:
        return "?"
    return _dt.datetime.fromtimestamp(ms / 1000, _dt.UTC).strftime("%Y-%m-%d")
