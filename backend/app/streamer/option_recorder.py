"""ChainRecorder — builds the option price history Schwab will not sell you.

Schwab's /pricehistory is equity-only. There is no endpoint, anywhere, that
returns a past option contract's bars. So every option price this system will
ever have for a historical date exists because this recorder captured it on the
day. That makes the recorder's value a pure function of elapsed calendar time,
which is why plan/16 decision D7 says to ship it FIRST, before the engine work:
every day it is not running is a day of data that can never be recovered.

Two independent jobs:

  (a) a daily REST chain snapshot near the close, per underlying on the options
      watchlist -> `option_chain_snapshots`, plus `GET /expirationchain` ->
      `option_expirations`;

  (b) a LEVELONE_OPTIONS stream handler, used by live/paper runs to mark open
      positions and monitor underlying-based stops.

The field map for (b) is NOT trusted on first contact. `streamer/recorder.py`'s
long field-map comment records that Schwab's published CHART_EQUITY table was
wrong on the wire by one position, and that it took captured raw payloads to
prove it. So raw LEVELONE_OPTIONS items are persisted verbatim to
`option_levelone_raw` for the first N messages, and the parsed path refuses to
write anything that fails a self-consistency check.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from zoneinfo import ZoneInfo

from ..db import Database
from ..logging import get_logger
from ..options.chain_service import ChainService
from ..options.contracts import Contract, ContractQuote, Greeks
from ..options.store import OptionChainStore
from ..options.symbology import is_occ, parse_occ, underlying_of
from .client import SERVICE_FIELDS, StreamerClient

log = get_logger("option_recorder")

ET = ZoneInfo("America/New_York")

#: Watchlist key in app_state. Deliberately SEPARATE from the equity
#: `recording_watchlist`: recording SPY 1-minute bars and recording SPY's option
#: chain are different decisions with different costs.
WATCHLIST_KEY = "option_recording_watchlist"

#: Snapshot at 15:50 ET — liquid, and before the post-close quote decay that
#: makes 16:05 chains useless for calibration. Always the LAST slot of the day,
#: and the one every pre-existing snapshot in the table was taken at, so it
#: stays the anchor that intraday history is compared against.
SNAPSHOT_ET = (15, 50)
#: First intraday slot: 15 minutes after the open, by which point the opening
#: auction's prints have settled and the book is quoting two-sided.
OPEN_SNAPSHOT_ET = (9, 45)


def snapshot_times(interval_min: int, first: str = "09:45",
                   last: str = "15:50") -> list[tuple[int, int]]:
    """The ET clock times to snapshot at, inclusive of both ends.

    `last` is ALWAYS included even when the interval does not divide evenly into
    the session — 15:50 is the calibration anchor and the slot every historical
    snapshot shares, so a cadence that happened to skip it would silently break
    continuity with every day already recorded.

    An interval of 0 or less means "daily close only", which is the pre-existing
    behaviour and is what makes this safe to default on."""
    lo_h, lo_m = _parse_et(first)
    hi_h, hi_m = _parse_et(last)
    lo, hi = lo_h * 60 + lo_m, hi_h * 60 + hi_m
    if interval_min <= 0 or lo >= hi:
        return [(hi_h, hi_m)]
    step = max(interval_min, 1)
    # Drop a generated slot that lands within half an interval of `last`: at a
    # 30m cadence the grid ends at 15:45, and recording both that and the 15:50
    # anchor spends a full extra sweep of REST budget and storage to observe
    # five minutes of the same market.
    out = [m for m in range(lo, hi, step) if hi - m >= step / 2]
    out.append(hi)
    return [(m // 60, m % 60) for m in sorted(set(out))]


def _parse_et(s: str) -> tuple[int, int]:
    h, _, m = (s or "").strip().partition(":")
    try:
        hh, mm = int(h), int(m or 0)
    except ValueError as e:
        raise ValueError(f"snapshot time must be HH:MM, got {s!r}") from e
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"snapshot time out of range: {s!r}")
    return hh, mm


DEFAULT_MAX_DTE = 180
DEFAULT_STRIKE_COUNT = 60

#: How many raw stream items to persist before trusting the field map.
RAW_CAPTURE_LIMIT = 200

#: How often the buffered ticks are written. A second bounds how much is lost
#: to a hard crash while keeping the write path off the websocket read loop.
TICK_FLUSH_INTERVAL_S = 1.0

# ---------------------------------------------------------------------------
# LEVELONE_OPTIONS field map. Source: schwab/market-data-production/README.md
# lines 884-942 (also documentation.md §2). TRANSCRIBED FROM THE TABLE, not
# adapted from the equities handler -- the two maps differ from field 1 onward
# (options field 1 is Description; equities field 1 is Bid Price), and copying
# the equity handler would shift every price by one column.
# ---------------------------------------------------------------------------
F_BID, F_ASK, F_LAST = "2", "3", "4"
F_VOLUME, F_OI, F_IV = "8", "9", "10"
F_MULTIPLIER = "13"
F_STRIKE, F_CONTRACT_TYPE, F_UNDERLYING = "20", "21", "22"
F_DTE = "27"
F_DELTA, F_GAMMA, F_THETA, F_VEGA, F_RHO = "28", "29", "30", "31", "32"
F_UNDERLYING_PX, F_MARK = "35", "37"
F_QUOTE_TIME = "38"

#: Subscribed field list — the union of what marking a position and monitoring
#: a stop actually need. Keep it tight: LEVELONE_OPTIONS is a "Change" service
#: and every extra field is bandwidth on every tick.
#:
#: Re-exported from the streamer client rather than declared here. It lived
#: here alone for a while, which is exactly how it came to be unused: the
#: client subscribes with SERVICE_FIELDS, and a service absent from that map
#: silently gets fields="0" — symbol only, no prices. One definition, one
#: place it is read from.
LEVELONE_OPTIONS_FIELDS = SERVICE_FIELDS["LEVELONE_OPTIONS"]


def select_tick_universe(rows, underlying_px: float | None, *,
                         strikes: int = 10, expiries: int = 4) -> list[str]:
    """Chain rows -> the contracts to stream, nearest-the-money first.

    `strikes` counts each side, so the default is a 21-strike band; both rights
    are taken at every strike. Ordering is load-bearing: the caller truncates
    against a global symbol cap, and what survives truncation must be the
    contracts closest to the money, since those are the ones a strategy will
    actually select and the ones whose quotes move fastest.

    Pure function so the band can be tested against a captured chain rather than
    inferred from a live subscription."""
    if not rows or not underlying_px or underlying_px <= 0:
        return []
    by_expiry: dict[int, list] = {}
    for r in rows:
        by_expiry.setdefault(r.expiry, []).append(r)

    out: list[tuple[float, int, str]] = []
    for exp_rank, expiry in enumerate(sorted(by_expiry)[:max(expiries, 1)]):
        exp_rows = by_expiry[expiry]
        # The ATM band is defined over the strikes that EXIST on this expiry,
        # not a fixed dollar width: strike spacing differs by expiry (weeklies
        # are often denser than monthlies) and by underlying, so a dollar band
        # would silently mean 40 strikes on SPY and 4 on a wide-spaced name.
        all_strikes = sorted({float(r.strike) for r in exp_rows})
        if not all_strikes:
            continue
        atm_i = min(range(len(all_strikes)),
                    key=lambda i: abs(all_strikes[i] - underlying_px))
        lo = max(0, atm_i - strikes)
        hi = min(len(all_strikes), atm_i + strikes + 1)
        keep = set(all_strikes[lo:hi])
        for r in exp_rows:
            if float(r.strike) in keep:
                out.append((abs(float(r.strike) - underlying_px), exp_rank, r.symbol))

    # Nearest strike first, then nearest expiry — dedupe preserving that order.
    out.sort(key=lambda t: (t[1], t[0], t[2]))
    seen: set[str] = set()
    ordered: list[str] = []
    for _d, _e, sym in out:
        if sym not in seen:
            seen.add(sym)
            ordered.append(sym)
    return ordered


class ChainRecorder:
    def __init__(self, db: Database, store: OptionChainStore, chains: ChainService,
                 streamer: StreamerClient | None = None, *,
                 max_dte: int = DEFAULT_MAX_DTE,
                 strike_count: int = DEFAULT_STRIKE_COUNT,
                 calendar=None,
                 intraday_stale_quote_s: int | None = None,
                 snapshot_slots: list[tuple[int, int]] | None = None,
                 tick_recording: bool = False,
                 tick_strikes: int = 10,
                 tick_expiries: int = 4,
                 tick_max_symbols: int = 1200,
                 tick_min_interval_ms: int = 1000):
        self._db = db
        self._store = store
        self._chains = chains
        self._streamer = streamer
        self._max_dte = max_dte
        self._strike_count = strike_count
        #: MarketCalendar. Optional so existing constructions keep working, but
        #: without it the only holiday guard is "is it a weekday", and at an
        #: intraday cadence that turns one junk snapshot per holiday into
        #: thirteen — each one a full chain of quotes frozen at the last close,
        #: which the calibrator cannot distinguish from real market data.
        self._calendar = calendar
        self._intraday_stale_ms = (
            int(intraday_stale_quote_s) * 1000
            if intraday_stale_quote_s else None)
        #: Reported, not used to fire: APScheduler owns the firing (main.py).
        #: Held here so the coverage endpoint can say how many snapshots a
        #: complete day SHOULD have, which is the only way to tell a thin
        #: recording day from a normal one.
        self._slots = list(snapshot_slots or [SNAPSHOT_ET])
        self._raw_captured = 0
        #: Contracts subscribed because a RUN holds a position in them. Kept
        #: separate from the recording universe below so the two can overlap
        #: without either one's unsubscribe silently blinding the other — a
        #: universe refresh that dropped the contract a live stop is watching
        #: would be a genuinely dangerous bug.
        self._subscribed: set[str] = set()
        #: Contracts subscribed purely to RECORD them.
        self._universe: set[str] = set()
        #: symbol -> last merged quote dict. LEVELONE_OPTIONS is a CHANGE
        #: service: each message carries only the fields that moved, so a
        #: handler that does not merge against the previous state will see a
        #: bid with no ask and conclude the market is broken.
        self._quotes: dict[str, dict] = {}

        # ---- tick recording state ----
        self._tick_recording = bool(tick_recording)
        self._tick_strikes = max(int(tick_strikes), 0)
        self._tick_expiries = max(int(tick_expiries), 1)
        self._tick_max_symbols = max(int(tick_max_symbols), 0)
        self._tick_min_interval_ms = max(int(tick_min_interval_ms), 0)
        #: Rows waiting to be written. The stream handler must never touch the
        #: DB inline: an ATM 0DTE contract can quote faster than SQLite will
        #: commit, and a slow write inside the handler backs up the websocket
        #: read loop for EVERY service, equities included.
        self._tick_buffer: list[dict] = []
        #: symbol -> (last persisted ts, last persisted (bid, ask, mark))
        self._tick_last: dict[str, tuple[int, tuple]] = {}
        self._flush_task: asyncio.Task | None = None
        self.ticks_recorded = 0
        self.ticks_dropped_conflated = 0
        self.ticks_dropped_unchanged = 0

        self.snapshots_taken = 0
        self.rows_recorded = 0
        if streamer is not None:
            streamer.on_data("LEVELONE_OPTIONS", self._on_levelone)

    def schedule_et(self) -> list[str]:
        """The ET clock times this recorder is scheduled for, as "HH:MM"."""
        return [f"{h:02d}:{m:02d}" for h, m in self._slots]

    # ---- watchlist ------------------------------------------------------
    async def get_watchlist(self) -> list[str]:
        return await self._db.state_get(WATCHLIST_KEY, [])

    async def set_watchlist(self, symbols: list[str]) -> list[str]:
        # `$` stripped here as well as in ChainService: a user who types the
        # Schwab request form `$SPX` must not end up with a watchlist entry
        # whose diagnostics file under `$SPX` while its rows file under `SPX`.
        symbols = sorted({s.strip().upper().lstrip("$")
                          for s in symbols if s and s.strip().strip("$")})
        await self._db.state_set(WATCHLIST_KEY, symbols)
        await self._db.audit("user", "option_watchlist", {"symbols": symbols})
        return symbols

    # ---- (a) daily snapshot --------------------------------------------
    async def snapshot_one(self, underlying: str, *, ts: int | None = None) -> dict:
        """Fetch, validate and persist one underlying's chain. Returns a summary
        dict; never raises, because one bad symbol must not abort the sweep."""
        underlying = (underlying or "").strip().upper().lstrip("$")
        ts = ts or int(time.time() * 1000)
        try:
            kw = {}
            if self._intraday_stale_ms:
                kw["stale_ms"] = self._intraday_stale_ms
            res = await self._chains.fetch(
                underlying, max_dte=self._max_dte, strike_count=self._strike_count,
                use_cache=False, **kw)
        except Exception as e:  # noqa: BLE001
            log.error("option_snapshot_failed", underlying=underlying, error=str(e))
            await self._store.record_snapshot_run(
                underlying, ts, requested=0, written=0, rejected=0,
                reject_reasons={}, error=str(e))
            return {"underlying": underlying, "error": str(e), "written": 0}

        # Stamp every row with the SWEEP's timestamp, not each row's own fetch
        # time, so all contracts in one snapshot share a key and `read_chain`
        # returns a coherent chain rather than a smear across seconds.
        for r in res.rows:
            r.ts = ts
        written = await self._store.upsert_snapshot(res.rows)
        await self._store.record_snapshot_run(
            underlying, ts, requested=res.requested, written=written,
            rejected=res.rejected, reject_reasons=dict(res.reject_reasons),
            underlying_px=res.underlying_px, error=res.error)
        self.snapshots_taken += 1
        self.rows_recorded += written
        if res.rejected:
            log.info("option_snapshot_rejections", underlying=underlying,
                     rejected=res.rejected, reasons=dict(res.reject_reasons))
        log.info("option_snapshot", underlying=underlying, requested=res.requested,
                 written=written, rejected=res.rejected, px=res.underlying_px)
        return {"underlying": underlying, "requested": res.requested,
                "written": written, "rejected": res.rejected,
                "reject_reasons": dict(res.reject_reasons),
                "underlying_px": res.underlying_px, "error": res.error}

    async def refresh_expirations(self, underlying: str) -> int:
        try:
            rows = await self._chains.fetch_expirations(underlying)
        except Exception as e:  # noqa: BLE001
            log.warning("expiration_refresh_failed", underlying=underlying, error=str(e))
            return 0
        return await self._store.upsert_expirations(underlying.strip().upper(), rows)

    async def snapshot_all(self, *, ts: int | None = None) -> list[dict]:
        """One sweep across the whole watchlist."""
        wl = await self.get_watchlist()
        if not wl:
            return []
        ts = ts or int(time.time() * 1000)
        out = []
        for sym in wl:
            out.append(await self.snapshot_one(sym, ts=ts))
            await self.refresh_expirations(sym)
            # Be a good citizen with the shared REST budget: the equity
            # backfiller and any running strategies share this rate limit.
            await asyncio.sleep(0.4)
        return out

    # ---- scheduled entrypoint -------------------------------------------
    async def scheduled_snapshot(self) -> list[dict]:
        """The APScheduler job body (wired in main.py at 15:50 ET, weekdays).

        Owns its own weekday/holiday guard rather than relying on the cron
        expression alone, so a manual trigger or a cron misconfiguration cannot
        write a snapshot of a closed market full of stale quotes — which would
        then silently poison every calibration that reads it.

        Never raises: a scheduler job that throws kills nothing but itself, and
        a failed chain snapshot must not look like a healthy one."""
        now = dt.datetime.now(tz=ET)
        if now.weekday() >= 5:
            log.info("option_snapshot_skipped_weekend", date=str(now.date()))
            return []
        # Holiday guard. At the old once-a-day cadence a holiday cost one junk
        # snapshot; at an intraday cadence it is one per slot, all of them
        # identical quotes frozen at the previous close, and NOTHING downstream
        # can tell them apart from real intraday movement — they would show up
        # in the calibration as a day of perfectly zero volatility.
        if self._calendar is not None:
            try:
                if not await self._calendar.is_open_now():
                    log.info("option_snapshot_skipped_market_closed",
                             date=str(now.date()), time=now.strftime("%H:%M"))
                    return []
            except Exception as e:  # noqa: BLE001 — a calendar outage must not
                # silently disable recording: a missed snapshot is unrecoverable
                # data loss, while a junk one is at least detectable after the
                # fact from its quote_ts column.
                log.warning("option_snapshot_calendar_check_failed", error=str(e))
        try:
            log.info("option_snapshot_sweep_start", date=str(now.date()))
            return await self.snapshot_all()
        except Exception as e:  # noqa: BLE001
            log.error("option_snapshot_sweep_error", error=str(e))
            return []

    # ---- (b) LEVELONE_OPTIONS -------------------------------------------
    async def subscribe(self, symbols: list[str]) -> None:
        """Subscribe option symbols for live marking. Idempotent."""
        if self._streamer is None:
            return
        # dict.fromkeys dedupes WITHIN the input too, not just against what is
        # already subscribed — a caller passing the same contract twice (two
        # positions on one strike) would otherwise send a duplicate key to
        # Schwab and count double against the symbol-subscription limit.
        wanted = [s for s in dict.fromkeys(symbols)
                  if is_occ(s) and s not in self._subscribed]
        if not wanted:
            return
        self._subscribed.update(wanted)
        # Already on the wire for recording? Then this is a no-op at the
        # streamer, but the position claim still has to be registered above so
        # a later universe refresh cannot unsubscribe it.
        new = [s for s in wanted if s not in self._universe]
        if new:
            await self._streamer.subscribe("LEVELONE_OPTIONS", new)
        log.info("levelone_options_subscribed", symbols=wanted)

    async def unsubscribe(self, symbols: list[str]) -> None:
        if self._streamer is None:
            return
        gone = [s for s in symbols if s in self._subscribed]
        if not gone:
            return
        for s in gone:
            self._subscribed.discard(s)
        # Only drop from the wire what nothing else still wants.
        drop = [s for s in gone if s not in self._universe]
        for s in drop:
            self._quotes.pop(s, None)
        if drop:
            await self._streamer.unsubscribe("LEVELONE_OPTIONS", drop)

    # ---- (c) tick recording universe ------------------------------------
    async def set_tick_universe(self, symbols: list[str]) -> dict:
        """Make the recorded contract set exactly `symbols`. Returns a summary.

        Diffs against the current universe rather than resubscribing wholesale:
        the ATM band shifts by a strike or two between refreshes, so a full
        SUBS/UNSUBS cycle would churn hundreds of subscriptions to change ten,
        and would drop quotes for the contracts that did not change during the
        gap."""
        wanted = {s for s in symbols if is_occ(s)}
        add = sorted(wanted - self._universe)
        remove = sorted(self._universe - wanted)
        self._universe = wanted
        if self._streamer is not None:
            # Never unsubscribe a contract a live position is watching.
            drop = [s for s in remove if s not in self._subscribed]
            fresh = [s for s in add if s not in self._subscribed]
            if drop:
                for s in drop:
                    self._quotes.pop(s, None)
                    self._tick_last.pop(s, None)
                await self._streamer.unsubscribe("LEVELONE_OPTIONS", drop)
            if fresh:
                await self._streamer.subscribe("LEVELONE_OPTIONS", fresh)
        return {"universe": len(wanted), "added": len(add), "removed": len(remove)}

    async def refresh_tick_universe(self) -> dict:
        """Rebuild the recorded contract set from live chains. Never raises.

        Scheduled periodically because the band is defined RELATIVE TO SPOT: a
        universe pinned at the open is centred on the wrong strikes by
        afternoon, and would be recording wings while the money moved
        elsewhere."""
        if not self._tick_recording:
            return {"skipped": "disabled"}
        if self._calendar is not None:
            try:
                if not await self._calendar.is_open_now():
                    # Outside the session there is nothing to record, and
                    # holding the subscription open just spends symbol budget.
                    if self._universe:
                        await self.set_tick_universe([])
                    return {"skipped": "market_closed"}
            except Exception as e:  # noqa: BLE001
                log.warning("tick_universe_calendar_check_failed", error=str(e))
        wl = await self.get_watchlist()
        if not wl:
            return {"skipped": "empty_watchlist"}

        picked: list[str] = []
        per_symbol: dict[str, int] = {}
        for sym in wl:
            try:
                res = await self._chains.fetch(
                    sym, max_dte=self._max_dte, strike_count=self._strike_count)
            except Exception as e:  # noqa: BLE001 — one bad symbol must not
                log.warning("tick_universe_fetch_failed",   # blind the others
                            underlying=sym, error=str(e))
                continue
            got = select_tick_universe(res.rows, res.underlying_px,
                                       strikes=self._tick_strikes,
                                       expiries=self._tick_expiries)
            per_symbol[sym] = len(got)
            picked.extend(got)

        # Global cap, applied nearest-the-money first. Schwab does not publish
        # the LEVELONE_OPTIONS symbol limit but does define error 19
        # (REACHED_SYMBOL_LIMIT); discovering it by having a subscription
        # rejected mid-session would cost a day of recording.
        capped = False
        if self._tick_max_symbols and len(picked) > self._tick_max_symbols:
            picked = picked[:self._tick_max_symbols]
            capped = True
            log.warning("tick_universe_capped", limit=self._tick_max_symbols,
                        wanted=sum(per_symbol.values()))
        out = await self.set_tick_universe(picked)
        out.update({"per_symbol": per_symbol, "capped": capped})
        log.info("tick_universe_refreshed", **{k: v for k, v in out.items()
                                               if k != "per_symbol"})
        return out

    def tick_universe_symbols(self, underlying: str | None = None) -> set[str]:
        """Currently-streamed contracts, optionally filtered to one underlying."""
        if underlying is None:
            return set(self._universe)
        u = underlying.strip().upper().lstrip("$")
        return {s for s in self._universe if underlying_of(s) == u}

    def tick_stats(self) -> dict:
        return {
            "enabled": self._tick_recording,
            "universe": len(self._universe),
            "position_subscriptions": len(self._subscribed),
            "buffered": len(self._tick_buffer),
            "recorded": self.ticks_recorded,
            "dropped_conflated": self.ticks_dropped_conflated,
            "dropped_unchanged": self.ticks_dropped_unchanged,
            "min_interval_ms": self._tick_min_interval_ms,
            # Reported so the UI states the ACTUAL configuration rather than
            # repeating defaults that may have been overridden in .env.
            "strikes_each_side": self._tick_strikes,
            "expiries": self._tick_expiries,
            "max_symbols": self._tick_max_symbols,
        }

    async def _on_levelone(self, content: list[dict]) -> None:
        for item in content:
            sym = item.get("key")
            if not sym:
                continue
            if self._raw_captured < RAW_CAPTURE_LIMIT:
                await self._capture_raw(sym, item)
            prev = self._quotes.get(sym, {})
            merged = {**prev, **{k: v for k, v in item.items() if v is not None}}
            self._quotes[sym] = merged
            if self._tick_recording:
                self._buffer_tick(sym, merged)

    def _buffer_tick(self, symbol: str, merged: dict) -> None:
        """Queue one merged quote for persistence, if it earns a row.

        Two filters, in this order, and the order matters. UNCHANGED comes
        first: a repeat of the same bid/ask carries no information at any
        interval, and dropping it costs nothing. CONFLATION comes second and is
        purely a volume bound on a contract that is genuinely moving — which is
        a real loss of resolution, so it is counted separately and reported."""
        if not is_occ(symbol):
            return
        bid, ask = _f(merged.get(F_BID)), _f(merged.get(F_ASK))
        if bid is None or ask is None or ask <= 0 or bid < 0 or bid > ask:
            return          # same usability bar as latest_quote
        mark = _f(merged.get(F_MARK)) or (bid + ask) / 2
        qt = merged.get(F_QUOTE_TIME)
        ts = int(qt) if isinstance(qt, (int, float)) and qt else int(time.time() * 1000)

        sig = (bid, ask, mark)
        last = self._tick_last.get(symbol)
        if last is not None:
            last_ts, last_sig = last
            if sig == last_sig:
                self.ticks_dropped_unchanged += 1
                return
            if self._tick_min_interval_ms and ts - last_ts < self._tick_min_interval_ms:
                self.ticks_dropped_conflated += 1
                return
        self._tick_last[symbol] = (ts, sig)

        iv = _f(merged.get(F_IV))
        if iv is not None and iv > 5.0:
            iv = iv / 100.0     # percentage points -> decimal (see chain_service)
        self._tick_buffer.append({
            "symbol": symbol, "ts": ts,
            "underlying": underlying_of(symbol),
            "bid": bid, "ask": ask, "mark": mark, "last": _f(merged.get(F_LAST)),
            "bid_size": None, "ask_size": None,
            "volume": _i(merged.get(F_VOLUME)),
            "open_interest": _i(merged.get(F_OI)), "iv": iv,
            "delta": _f(merged.get(F_DELTA)), "gamma": _f(merged.get(F_GAMMA)),
            "theta": _f(merged.get(F_THETA)), "vega": _f(merged.get(F_VEGA)),
            "rho": _f(merged.get(F_RHO)),
            "underlying_px": _f(merged.get(F_UNDERLYING_PX)),
        })

    # ---- tick flush loop -------------------------------------------------
    async def start_tick_flusher(self) -> None:
        if not self._tick_recording or self._flush_task is not None:
            return
        self._flush_task = asyncio.create_task(self._flush_loop(),
                                               name="option_tick_flusher")

    async def stop_tick_flusher(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush_ticks()    # never discard what is already buffered

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_FLUSH_INTERVAL_S)
            try:
                await self.flush_ticks()
            except Exception as e:  # noqa: BLE001 — the loop must outlive one
                log.warning("tick_flush_failed", error=str(e))   # bad batch

    async def flush_ticks(self) -> int:
        """Write and clear the buffer. Safe to call concurrently with the feed:
        the buffer is swapped out in one statement, and the handler only ever
        appends."""
        if not self._tick_buffer:
            return 0
        batch, self._tick_buffer = self._tick_buffer, []
        written = await self._store.insert_ticks(batch)
        self.ticks_recorded += written
        return written

    async def _capture_raw(self, symbol: str, item: dict) -> None:
        import json
        self._raw_captured += 1
        try:
            await self._db.execute(
                "INSERT INTO option_levelone_raw (received_at, symbol, raw_json) "
                "VALUES (?,?,?)",
                (int(time.time() * 1000), symbol, json.dumps(item)),
            )
        except Exception as e:  # noqa: BLE001 — diagnostics must never break the feed
            log.warning("levelone_raw_capture_failed", error=str(e))

    def latest_quote(self, symbol: str, max_age_s: float = 15.0) -> ContractQuote | None:
        """The freshest streamed quote for one contract, or None when it is
        missing, stale or internally inconsistent.

        None is a MEANINGFUL answer here, not a failure: plan/16 §9.2 requires
        the runner to BLOCK an entry rather than fall back to a modelled price,
        so returning a dubious quote would be worse than returning nothing."""
        raw = self._quotes.get(symbol)
        if not raw or not is_occ(symbol):
            return None
        bid, ask = _f(raw.get(F_BID)), _f(raw.get(F_ASK))
        if bid is None or ask is None or ask <= 0 or bid < 0 or bid > ask:
            return None
        qt = raw.get(F_QUOTE_TIME)
        ts = int(qt) if isinstance(qt, (int, float)) and qt else int(time.time() * 1000)
        if max_age_s and (time.time() * 1000 - ts) > max_age_s * 1000:
            return None
        occ = parse_occ(symbol)
        # Self-consistency check against the symbol itself. This is the cheap
        # guard that would have caught the CHART_EQUITY field shift immediately:
        # if the map is off by one, the "strike" field will not match the strike
        # encoded in the symbol we subscribed.
        strike_field = _f(raw.get(F_STRIKE))
        if strike_field is not None and abs(strike_field - float(occ.strike)) > 1e-6:
            log.warning("levelone_options_field_map_mismatch", symbol=symbol,
                        field_strike=strike_field, symbol_strike=float(occ.strike))
            return None
        mult = _f(raw.get(F_MULTIPLIER)) or 100.0
        mark = _f(raw.get(F_MARK)) or (bid + ask) / 2
        iv = _f(raw.get(F_IV))
        if iv is not None and iv > 5.0:
            iv = iv / 100.0        # percentage points -> decimal (see chain_service)
        contract = Contract(underlying=occ.underlying, expiry=occ.expiry,
                            right=occ.right, strike=occ.strike, multiplier=mult)
        return ContractQuote(
            contract=contract, ts=ts, mid=mark, bid=bid, ask=ask,
            underlying_px=_f(raw.get(F_UNDERLYING_PX)) or 0.0, iv=iv or 0.0,
            greeks=Greeks(delta=_f(raw.get(F_DELTA)) or 0.0,
                          gamma=_f(raw.get(F_GAMMA)) or 0.0,
                          theta=_f(raw.get(F_THETA)) or 0.0,
                          vega=_f(raw.get(F_VEGA)) or 0.0,
                          rho=_f(raw.get(F_RHO)) or 0.0),
            volume=_i(raw.get(F_VOLUME)), open_interest=_i(raw.get(F_OI)),
            source="live")


def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _i(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
