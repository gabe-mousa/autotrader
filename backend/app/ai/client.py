"""AIManager: talks to the Claude API, converts responses through the
AI-facing schema into real StrategyDocuments, and runs the validate/repair
loop. Never touches the anthropic SDK from outside this module (or, for
testability, an injected fake with the same call surface)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Protocol

from pydantic import ValidationError

from ..logging import get_logger
from ..strategy.lint import is_warning, lint
from ..strategy.schema import StrategyDocument
from .prompts import build_messages, full_system_prompt, repair_prompt
from .schema import (DraftParseError, StrategyDraft, extract_draft_json,
                     strip_server_owned)

log = get_logger("ai")

MODEL = "claude-opus-4-8"
MAX_TOKENS = 8000
MAX_REPAIR_ROUNDS = 2

# JSON-schema keys the structured-outputs endpoint doesn't support; the API
# doesn't need them for shape enforcement — Pydantic + lint fully re-validate
# every field afterward, so stripping these is safe (see docs/plan/03).
_UNSUPPORTED_SCHEMA_KEYS = {"minimum", "maximum", "exclusiveMinimum",
                           "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
                           "minItems", "maxItems", "pattern"}


def _sanitize_schema(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _sanitize_schema(v) for k, v in node.items()
                if k not in _UNSUPPORTED_SCHEMA_KEYS}
    if isinstance(node, list):
        return [_sanitize_schema(v) for v in node]
    return node


class NotConfiguredError(Exception):
    """No ANTHROPIC_API_KEY set — caller should show a setup prompt, not an error."""


class AIRefusalError(Exception):
    def __init__(self, category: str | None, explanation: str | None):
        self.category = category
        self.explanation = explanation
        super().__init__(f"model declined (category={category}): {explanation}")


class RawAnthropicClient(Protocol):
    """The subset of the anthropic SDK surface AIManager uses — lets tests
    inject a fake without hitting the network."""

    def create(self, **kwargs) -> Any: ...


class GenerationResult:
    def __init__(self, draft: StrategyDraft | None, document: StrategyDocument | None,
                errors: list[str], warnings: list[str], raw_notes: str = ""):
        self.draft = draft
        self.document = document
        self.errors = errors
        self.warnings = warnings
        self.raw_notes = raw_notes

    @property
    def ok(self) -> bool:
        return self.document is not None and not self.errors


class AIManager:
    def __init__(self, api_key: str, db=None, client: RawAnthropicClient | None = None):
        self._api_key = api_key
        self._db = db
        self._client = client  # lazily constructed unless injected (tests)
        self._system_cache: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> RawAnthropicClient:
        """Returns an object exposing .create(**kwargs) — i.e. the SDK's
        `client.messages` resource, not the top-level client (which nests
        .create under .messages). Tests inject a fake with the same surface."""
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise NotConfiguredError("ANTHROPIC_API_KEY is not set")
        import anthropic
        self._client = anthropic.Anthropic(api_key=self._api_key).messages
        return self._client

    def _system(self) -> str:
        if self._system_cache is None:
            self._system_cache = full_system_prompt()
        return self._system_cache

    def _schema(self) -> dict:
        return _sanitize_schema(StrategyDraft.model_json_schema())

    # ---- core call ------------------------------------------------------
    def _call_raw(self, messages: list[dict]) -> dict:
        """One request; returns the raw parsed JSON dict (NOT yet validated
        against StrategyDraft — that happens in the caller so a validator
        failure, e.g. an unknown indicator type, is treated as a repairable
        draft error rather than an uncaught exception). Raises AIRefusalError
        on a policy decline — refusals are not repairable, so that propagates.

        No `output_config`: the model is prompted with the real StrategyDocument
        schema and replies with plain JSON. See ai/schema.py for why constrained
        decoding was dropped (its grammar ceiling made whole sections of the
        document unreachable). Malformed JSON surfaces as DraftParseError and
        goes through the same repair loop as a schema or lint failure."""
        client = self._get_client()
        response = client.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": self._system(),
                     "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            messages=messages,
        )
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise AIRefusalError(
                getattr(details, "category", None) if details else None,
                getattr(details, "explanation", None) if details else None,
            )
        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise ValueError("no text block in response")
        return extract_draft_json(text)

    def _validate_draft(self, draft: StrategyDraft, existing_id: str | None
                        ) -> tuple[StrategyDocument | None, list[str]]:
        """Raw strategy dict -> real StrategyDocument -> lint. Returns
        (document_or_None, problems) where problems includes both validation
        errors and lint findings (warnings kept separate by caller).

        The model emits the real document shape now, so there is no conversion
        step and nothing to merge: every lever it could set, it set itself, and
        every lever it left alone keeps the value it was shown. Only `id` is
        injected (to preserve identity across refinement turns) —
        name/description/tags always come from the model's own output; version
        is managed at save time by strategy/files.py, not here."""
        full = dict(draft.strategy)
        full["schema_version"] = 1
        if existing_id:
            full["meta"] = {**(full.get("meta") or {}), "id": existing_id}
        try:
            doc = StrategyDocument.model_validate(full)
        except ValidationError as e:
            return None, [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                         for err in e.errors()]
        return doc, lint(doc)

    def _generate_with_repair(self, messages: list[dict], existing_id: str | None = None
                              ) -> GenerationResult:
        attempt_messages = list(messages)
        last_draft: StrategyDraft | None = None
        errors: list[str] = []
        warnings: list[str] = []
        for attempt in range(MAX_REPAIR_ROUNDS + 1):
            draft: StrategyDraft | None = None
            doc: StrategyDocument | None = None
            raw: dict | None = None
            try:
                raw = self._call_raw(attempt_messages)
            except DraftParseError as e:
                # the reply wasn't one JSON object — repairable like any other
                # draft mistake, but there's no dict to echo back, so the raw
                # text goes into the assistant turn instead
                errors = [str(e)]
                log.warning("ai_draft_unparseable", attempt=attempt)
                if attempt < MAX_REPAIR_ROUNDS:
                    attempt_messages = attempt_messages + [
                        {"role": "assistant", "content": e.raw[:4000]},
                        {"role": "user", "content": repair_prompt(errors)},
                    ]
                continue
            try:
                draft = StrategyDraft.model_validate(raw)
            except ValidationError as e:
                # the envelope was wrong (missing `strategy`, bad `unsupported`
                # entry, ...). Treat exactly like a lint failure, not a crash.
                errors = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                         for err in e.errors()]
            else:
                last_draft = draft
                doc, problems = self._validate_draft(draft, existing_id)
                errors = [p for p in problems if not is_warning(p)]
                warnings = [p for p in problems if is_warning(p)]
            if doc is not None and not errors:
                return GenerationResult(draft, doc, errors, warnings, draft.notes if draft else "")
            log.warning("ai_draft_invalid", attempt=attempt, errors=errors)
            if attempt < MAX_REPAIR_ROUNDS:
                attempt_messages = attempt_messages + [
                    {"role": "assistant", "content": json.dumps(raw)},
                    {"role": "user", "content": repair_prompt(errors)},
                ]
        return GenerationResult(last_draft, None, errors, warnings,
                                last_draft.notes if last_draft else "")

    # ---- public API -------------------------------------------------------
    async def generate(self, user_request: str, conversation_id: str | None = None
                       ) -> tuple[str, GenerationResult]:
        if not self.configured:
            raise NotConfiguredError("ANTHROPIC_API_KEY is not set")
        conversation_id = conversation_id or str(uuid.uuid4())
        history = await self._load_history(conversation_id) if self._db else []
        messages = build_messages(user_request, None, history)
        result = self._generate_with_repair(messages)
        await self._persist_turn(conversation_id, user_request, result)
        return conversation_id, result

    async def refine(self, conversation_id: str, message: str, current_document: dict
                     ) -> tuple[str, GenerationResult]:
        if not self.configured:
            raise NotConfiguredError("ANTHROPIC_API_KEY is not set")
        history = await self._load_history(conversation_id) if self._db else []
        # the model reads and writes the real document shape, so the document
        # goes back to it as-is — only server-owned fields are hidden
        messages = build_messages(message, strip_server_owned(current_document), history)
        existing_id = current_document.get("meta", {}).get("id")
        result = self._generate_with_repair(messages, existing_id)
        await self._persist_turn(conversation_id, message, result)
        return conversation_id, result

    async def explain(self, question: str, context: str = "") -> str:
        """Read-only Q&A — no structured output, no document mutation."""
        if not self.configured:
            raise NotConfiguredError("ANTHROPIC_API_KEY is not set")
        client = self._get_client()
        content = f"{context}\n\n{question}" if context else question
        response = client.create(
            model=MODEL, max_tokens=2000,
            system=[{"type": "text", "text": self._system(),
                     "cache_control": {"type": "ephemeral"}}],
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": content}],
        )
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise AIRefusalError(
                getattr(details, "category", None) if details else None,
                getattr(details, "explanation", None) if details else None,
            )
        return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")

    # ---- conversation persistence ---------------------------------------
    async def _load_history(self, conversation_id: str) -> list[dict]:
        rows = await self._db.fetch_all(
            "SELECT role, content_json FROM ai_messages WHERE conversation_id=? "
            "ORDER BY id", (conversation_id,))
        out = []
        for r in rows:
            payload = json.loads(r["content_json"])
            # keep history compact: prior assistant turns are stored as their
            # rendered `notes` text, not the full structured draft
            out.append({"role": r["role"], "content": payload.get("text", "")})
        return out

    async def _persist_turn(self, conversation_id: str, user_text: str,
                            result: GenerationResult) -> None:
        if self._db is None:
            return
        now = int(time.time() * 1000)
        exists = await self._db.fetch_one(
            "SELECT id FROM ai_conversations WHERE id=?", (conversation_id,))
        if not exists:
            await self._db.execute(
                "INSERT INTO ai_conversations (id, strategy_id, created_at) VALUES (?,?,?)",
                (conversation_id, None, now))
        await self._db.execute(
            "INSERT INTO ai_messages (conversation_id, role, content_json, model, created_at) "
            "VALUES (?,?,?,?,?)",
            (conversation_id, "user", json.dumps({"text": user_text}), None, now))
        assistant_summary = result.raw_notes or (
            "(draft failed validation)" if not result.ok else "")
        await self._db.execute(
            "INSERT INTO ai_messages (conversation_id, role, content_json, model, created_at) "
            "VALUES (?,?,?,?,?)",
            (conversation_id, "assistant", json.dumps({"text": assistant_summary}), MODEL, now))
