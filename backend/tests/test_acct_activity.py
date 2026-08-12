"""AcctActivityRecorder tests: raw capture persistence, replay dedup, and
graceful no-crash on messages with no seq at all.

The fixtures below are REAL Schwab ACCT_ACTIVITY payloads lifted verbatim
from the captured `broker_activity_raw` rows for a live 1-share WEN order on
2026-07-24, with the account number redacted. They are here so the capture
path is proven against the actual wire format rather than an invented one —
see `app/streamer/acct_activity.py`'s docstring for the format itself."""

from __future__ import annotations

import json

import pytest

from app.db import Database
from app.streamer.acct_activity import ACCT_ACTIVITY_KEY, DEDUP_WINDOW, AcctActivityRecorder

# Note "3" is a JSON *string*, not a nested object — that is how Schwab really
# sends it, and any future parser has to json.loads() it a second time.
REAL_FILL_DATA = (
    '{"SchwabOrderID":"1007327483126","AccountNumber":"REDACTED","BaseEvent":{"EventType":'
    '"OrderFillCompleted","OrderFillCompletedEventOrderLegQuantityInfo":{"EventType":'
    '"OrderFillCompleted","LegId":"1007327483126","LegStatus":"LegClosed","QuantityInfo":'
    '{"ExecutionID":"20260724-EST-ngOMS-18239777630","CumulativeQuantity":{"lo":"1000000",'
    '"signScale":12},"LeavesQuantity":{"signScale":12},"AveragePrice":{"lo":"7201500",'
    '"signScale":12}},"PriceImprovement":{"lo":"8500","signScale":12},"LegSubStatus":'
    '"LegSubStatusFilled","ExecutionInfo":{"ExecutionSequenceNumber":1,"ExecutionId":'
    '"20260724-EST-ngOMS-18239777630","ExecutionQuantity":{"lo":"1000000","signScale":12},'
    '"ExecutionPrice":{"lo":"7201500","signScale":12},"ExecutionTimeStamp":{"DateTimeString":'
    '"2026-07-24 12:34:44.529"},"ExecutionTransType":"Fill","RouteName":"CES_NMS_F1_J1"},'
    '"OrderInfoForTransactionPosting":{"LimitPrice":{},"OrderTypeCode":"Market","BuySellCode":'
    '"Buy","Quantity":{"lo":"1000000","signScale":12},"StopPrice":{},"Symbol":"WEN"}}}}'
)
REAL_FILL = {"seq": 9, "key": ACCT_ACTIVITY_KEY, "1": "REDACTED",
             "2": "OrderFillCompleted", "3": REAL_FILL_DATA}

# A real SUBSCRIBED ack. Sent on every (re)subscription with seq restarting
# from 0 — which is why dedup can't use a seq high-water mark.
REAL_SUBSCRIBED = {"seq": 0, "key": ACCT_ACTIVITY_KEY, "1": "", "2": "SUBSCRIBED", "3": ""}

# A real non-order notice: message type is EMPTY and "3" is plain text, not
# JSON. Proof that a parser must never assume "3" parses.
REAL_NOTICE = {"seq": 1, "key": ACCT_ACTIVITY_KEY, "1": "REDACTED", "2": "",
               "3": "Feature not supported"}


class FakeStreamer:
    def __init__(self):
        self.handler = None
        self.subscribed: list[tuple[str, list[str]]] = []

    def on_data(self, service, handler):
        self.handler = handler

    async def subscribe(self, service, symbols):
        self.subscribed.append((service, list(symbols)))

    async def push(self, content):
        await self.handler(content)


@pytest.fixture
async def rig(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    streamer = FakeStreamer()
    rec = AcctActivityRecorder(db, streamer)
    yield db, streamer, rec
    await db.close()


async def test_start_subscribes_the_fixed_key(rig):
    db, streamer, rec = rig
    await rec.start()
    assert streamer.subscribed == [("ACCT_ACTIVITY", [ACCT_ACTIVITY_KEY])]


async def test_captures_raw_message_verbatim(rig):
    db, streamer, rec = rig
    msg = {"seq": 1, "key": ACCT_ACTIVITY_KEY, "1": "12345", "2": "OrderFill",
          "3": '{"orderId": 999}'}
    await streamer.push([msg])

    assert rec.messages_captured == 1
    rows = await rec.recent()
    assert len(rows) == 1
    assert rows[0]["seq"] == 1
    assert json.loads(rows[0]["raw_json"]) == msg


async def test_dedupes_replayed_messages_by_seq(rig):
    db, streamer, rec = rig
    await streamer.push([{"seq": 1, "1": "a"}])
    await streamer.push([{"seq": 2, "1": "b"}])
    # reconnect replay: seq 1 and 2 come back again, plus a genuinely new seq 3
    await streamer.push([{"seq": 1, "1": "a"}, {"seq": 2, "1": "b"}, {"seq": 3, "1": "c"}])

    assert rec.messages_captured == 3
    rows = await rec.recent()
    assert sorted(r["seq"] for r in rows) == [1, 2, 3]


async def test_messages_without_seq_are_still_captured(rig):
    db, streamer, rec = rig
    await streamer.push([{"1": "no sequence field here"}])
    assert rec.messages_captured == 1
    rows = await rec.recent()
    assert rows[0]["seq"] is None


# ---- real captured payloads -------------------------------------------


async def test_captures_a_real_order_fill_verbatim(rig):
    """The one message a future fast path would care about most, taken from a
    real live fill, survives the capture path byte-for-byte."""
    db, streamer, rec = rig
    await streamer.push([REAL_FILL])

    rows = await rec.recent()
    assert len(rows) == 1
    assert rows[0]["seq"] == 9
    stored = json.loads(rows[0]["raw_json"])
    assert stored == REAL_FILL
    # "3" must come back out as the JSON *string* Schwab sent, unmodified.
    assert isinstance(stored["3"], str)
    data = json.loads(stored["3"])
    # Correlation key that makes a fast path possible at all: this is exactly
    # the id place_order returns / SchwabBroker._working is keyed by.
    assert data["SchwabOrderID"] == "1007327483126"
    event = data["BaseEvent"]["OrderFillCompletedEventOrderLegQuantityInfo"]
    assert event["LegStatus"] == "LegClosed"
    assert event["LegSubStatus"] == "LegSubStatusFilled"
    # Packed decimal: value = lo / 10**(signScale >> 1). 1 share @ 7.2015.
    qty = event["QuantityInfo"]["CumulativeQuantity"]
    px = event["QuantityInfo"]["AveragePrice"]
    assert int(qty["lo"]) / 10 ** (qty["signScale"] >> 1) == 1.0
    assert int(px["lo"]) / 10 ** (px["signScale"] >> 1) == 7.2015


async def test_captures_notice_whose_message_data_is_not_json(rig):
    """Real messages arrive with an EMPTY type and plain text in "3"; capture
    must not care, and must not corrupt them."""
    db, streamer, rec = rig
    await streamer.push([REAL_NOTICE])

    rows = await rec.recent()
    assert json.loads(rows[0]["raw_json"]) == REAL_NOTICE
    assert json.loads(rows[0]["raw_json"])["3"] == "Feature not supported"


async def test_reconnect_seq_reset_does_not_drop_a_new_fill(rig):
    """Regression: `seq` restarts at 0 on every resubscribe, so the old
    "skip seq <= high-water mark" rule silently discarded real fills after a
    reconnect. A low-seq NEW message must always be captured."""
    db, streamer, rec = rig
    # a busy session pushes the sequence counter well past the fill's seq 9
    await streamer.push([{"seq": n, "key": ACCT_ACTIVITY_KEY, "2": "SUBSCRIBED"}
                        for n in range(40)])
    assert rec.messages_captured == 40

    # reconnect: seq restarts, then a genuine fill arrives at seq 9
    await streamer.push([REAL_SUBSCRIBED, REAL_FILL])

    assert rec.messages_captured == 42
    types = [json.loads(r["raw_json"]).get("2") for r in await rec.recent()]
    assert "OrderFillCompleted" in types


async def test_identical_replay_is_deduped_but_a_later_repeat_is_kept(rig):
    """Byte-identical replay inside the window is a reconnect artifact and is
    dropped; the same payload again long after (a fresh subscribe days later)
    is real traffic and must still be recorded."""
    db, streamer, rec = rig
    await streamer.push([REAL_SUBSCRIBED])
    await streamer.push([REAL_SUBSCRIBED])  # immediate replay -> dropped
    assert rec.messages_captured == 1

    # push enough unrelated traffic to age it out of the dedup window
    await streamer.push([{"seq": 1000 + n, "2": "SUBSCRIBED"} for n in range(DEDUP_WINDOW)])
    await streamer.push([REAL_SUBSCRIBED])
    assert rec.messages_captured == DEDUP_WINDOW + 2


async def test_last_received_at_tracks_liveness(rig):
    db, streamer, rec = rig
    assert rec.last_received_at is None
    await streamer.push([REAL_FILL])
    assert isinstance(rec.last_received_at, int)
