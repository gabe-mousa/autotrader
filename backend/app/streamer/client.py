"""Schwab Streamer WebSocket client.

Hard constraints (see docs/plan/04-market-data.md and schwab market-data docs):
- ONE simultaneous connection per user. This class is the sole owner.
- LOGIN must be the first command and must succeed before SUBS/ADD.
- SUBS overwrites a service's whole symbol set; ADD appends; UNSUBS removes.
  Consumers therefore never send commands — they register interest through the
  ref-counted SubscriptionManager which issues minimal diffs (and a full SUBS
  after every reconnect).
- Reconnect: exponential backoff 1s→60s with jitter; heartbeat gap >30s forces
  a reconnect; access-token rotation triggers a proactive re-LOGIN (reconnect).
"""

from __future__ import annotations

import asyncio
import json
import random
import ssl
import time
from collections import defaultdict
from typing import Awaitable, Callable

import certifi
import websockets

# Framework/python.org macOS builds ship without a wired-up system CA store;
# use certifi's bundle explicitly (httpx does the same internally).
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

from ..auth.manager import AuthManager, NotAuthenticatedError
from ..logging import get_logger

log = get_logger("streamer")

HEARTBEAT_TIMEOUT_S = 30
BACKOFF_MIN_S, BACKOFF_MAX_S = 1.0, 60.0

# handler receives the list of content dicts from a data message
DataHandler = Callable[[list[dict]], Awaitable[None]]


class SubscriptionManager:
    """Ref-counts (service, symbol) interest; computes ADD/UNSUBS diffs."""

    def __init__(self):
        self._refs: dict[str, dict[str, int]] = defaultdict(dict)  # service -> {symbol: n}

    def acquire(self, service: str, symbols: list[str]) -> list[str]:
        """Returns symbols newly needed on the wire (refcount 0 -> 1)."""
        new = []
        for s in symbols:
            n = self._refs[service].get(s, 0)
            self._refs[service][s] = n + 1
            if n == 0:
                new.append(s)
        return new

    def release(self, service: str, symbols: list[str]) -> list[str]:
        """Returns symbols no longer needed (refcount -> 0)."""
        gone = []
        for s in symbols:
            n = self._refs[service].get(s, 0)
            if n <= 1:
                self._refs[service].pop(s, None)
                if n == 1:
                    gone.append(s)
            else:
                self._refs[service][s] = n - 1
        return gone

    def active(self, service: str) -> list[str]:
        return sorted(self._refs[service].keys())

    def services(self) -> list[str]:
        return [svc for svc, syms in self._refs.items() if syms]


# fields we request per service (see captured streamer field tables)
SERVICE_FIELDS = {
    "LEVELONE_EQUITIES": "0,1,2,3,4,5,8,33,34,35",  # sym,bid,ask,last,bidSz,askSz,vol,mark,qTime,tTime
    "CHART_EQUITY": "0,1,2,3,4,5,6,7,8",
    "ACCT_ACTIVITY": "0,1,2,3",
    # sym,bid,ask,last,vol,OI,IV,mult,strike,type,underlying,DTE,greeks,
    # underlyingPx,mark,quoteTime. A service MISSING from this map subscribes
    # with fields="0" — the symbol and nothing else — so the feed arrives
    # technically working and completely empty. That is what LEVELONE_OPTIONS
    # did until this entry existed: option_recorder.py defined the field list
    # but nothing ever passed it to the wire.
    "LEVELONE_OPTIONS": "0,2,3,4,8,9,10,13,20,21,22,27,28,29,30,31,32,35,37,38",
}


class StreamerClient:
    def __init__(self, auth: AuthManager, schwab):
        self._auth = auth
        self._schwab = schwab
        self.subs = SubscriptionManager()
        self._handlers: dict[str, list[DataHandler]] = defaultdict(list)
        self._ws: websockets.ClientProtocol | None = None
        self._task: asyncio.Task | None = None
        self._req_id = 0
        self._logged_in = asyncio.Event()
        self._last_msg_at = 0.0
        self._want_reconnect = asyncio.Event()
        self.state = "disconnected"  # disconnected | connecting | connected | degraded
        self._streamer_info: dict | None = None
        auth.on_refresh(lambda: self._want_reconnect.set())  # re-LOGIN on token rotation

    # ---- public API -----------------------------------------------------
    def on_data(self, service: str, handler: DataHandler) -> None:
        self._handlers[service].append(handler)

    async def subscribe(self, service: str, symbols: list[str]) -> None:
        new = self.subs.acquire(service, symbols)
        if new and self._logged_in.is_set():
            await self._send_cmd(service, "ADD", keys=new)

    async def unsubscribe(self, service: str, symbols: list[str]) -> None:
        gone = self.subs.release(service, symbols)
        if gone and self._logged_in.is_set():
            await self._send_cmd(service, "UNSUBS", keys=gone)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="streamer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.state = "disconnected"

    # ---- connection loop ------------------------------------------------
    async def _run(self) -> None:
        backoff = BACKOFF_MIN_S
        while True:
            try:
                self.state = "connecting"
                await self._connect_once()
                backoff = BACKOFF_MIN_S  # successful session resets backoff
            except NotAuthenticatedError:
                self.state = "disconnected"
                await asyncio.sleep(30)  # wait for interactive auth
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("streamer_session_ended", error=str(e))
            self.state = "degraded"
            delay = backoff + random.uniform(0, backoff / 2)
            log.info("streamer_reconnect_wait", seconds=round(delay, 1))
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _connect_once(self) -> None:
        self._logged_in.clear()
        self._want_reconnect.clear()
        prefs = await self._schwab.get_user_preference()
        info = (prefs.get("streamerInfo") or [{}])[0]
        self._streamer_info = info
        url = info.get("streamerSocketUrl")
        if not url:
            raise RuntimeError("no streamerSocketUrl in userPreference")
        token = await self._auth.get_access_token()

        async with websockets.connect(url, ssl=_SSL_CTX, max_size=4 * 1024 * 1024) as ws:
            self._ws = ws
            await self._send_raw(
                {
                    "requests": [
                        {
                            "requestid": self._next_id(),
                            "service": "ADMIN",
                            "command": "LOGIN",
                            "SchwabClientCustomerId": info.get("schwabClientCustomerId"),
                            "SchwabClientCorrelId": info.get("schwabClientCorrelId"),
                            "parameters": {
                                "Authorization": token,
                                "SchwabClientChannel": info.get("schwabClientChannel"),
                                "SchwabClientFunctionId": info.get("schwabClientFunctionId"),
                            },
                        }
                    ]
                }
            )
            self._last_msg_at = time.time()
            watchdog = asyncio.create_task(self._watchdog(), name="streamer-watchdog")
            try:
                async for raw in ws:
                    self._last_msg_at = time.time()
                    await self._dispatch(json.loads(raw))
                    if self._want_reconnect.is_set():
                        log.info("streamer_reconnecting_for_token_rotation")
                        break
            finally:
                watchdog.cancel()
                self._ws = None
                self._logged_in.clear()

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            gap = time.time() - self._last_msg_at
            if gap > HEARTBEAT_TIMEOUT_S:
                log.warning("streamer_heartbeat_gap", seconds=round(gap))
                if self._ws:
                    await self._ws.close(code=4000, reason="heartbeat gap")
                return

    # ---- message handling ----------------------------------------------
    async def _dispatch(self, msg: dict) -> None:
        for resp in msg.get("response", []):
            await self._handle_response(resp)
        for data in msg.get("data", []):
            service = data.get("service", "")
            content = data.get("content", [])
            for handler in self._handlers.get(service, []):
                try:
                    await handler(content)
                except Exception as e:  # noqa: BLE001 — a bad handler never kills the stream
                    log.error("data_handler_error", service=service, error=str(e))
        # notify (heartbeat) needs no action beyond _last_msg_at update

    async def _handle_response(self, resp: dict) -> None:
        service, command = resp.get("service"), resp.get("command")
        code = (resp.get("content") or {}).get("code")
        if service == "ADMIN" and command == "LOGIN":
            if code == 0:
                log.info("streamer_login_ok")
                self._logged_in.set()
                self.state = "connected"
                await self._resubscribe_all()
            else:
                msg = (resp.get("content") or {}).get("msg", "")
                raise RuntimeError(f"streamer LOGIN denied code={code} {msg}")
        elif code not in (0, None, 26, 27, 28, 29):  # SUCCEEDED_* codes are fine
            log.warning("streamer_command_failed", service=service, command=command, code=code,
                        msg=(resp.get("content") or {}).get("msg"))

    async def _resubscribe_all(self) -> None:
        """Full SUBS per service after (re)connect — SUBS overwrites, which is
        exactly what we want here."""
        for service in self.subs.services():
            await self._send_cmd(service, "SUBS", keys=self.subs.active(service))

    # ---- wire helpers ---------------------------------------------------
    async def _send_cmd(self, service: str, command: str, keys: list[str]) -> None:
        info = self._streamer_info or {}
        params: dict = {"keys": ",".join(keys)}
        if command in ("SUBS", "ADD"):
            params["fields"] = SERVICE_FIELDS.get(service, "0")
        await self._send_raw(
            {
                "requests": [
                    {
                        "requestid": self._next_id(),
                        "service": service,
                        "command": command,
                        "SchwabClientCustomerId": info.get("schwabClientCustomerId"),
                        "SchwabClientCorrelId": info.get("schwabClientCorrelId"),
                        "parameters": params,
                    }
                ]
            }
        )

    async def _send_raw(self, payload: dict) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps(payload))

    def _next_id(self) -> str:
        self._req_id += 1
        return str(self._req_id)
