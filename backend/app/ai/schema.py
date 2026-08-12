"""AI response envelope + the schema the model is shown.

DESIGN (rewritten 2026-07-26 — see LIVING-DOC): the model emits a plain JSON
object and we validate it ourselves, rather than using the API's constrained
decoding (`output_config.format.json_schema`).

Constrained decoding was the original approach and it forced two workarounds,
because the compiled grammar has a hard complexity ceiling this document blew
through ("compiled grammar is too large" 400):

  1. Recursive schemas are unsupported, and `strategy.schema.Expr` is
     recursive, so rule conditions had to be emitted as JSON-encoded STRINGS
     (`when_json`, `params_json`) and re-parsed here.
  2. The ceiling left no room for whole sections. `session` (entry window,
     flatten_eod) and `orders` were dropped from the model's view entirely —
     so the model told users those levers "are not part of the configurable
     strategy body" and substituted a bar-count time stop, even though
     backtest/engine.py and runtime/runner.py both honor them. Measured live,
     the budget afforded only ~3 extra leaf fields; there was no shape of the
     old design that fit every lever.

Without constrained decoding there is no grammar and therefore no ceiling: the
model is shown the REAL StrategyDocument schema and emits the REAL nested
document, every lever included. Validation is unchanged — StrategyDocument +
lint + the existing repair loop already re-validated everything server-side
regardless, so nothing that protected correctness was given up. Measured over
10 varied live generations (simple, deeply nested, 6-indicator, vague,
contradictory), 10/10 validated on the first attempt with no repair round.

The tradeoff accepted: shape errors are now caught after generation instead of
being impossible during it. That is what MAX_REPAIR_ROUNDS is for, and a
malformed draft was always a possible outcome anyway — off-catalog indicators
and lint failures both took that path before."""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field

from ..strategy.schema import StrategyDocument


class AIBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnsupportedFeature(AIBase):
    requested: str
    reason: str
    approximation: str | None = None


class StrategyDraft(AIBase):
    """One model turn. `strategy` stays a raw dict here and is validated
    through the real StrategyDocument separately, so a structurally bad
    document is a repairable draft error rather than a parse crash — the same
    split the previous design used."""

    strategy: dict
    assumptions: list[str] = Field(default_factory=list)
    unsupported: list[UnsupportedFeature] = Field(default_factory=list)
    notes: str = ""


class DraftParseError(Exception):
    """The model's reply wasn't a single JSON object. Distinct from a schema
    failure because it points at a different model mistake for the repair
    prompt (formatting, not content)."""

    def __init__(self, raw: str, cause: Exception | None = None):
        self.raw = raw
        super().__init__(f"response was not valid JSON: {cause}")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_draft_json(text: str) -> dict:
    """Parse the model's reply into a dict.

    The prompt asks for a bare JSON object, but a markdown fence is the one
    deviation worth tolerating rather than burning a repair round on — it's
    the most common way a model 'complies' and the content inside is
    invariably fine. Anything else is a genuine formatting failure and goes to
    the repair loop."""
    body = text.strip()
    fenced = _FENCE.search(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise DraftParseError(text, e)
    if not isinstance(parsed, dict):
        raise DraftParseError(text, ValueError("expected a JSON object"))
    return parsed


def document_schema_json() -> str:
    """The real StrategyDocument JSON Schema, for embedding in the (cached)
    system prompt. Compact separators — this is ~6KB of the stable prefix and
    is never read by a human."""
    return json.dumps(StrategyDocument.model_json_schema(), separators=(",", ":"))


def strip_server_owned(doc: dict) -> dict:
    """Remove fields the server owns before showing a document back to the
    model for refinement. `meta.id` is re-injected after generation to
    preserve identity across turns; version/generated_by are managed at save
    time by strategy/files.py."""
    out = {k: v for k, v in doc.items() if k != "schema_version"}
    meta = dict(out.get("meta") or {})
    for key in ("id", "version", "generated_by"):
        meta.pop(key, None)
    out["meta"] = meta
    return out
