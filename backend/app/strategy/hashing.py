"""Content hashing: canonical JSON (sorted keys, normalized numbers) of the
document EXCLUDING meta -> SHA-256. Cosmetic edits (name, description, tags)
keep the hash; any lever change produces a new one. The hash is the permanent
identity of a strategy *version* (promotion gate, run traceability)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .schema import StrategyDocument


def _normalize(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _normalize(v) for k, v in sorted(x.items()) if v is not None}
    if isinstance(x, list):
        return [_normalize(v) for v in x]
    if isinstance(x, float):
        if x == int(x) and abs(x) < 1e15:  # 2.0 == 2 — same lever value
            return int(x)
        return round(x, 10)
    return x


def content_hash(doc: StrategyDocument) -> str:
    payload = doc.model_dump(mode="json", by_alias=True, exclude={"meta"})
    canonical = json.dumps(_normalize(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def short_hash(doc: StrategyDocument) -> str:
    return content_hash(doc)[:8]
