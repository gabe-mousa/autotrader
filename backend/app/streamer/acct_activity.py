"""ACCT_ACTIVITY raw capture (Phase 5.5 — see app/broker/schwab.py's module
docstring for the full "why"). Schwab documents the envelope for this
streaming service but NOT the message-type-specific JSON shapes of "message
data", and publishes no enum of message types anywhere in the captured docs
(schwab/market-data-production/README.md §ACCT_ACTIVITY). Rather than guess
at an undocumented wire format that describes real order/fill state, this
module captures every raw message verbatim so a parser can later be built
from observed ground truth (docs/plan/11-roadmap.md Phase 5.5's purpose).

EMPIRICALLY OBSERVED (247 messages captured 2026-07-23 .. 2026-07-29; three
real orders on 2026-07-24 — two filled, one canceled). Everything below is
from real captured rows, not from docs:

Envelope — exactly five keys, no more:
    {"seq": <int>, "key": "Account Activity",
     "1": "<account number>",       # str
     "2": "<message type>",          # str, "" on non-order notices
     "3": "<message data>"}          # str — a JSON *string*, NOT an object.
                                     # Must be json.loads()'d again. On some
                                     # notices it is plain text, e.g.
                                     # "Feature not supported" (seen 3x with
                                     # an EMPTY "2"), so a parser must never
                                     # assume "3" is valid JSON.

Message types observed (count): SUBSCRIBED 227, "" 3, OrderCreated 3,
OrderAccepted 3, OrderFillCompleted 2, ExecutionRequested 2,
ExecutionRequestCreated 2, ExecutionRequestCompleted 2, ExecutionCreated 1,
CancelAccepted 1, OrderUROutCompleted 1.

Order-event message data is shaped
    {"SchwabOrderID": str, "AccountNumber": str,
     "BaseEvent": {"EventType": <type>, "<Type>Event<Suffix>": {...}}}
where the inner key name VARIES per type and is not derivable from the type
alone (OrderAccepted -> "OrderAcceptedEvent", OrderCreated ->
"OrderCreatedEventEquityOrder", OrderFillCompleted ->
"OrderFillCompletedEventOrderLegQuantityInfo", ExecutionRequested ->
"ExecutionRequestedEventRoutedInfo", ...). A parser should read
BaseEvent["EventType"] and then take the single other key, not guess names.

Correlation key — CONFIRMED against our own `orders` table: message data's
top-level `SchwabOrderID` is byte-identical to `orders.schwab_order_id`,
i.e. the id `SchwabClient.place_order` returns and `SchwabBroker._working`
is keyed by. No id translation is needed.

Numbers are a packed decimal, never a JSON number:
    {"lo": "<unscaled digits>", "signScale": <int>}   # "lo" absent => 0
    value = int(lo) / 10**(signScale >> 1),  negative iff signScale & 1
Confirmed by 1 share -> {"lo":"1000000","signScale":12} (scale 6) and a Buy's
EstimatedPrincipalAmount carrying signScale 13 (negative, cash out) where the
matching Sell's carried 12 (positive, cash in). A `hi` word is documented
nowhere and was never observed; a parser must refuse values carrying one.

Dedup: reconnects can make Schwab replay recent messages. `seq` IS a
top-level envelope field (confirmed — the numbered "0" slot does not exist),
but it is NOT globally monotonic: it RESTARTS at 0 on every new
subscription. Proof in the captured rows — the identical SUBSCRIBED payload
appears 10x at seq 0, 10x at seq 1, 10x at seq 3. So the original
"skip anything with seq <= high-water mark" rule silently DISCARDED every
message after a reconnect until the counter climbed back past the old mark,
which is exactly how a fill message gets lost. Dedup is therefore by exact
(seq, payload) identity over a bounded recent window instead: a genuine
replay is byte-identical and gets dropped, while a genuinely new low-seq
message after a reconnect is always kept. Worst case is a duplicate row in
an inspection-only table; the alternative was losing real fills."""

from __future__ import annotations

import json
import time
from collections import deque

from ..db import Database
from ..logging import get_logger
from .client import StreamerClient

log = get_logger("acct-activity")

ACCT_ACTIVITY_KEY = "Account Activity"

# How many recently-seen (seq, payload) identities to remember for replay
# dedup. Schwab replays only the tail of a session on reconnect, so this only
# has to outlive one reconnect burst — far smaller than a session's traffic,
# which is what keeps a legitimately-repeated message days later recordable.
DEDUP_WINDOW = 512


class AcctActivityRecorder:
    def __init__(self, db: Database, streamer: StreamerClient):
        self._db = db
        self._streamer = streamer
        self._recent: deque[tuple[int | None, str]] = deque(maxlen=DEDUP_WINDOW)
        self._recent_set: set[tuple[int | None, str]] = set()
        self.messages_captured = 0
        # Wall-clock ms of the last message of ANY kind. The capture went
        # completely silent from 2026-07-29 03:23Z onward while a live run
        # placed and filled four real orders, so "is this feed even alive?"
        # is a question worth being able to answer without a DB query.
        self.last_received_at: int | None = None
        streamer.on_data("ACCT_ACTIVITY", self._on_activity)

    async def start(self) -> None:
        """Subscribe once. Idempotent via the streamer's own ref-counted
        SubscriptionManager (same pattern CandleRecorder/BarAggregator use)."""
        await self._streamer.subscribe("ACCT_ACTIVITY", [ACCT_ACTIVITY_KEY])

    async def _on_activity(self, content: list[dict]) -> None:
        for item in content:
            seq = _as_int(item.get("seq") if "seq" in item else item.get("0"))
            raw = json.dumps(item, sort_keys=True)
            key = (seq, raw)
            if key in self._recent_set:
                continue  # byte-identical replay after a reconnect
            if len(self._recent) == self._recent.maxlen:
                self._recent_set.discard(self._recent[0])
            self._recent.append(key)
            self._recent_set.add(key)
            now_ms = int(time.time() * 1000)
            await self._db.execute(
                "INSERT INTO broker_activity_raw (received_at, seq, raw_json) VALUES (?,?,?)",
                (now_ms, seq, json.dumps(item)))
            self.messages_captured += 1
            self.last_received_at = now_ms
            log.info("acct_activity_captured", seq=seq, raw=item)

    async def recent(self, limit: int = 100) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT id, received_at, seq, raw_json FROM broker_activity_raw "
            "ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def _as_int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
