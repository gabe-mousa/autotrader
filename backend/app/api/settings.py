"""Settings API — lets a user configure secrets (Schwab credentials, the
Anthropic key) from the Settings page instead of hand-editing `.env`.

Writes land in the real `.env` file so the rest of the app (which reads
everything through `config.Settings`, never `os.environ` directly) sees no
difference between a value typed here and one set by hand. `Settings` is
loaded once at process start (`functools.lru_cache`), so a save here only
takes effect after the backend restarts — every response says so.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..logging import get_logger

router = APIRouter(prefix="/api/settings")
log = get_logger("api-settings")

# .env lives at the repo root — same layout config.py assumes (one level
# above backend/).
_ENV_PATH = Path(__file__).resolve().parent.parent.parent.parent / ".env"

# Keys this endpoint is allowed to touch, mapped to their .env names.
# Deliberately an explicit allowlist rather than an arbitrary key/value map:
# .env also holds unrelated app config (ports, safety limits) that has no
# business being editable from a "put your API keys in" form.
_EDITABLE: dict[str, str] = {
    "schwab_client_id": "SCHWAB_CLIENT_ID",
    "schwab_client_secret": "SCHWAB_CLIENT_SECRET",
    "schwab_callback_url": "SCHWAB_CALLBACK_URL",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
}
# Fields whose current value is never echoed back to the browser — only
# whether one is set. schwab_callback_url isn't a secret (it's a fixed local
# redirect URI) so it's shown in full to make it easy to confirm/edit.
_SECRET_FIELDS = {"schwab_client_id", "schwab_client_secret", "anthropic_api_key"}


def _read_env_lines() -> list[str]:
    if not _ENV_PATH.exists():
        return []
    return _ENV_PATH.read_text().splitlines()


def _current_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_env_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _upsert_env(updates: dict[str, str]) -> None:
    """Rewrite `.env` with the given ENV_KEY -> value pairs, preserving every
    other line (comments, unrelated settings) exactly as-is. Appends any key
    that doesn't already exist."""
    lines = _read_env_lines()
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    if remaining:
        if out and out[-1].strip() != "":
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(out) + "\n")


@router.get("/credentials")
async def get_credentials(request: Request) -> dict:
    """Current state of each editable credential. Secret fields report only
    whether they're set — never the value — so this endpoint is safe to call
    from the Settings page on every load."""
    settings = request.app.state.settings
    env_values = _current_env_values()
    fields = {}
    for field, env_key in _EDITABLE.items():
        current = env_values.get(env_key, getattr(settings, field, "") or "")
        if field in _SECRET_FIELDS:
            fields[field] = {"configured": bool(current)}
        else:
            fields[field] = {"configured": bool(current), "value": current}
    return {"fields": fields, "env_path": str(_ENV_PATH), "restart_required": True}


@router.put("/credentials")
async def put_credentials(request: Request, body: dict) -> dict:
    """Save one or more credentials to `.env`. A field omitted or sent as an
    empty string is left untouched — this form is never the only way to see
    a value, so there's no legitimate reason to silently blank one out."""
    updates: dict[str, str] = {}
    for field, env_key in _EDITABLE.items():
        if field not in body:
            continue
        value = body[field]
        if value is None:
            continue
        value = str(value).strip()
        if value == "":
            continue
        if "\n" in value or "\r" in value:
            raise HTTPException(400, f"{field} cannot contain newlines")
        updates[env_key] = value

    if not updates:
        raise HTTPException(400, "no fields to update")

    _upsert_env(updates)
    log.info("credentials_saved", fields=sorted(updates.keys()))
    return {
        "saved": sorted(updates.keys()),
        "restart_required": True,
        "message": "Saved to .env. Restart the backend for this to take effect.",
    }


# ---- Live trading toggle (LIVE_PROBE_ENABLED) --------------------------------
#
# Kept separate from /credentials rather than folded into that field map:
# credentials treat "" in the request body as "leave unchanged" (a form
# field left blank shouldn't silently wipe a saved secret), but a boolean
# toggle has no such ambiguous state — every PUT here is an explicit,
# complete on/off, so it gets its own small pair of routes instead of
# stretching the credentials convention to fit.

_TRUE_STRINGS = {"1", "true", "yes", "on"}


@router.get("/live-trading")
async def get_live_trading(request: Request) -> dict:
    settings = request.app.state.settings
    raw = _current_env_values().get("LIVE_PROBE_ENABLED")
    enabled = raw.strip().lower() in _TRUE_STRINGS if raw is not None else bool(
        settings.live_probe_enabled
    )
    return {"enabled": enabled, "restart_required": True}


@router.put("/live-trading")
async def put_live_trading(request: Request, body: dict) -> dict:
    if not isinstance(body.get("enabled"), bool):
        raise HTTPException(400, "body must include a boolean 'enabled'")
    enabled = body["enabled"]
    _upsert_env({"LIVE_PROBE_ENABLED": "true" if enabled else "false"})
    log.info("live_trading_toggled", enabled=enabled)
    return {
        "enabled": enabled,
        "restart_required": True,
        "message": "Saved to .env. Restart the backend for this to take effect.",
    }
