"""Atomic persistence for Schwab OAuth tokens.

Every refresh ROTATES the refresh token (Schwab invalidates the old one), so a
lost write means a forced browser re-login. Writes are therefore atomic
(write-temp-then-rename) and the file is chmod 0600. Tokens never enter the DB
or logs."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ACCESS_TOKEN_LIFETIME_S = 30 * 60
REFRESH_TOKEN_LIFETIME_S = 7 * 24 * 3600


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    access_token_obtained_at: float  # unix seconds
    refresh_token_obtained_at: float

    @property
    def access_expires_at(self) -> float:
        return self.access_token_obtained_at + ACCESS_TOKEN_LIFETIME_S

    @property
    def refresh_expires_at(self) -> float:
        return self.refresh_token_obtained_at + REFRESH_TOKEN_LIFETIME_S

    def access_expires_in(self, now: float | None = None) -> float:
        return self.access_expires_at - (now or time.time())

    def refresh_expires_in(self, now: float | None = None) -> float:
        return self.refresh_expires_at - (now or time.time())

    @property
    def access_valid(self) -> bool:
        return self.access_expires_in() > 60  # 1-min safety margin

    @property
    def refresh_valid(self) -> bool:
        return self.refresh_expires_in() > 0


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> TokenSet | None:
        try:
            data = json.loads(self.path.read_text())
            return TokenSet(**data)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, TypeError, KeyError):
            # Corrupt file: preserve it for inspection, treat as absent.
            self.path.rename(self.path.with_suffix(".corrupt"))
            return None

    def save(self, tokens: TokenSet) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(tokens), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.path)  # atomic on POSIX
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
