"""ChartRawRecorder tests: raw CHART_EQUITY capture persistence, verbatim
before any field-name mapping is applied (see app/streamer/chart_raw.py's
module docstring)."""

from __future__ import annotations

import json

import pytest

from app.db import Database
from app.streamer.chart_raw import ChartRawRecorder


class FakeStreamer:
    def __init__(self):
        self.handler = None

    def on_data(self, service, handler):
        self.handler = handler

    async def push(self, content):
        await self.handler(content)


@pytest.fixture
async def rig(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    streamer = FakeStreamer()
    rec = ChartRawRecorder(db, streamer)
    yield db, streamer, rec
    await db.close()


async def test_captures_raw_message_verbatim(rig):
    db, streamer, rec = rig
    msg = {"key": "WEN", "1": 486.0, "2": 7.025, "3": 7.025, "4": 7.02, "5": 100,
          "6": 12345, "7": 1753300800000, "8": 20260724}
    await streamer.push([msg])

    assert rec.messages_captured == 1
    rows = await rec.recent()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "WEN"
    assert json.loads(rows[0]["raw_json"]) == msg


async def test_captures_every_item_in_a_batch_even_across_symbols(rig):
    db, streamer, rec = rig
    await streamer.push([
        {"key": "SPY", "1": 486.0},
        {"key": "QQQ", "1": 486.0},
        {"key": "GOOG", "1": 486.0},
    ])
    assert rec.messages_captured == 3
    rows = await rec.recent()
    assert sorted(r["symbol"] for r in rows) == ["GOOG", "QQQ", "SPY"]


async def test_captures_items_with_no_key(rig):
    db, streamer, rec = rig
    await streamer.push([{"1": "no key field here"}])
    assert rec.messages_captured == 1
    rows = await rec.recent()
    assert rows[0]["symbol"] is None
