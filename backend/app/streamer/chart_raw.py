"""Raw CHART_EQUITY capture — a diagnostic twin of AcctActivityRecorder (see
that module's docstring for the general rationale).

bar_aggregator.py's and recorder.py's _on_chart handlers both independently
found (2026-07-23, 2026-07-24) that the field Schwab documents as "1"/Open
Price sometimes carries a value wildly inconsistent with high/low/close —
e.g. a value identical across unrelated symbols in the same push that
increments by exactly 1 each minute, looking like a counter rather than a
price. Both handlers correctly reject those bars via an OHLC sanity guard
rather than pass corrupted data to a live strategy — but neither logs the
message as delivered on the wire, only the already-parsed (and already
possibly mis-mapped) numbers. This module stores every raw CHART_EQUITY
content item verbatim, before any field-name mapping is applied, so the
real wire shape can be inspected empirically instead of inferred from
parsed log output. See chart_equity_raw in app/schema.sql.

Deliberately NOT gated behind live_probe_enabled: like AcctActivityRecorder,
this is read-only capture of data the app already receives (CHART_EQUITY is
subscribed regardless, for the CandleRecorder/BarAggregator) — it cannot
place or affect any order."""

from __future__ import annotations

import json
import time

from ..db import Database
from ..logging import get_logger
from .client import StreamerClient

log = get_logger("chart-raw")


class ChartRawRecorder:
    def __init__(self, db: Database, streamer: StreamerClient):
        self._db = db
        self.messages_captured = 0
        streamer.on_data("CHART_EQUITY", self._on_chart)

    async def _on_chart(self, content: list[dict]) -> None:
        now = int(time.time() * 1000)
        for item in content:
            symbol = item.get("key")
            await self._db.execute(
                "INSERT INTO chart_equity_raw (received_at, symbol, raw_json) VALUES (?,?,?)",
                (now, symbol, json.dumps(item)))
            self.messages_captured += 1

    async def recent(self, limit: int = 100) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT id, received_at, symbol, raw_json FROM chart_equity_raw "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
