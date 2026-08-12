"""Schwab OAuth 2.0 three-legged flow + token lifecycle.

Flow (per schwab/README.md):
  1. Browser -> https://api.schwabapi.com/v1/oauth/authorize?client_id=..&redirect_uri=..
  2. Schwab redirects to our HTTPS callback with ?code=... (URL-decoded, ends '@')
  3. POST /v1/oauth/token (Basic client_id:client_secret) grant_type=authorization_code
  4. Refresh with grant_type=refresh_token every ~25 min; each refresh rotates
     the refresh token. Refresh token hard-expires after 7 days -> full re-auth.
"""

from __future__ import annotations

import asyncio
import base64
import time
import urllib.parse
from typing import Callable

import httpx

from ..config import Settings
from ..logging import get_logger
from .token_store import TokenSet, TokenStore

AUTHORIZE_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
REFRESH_INTERVAL_S = 25 * 60  # renew well before the 30-min expiry
RETRY_BACKOFF_S = (5, 15, 60, 120, 300)

log = get_logger("auth")


class AuthError(Exception):
    pass


class NotAuthenticatedError(AuthError):
    """No valid token set — interactive re-auth required."""


class AuthManager:
    def __init__(self, settings: Settings, store: TokenStore | None = None):
        self._s = settings
        self._store = store or TokenStore(settings.tokens_path)
        self._tokens: TokenSet | None = self._store.load()
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None
        self._last_refresh_at: float | None = None
        self._last_refresh_error: str | None = None
        # subscribers notified on token change (e.g. streamer re-LOGIN)
        self._on_refresh: list[Callable[[], None]] = []

    # ---- flow step 1: authorize URL ------------------------------------
    def authorize_url(self) -> str:
        if not self._s.schwab_configured:
            raise AuthError("Schwab client id/secret not configured in .env")
        q = urllib.parse.urlencode(
            {"client_id": self._s.schwab_client_id, "redirect_uri": self._s.schwab_callback_url}
        )
        return f"{AUTHORIZE_URL}?{q}"

    # ---- flow steps 2-3: exchange code ---------------------------------
    async def exchange_code(self, code: str) -> None:
        code = urllib.parse.unquote(code)  # portal docs: must end '@' not '%40'
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._s.schwab_callback_url,
        }
        tokens = await self._token_request(data)
        # brand-new refresh token -> reset the 7-day clock
        now = time.time()
        self._set_tokens(
            TokenSet(
                access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                access_token_obtained_at=now,
                refresh_token_obtained_at=now,
            )
        )
        log.info("oauth_exchange_complete")

    # ---- flow step 4: refresh ------------------------------------------
    async def refresh(self) -> None:
        async with self._lock:
            if self._tokens is None or not self._tokens.refresh_valid:
                raise NotAuthenticatedError("refresh token missing or expired")
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self._tokens.refresh_token,
            }
            tokens = await self._token_request(data)
            now = time.time()
            new_refresh = tokens.get("refresh_token") or self._tokens.refresh_token
            rotated = new_refresh != self._tokens.refresh_token
            self._set_tokens(
                TokenSet(
                    access_token=tokens["access_token"],
                    refresh_token=new_refresh,
                    access_token_obtained_at=now,
                    # only reset the 7-day clock if Schwab actually rotated it
                    refresh_token_obtained_at=now
                    if rotated
                    else self._tokens.refresh_token_obtained_at,
                )
            )
            self._last_refresh_at = now
            self._last_refresh_error = None
            log.info("token_refreshed", rotated_refresh=rotated)

    async def _token_request(self, data: dict) -> dict:
        basic = base64.b64encode(
            f"{self._s.schwab_client_id}:{self._s.schwab_client_secret}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                TOKEN_URL,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=data,
            )
        if resp.status_code != 200:
            raise AuthError(f"token endpoint {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ---- access for API callers ----------------------------------------
    async def get_access_token(self) -> str:
        """Valid access token, refreshing if needed. Raises NotAuthenticatedError."""
        t = self._tokens
        if t is None:
            raise NotAuthenticatedError("not connected to Schwab")
        if not t.access_valid:
            await self.refresh()
            t = self._tokens
            assert t is not None
        return t.access_token

    # ---- background refresh loop ---------------------------------------
    def start_background_refresh(self) -> None:
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop(), name="auth-refresh")

    async def stop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass

    async def _refresh_loop(self) -> None:
        while True:
            t = self._tokens
            if t is None or not t.refresh_valid:
                await asyncio.sleep(30)  # idle until interactive auth happens
                continue
            # sleep until ~5 min before access expiry
            delay = max(30.0, t.access_expires_in() - 300)
            await asyncio.sleep(min(delay, REFRESH_INTERVAL_S))
            for attempt, backoff in enumerate((0,) + RETRY_BACKOFF_S):
                if backoff:
                    await asyncio.sleep(backoff)
                try:
                    await self.refresh()
                    for cb in self._on_refresh:
                        cb()
                    break
                except NotAuthenticatedError:
                    log.error("refresh_token_expired_needs_reauth")
                    break
                except Exception as e:  # network/5xx — retry with backoff
                    self._last_refresh_error = str(e)
                    log.warning("token_refresh_failed", attempt=attempt, error=str(e))

    def on_refresh(self, cb: Callable[[], None]) -> None:
        self._on_refresh.append(cb)

    # ---- status for the UI ---------------------------------------------
    def status(self) -> dict:
        t = self._tokens
        connected = bool(t and t.refresh_valid)
        return {
            "connected": connected,
            "client_configured": self._s.schwab_configured,
            "access_token_expires_in": int(t.access_expires_in()) if connected and t else None,
            "refresh_token_expires_in": int(t.refresh_expires_in()) if connected and t else None,
            "refresh_token_expires_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t.refresh_expires_at))
                if connected and t
                else None
            ),
            "last_refresh_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._last_refresh_at))
                if self._last_refresh_at
                else None
            ),
            "last_refresh_error": self._last_refresh_error,
        }

    def _set_tokens(self, tokens: TokenSet) -> None:
        self._tokens = tokens
        self._store.save(tokens)
