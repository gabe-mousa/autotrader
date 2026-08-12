"""Market calendar: equity session hours per date, fetched from Schwab
GET /markets/equity and cached in the DB. Past dates can't be queried from the
API, so past sessions are recorded as observed (from candle data) or assumed
Mon-Fri 9:30-16:00 ET minus cached knowledge."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from ..db import Database
from ..logging import get_logger

ET = ZoneInfo("America/New_York")
log = get_logger("calendar")


class MarketCalendar:
    def __init__(self, db: Database, schwab):
        self._db = db
        self._schwab = schwab

    async def session(self, date: dt.date) -> tuple[int, int] | None:
        """(session_start_ms, session_end_ms) UTC for the regular equity session,
        or None if the market is closed that date."""
        key = date.isoformat()
        row = await self._db.fetch_one(
            "SELECT is_open, session_start, session_end FROM market_calendar WHERE date=?",
            (key,),
        )
        if row:
            return (row["session_start"], row["session_end"]) if row["is_open"] else None

        today = dt.date.today()
        if date >= today:
            # future/today: authoritative from the API
            try:
                payload = await self._schwab.get_market_hours(key)
                is_open, start_ms, end_ms = _parse_hours(payload, date)
                await self._save(key, is_open, start_ms, end_ms)
                return (start_ms, end_ms) if is_open else None
            except Exception as e:  # noqa: BLE001 — fall through to heuristic
                log.warning("market_hours_fetch_failed", date=key, error=str(e))
        # past date (or fetch failure): weekday heuristic, standard hours.
        # Backfill jobs correct false-opens implicitly (no candles on holidays).
        if date.weekday() >= 5:
            await self._save(key, False, None, None)
            return None
        start = dt.datetime.combine(date, dt.time(9, 30), ET)
        end = dt.datetime.combine(date, dt.time(16, 0), ET)
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        # heuristic results for past dates are cached too (marked by normal row)
        await self._save(key, True, start_ms, end_ms)
        return (start_ms, end_ms)

    async def is_open_now(self) -> bool:
        now = dt.datetime.now(tz=dt.timezone.utc)
        sess = await self.session(now.astimezone(ET).date())
        if not sess:
            return False
        return sess[0] <= int(now.timestamp() * 1000) <= sess[1]

    async def _save(self, key: str, is_open: bool, start_ms: int | None, end_ms: int | None):
        await self._db.execute(
            "INSERT OR REPLACE INTO market_calendar (date, is_open, session_start, session_end) "
            "VALUES (?,?,?,?)",
            (key, int(is_open), start_ms, end_ms),
        )


def _parse_hours(payload: dict, date: dt.date) -> tuple[bool, int | None, int | None]:
    """Parse GET /markets/equity response -> (is_open, start_ms, end_ms).
    Shape: {"equity": {"EQ": {"isOpen": true, "sessionHours": {"regularMarket":
    [{"start": "...-04:00", "end": "..."}]}}}} (key names vary; be defensive)."""
    market = payload.get("equity") or {}
    node = next(iter(market.values()), {}) if market else {}
    if not node.get("isOpen"):
        return False, None, None
    hours = (node.get("sessionHours") or {}).get("regularMarket") or []
    if not hours:
        return False, None, None
    start = dt.datetime.fromisoformat(hours[0]["start"])
    end = dt.datetime.fromisoformat(hours[0]["end"])
    return True, int(start.timestamp() * 1000), int(end.timestamp() * 1000)
