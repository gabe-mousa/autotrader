"""AI integration tests. NEVER hits the real Anthropic API — a FakeMessages
client stands in for anthropic_client.messages, so these are free and
deterministic. Exercises: the plain-JSON response contract (the model emits the
REAL StrategyDocument shape — see ai/schema.py for why constrained decoding was
dropped), draft parsing incl. fenced/garbage replies, the repair loop, refine
round-tripping, and refusal handling."""

from __future__ import annotations

import json

import pytest

from app.ai.client import AIManager, AIRefusalError, NotConfiguredError
from app.ai.schema import DraftParseError, extract_draft_json
from app.db import Database


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, content_text: str | None = None, stop_reason: str = "end_turn",
                stop_details=None):
        self.content = [FakeTextBlock(content_text)] if content_text is not None else []
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class FakeStopDetails:
    def __init__(self, category, explanation):
        self.category = category
        self.explanation = explanation


VALID_DRAFT = {
    "strategy": {
        "meta": {"name": "AI SMA Cross", "description": "auto", "tags": []},
        "universe": {"symbols": ["SPY"]},
        # intraday: session.flatten_eod is only meaningful on intraday bars
        # (lint enforces this), and this fixture exercises the session levers
        "timeframe": {"bar": "5m"},
        "session": {"hours": "regular",
                    "entry_window": {"start": "09:45", "end": "15:30"},
                    "flatten_eod": True},
        "indicators": [{"id": "rsi14", "type": "rsi",
                       "params": {"period": 14}, "source": "close"}],
        "entry": {"long": {"when": {"op": "lt", "left": {"ind": "rsi14"},
                                    "right": {"value": 30}}}},
        "exit": {"stop_loss": {"type": "percent", "value": 2.0},
                 "take_profit": {"type": "percent", "value": 5.0}},
        "sizing": {"type": "percent_of_equity", "value": 20},
        "orders": {"entry": {"type": "marketable_limit", "offset_bps": 15},
                   "exit": {"type": "market"}, "unfilled_timeout_sec": 30,
                   "duration": "DAY"},
    },
    "assumptions": ["defaulted size to 20%"],
    "unsupported": [],
    "notes": "Simple RSI dip buy.",
}

# invalid: unknown indicator type referenced by a rule -> real StrategyDocument
# validation fails structurally (not just lint) since ind type is checked in schema
INVALID_DRAFT_UNKNOWN_INDICATOR = {
    "strategy": {
        "meta": {"name": "Bad", "description": "", "tags": []},
        "universe": {"symbols": ["SPY"]},
        "timeframe": {"bar": "1d"},
        "indicators": [{"id": "x", "type": "supertrend", "params": {}}],
        "entry": {"long": {"when": {"op": "lt", "left": {"ind": "x"},
                                    "right": {"value": 1}}}},
        "exit": {"stop_loss": {"type": "percent", "value": 2.0}},
        "sizing": {"type": "percent_of_equity", "value": 10},
    },
    "assumptions": [], "unsupported": [], "notes": "",
}


class FakeMessages:
    """Queue of canned responses, one per .create() call, so tests can script
    a repair-loop sequence (bad draft -> repaired draft)."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeMessages exhausted — too many calls")
        return self._responses.pop(0)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


def draft_response(draft: dict) -> FakeResponse:
    return FakeResponse(json.dumps(draft))


# ---- schema / JSON-schema safety ------------------------------------------

def test_model_is_shown_the_real_document_schema():
    """The whole point of dropping constrained decoding: the model sees the
    REAL StrategyDocument — including the recursive expression types and the
    session/orders sections the old AI-facing schema had to omit."""
    from app.ai.prompts import full_system_prompt
    from app.ai.schema import document_schema_json

    schema = json.loads(document_schema_json())
    defs = schema.get("$defs", {})
    # recursive expression types are present now (they could not be, before)
    assert "AllExpr" in defs and "NotExpr" in defs
    # and the sections the model used to call "unsupported"
    assert "Session" in defs and "Orders" in defs and "EntryWindow" in defs

    prompt = full_system_prompt()
    assert "flatten_eod" in prompt and "entry_window" in prompt
    # the prompt must actively forbid the observed failure mode
    assert "time_stop" in prompt


def test_extract_draft_json_tolerates_a_markdown_fence():
    inner = {"strategy": {"meta": {"name": "x"}}, "notes": "hi"}
    assert extract_draft_json(f"```json\n{json.dumps(inner)}\n```") == inner
    assert extract_draft_json(json.dumps(inner)) == inner


def test_extract_draft_json_rejects_prose_and_non_objects():
    with pytest.raises(DraftParseError):
        extract_draft_json("Sure! Here is your strategy: it buys SPY.")
    with pytest.raises(DraftParseError):
        extract_draft_json("[1, 2, 3]")


def test_strip_server_owned_hides_server_managed_meta():
    from app.ai.schema import strip_server_owned

    out = strip_server_owned({
        "schema_version": 1,
        "meta": {"id": "abc", "version": 7, "generated_by": {"model": "x"}, "name": "N"},
        "universe": {"symbols": ["SPY"]},
    })
    assert "schema_version" not in out
    assert out["meta"] == {"name": "N"}
    assert out["universe"] == {"symbols": ["SPY"]}


# ---- AIManager: not configured ---------------------------------------------

async def test_not_configured_raises(db):
    mgr = AIManager(api_key="", db=db)
    assert not mgr.configured
    with pytest.raises(NotConfiguredError):
        await mgr.generate("buy SPY on RSI dip")


# ---- AIManager: happy path --------------------------------------------------

async def test_generate_happy_path_persists_conversation(db):
    fake = FakeMessages([draft_response(VALID_DRAFT)])
    mgr = AIManager(api_key="sk-test", db=db, client=fake)
    conv_id, result = await mgr.generate("buy SPY on RSI dip")
    assert result.ok
    assert result.document.meta.name == "AI SMA Cross"
    assert result.document.entry.long is not None
    assert len(fake.calls) == 1
    # cache_control present on the stable system prefix
    sys_blocks = fake.calls[0]["system"]
    assert sys_blocks[0]["cache_control"] == {"type": "ephemeral"}
    # persisted: one conversation, two messages (user + assistant)
    conv = await db.fetch_one("SELECT id FROM ai_conversations WHERE id=?", (conv_id,))
    assert conv is not None
    msgs = await db.fetch_all(
        "SELECT role FROM ai_messages WHERE conversation_id=? ORDER BY id", (conv_id,))
    assert [m["role"] for m in msgs] == ["user", "assistant"]


async def test_generate_sends_no_output_config(db):
    """Constrained decoding is deliberately NOT used — its grammar ceiling is
    what made session/orders unreachable. Re-adding output_config would silently
    reintroduce the 'compiled grammar is too large' 400."""
    fake = FakeMessages([draft_response(VALID_DRAFT)])
    mgr = AIManager(api_key="sk-test", db=db, client=fake)
    await mgr.generate("anything")
    assert "output_config" not in fake.calls[0]
    assert fake.calls[0]["thinking"] == {"type": "adaptive"}


async def test_generate_round_trips_session_and_order_levers(db):
    """The regression this whole rewrite exists for: the model asked for a
    session window / flatten_eod / marketable-limit orders and they must land
    in the saved document instead of being dropped or approximated."""
    fake = FakeMessages([draft_response(VALID_DRAFT)])
    mgr = AIManager(api_key="sk-test", db=db, client=fake)
    _, result = await mgr.generate("day-trade SPY, flat by the close")
    doc = result.document
    assert doc.session.entry_window is not None
    assert (doc.session.entry_window.start, doc.session.entry_window.end) == ("09:45", "15:30")
    assert doc.session.flatten_eod is True
    assert doc.orders.entry.type == "marketable_limit"
    assert doc.orders.entry.offset_bps == 15
    assert doc.orders.unfilled_timeout_sec == 30


# ---- repair loop ------------------------------------------------------------

async def test_repair_loop_recovers_on_second_attempt(db):
    fake = FakeMessages([
        draft_response(INVALID_DRAFT_UNKNOWN_INDICATOR),
        draft_response(VALID_DRAFT),
    ])
    mgr = AIManager(api_key="sk-test", db=db, client=fake)
    conv_id, result = await mgr.generate("buy SPY on RSI dip")
    assert result.ok
    assert len(fake.calls) == 2
    # second call's messages include the repair instruction
    second_messages = fake.calls[1]["messages"]
    assert any("failed validation" in m["content"] for m in second_messages
              if isinstance(m["content"], str))


async def test_repair_loop_gives_up_after_max_rounds(db):
    fake = FakeMessages([draft_response(INVALID_DRAFT_UNKNOWN_INDICATOR)] * 3)
    mgr = AIManager(api_key="sk-test", db=db, client=fake)
    conv_id, result = await mgr.generate("buy something weird")
    assert not result.ok
    assert result.errors
    assert len(fake.calls) == 3  # initial + 2 repair rounds


# ---- refusal ----------------------------------------------------------------

async def test_refusal_raises_with_category(db):
    fake = FakeMessages([FakeResponse(
        content_text=None, stop_reason="refusal",
        stop_details=FakeStopDetails("cyber", "policy declined"))])
    mgr = AIManager(api_key="sk-test", db=db, client=fake)
    with pytest.raises(AIRefusalError) as exc:
        await mgr.generate("do something disallowed")
    assert exc.value.category == "cyber"


# ---- refine ------------------------------------------------------------------

async def test_refine_preserves_strategy_id_across_turns(db):
    fake = FakeMessages([draft_response(VALID_DRAFT)])
    mgr = AIManager(api_key="sk-test", db=db, client=fake)
    current = {
        "schema_version": 1,
        "meta": {"id": "fixed-id-123", "name": "Old Name", "version": 3},
        "universe": {"symbols": ["QQQ"]},
        "timeframe": {"bar": "1d"},
        "indicators": [],
        "entry": {"long": None},
        "exit": {},
        "sizing": {"type": "fixed_dollars", "value": 1000},
    }
    conv_id, result = await mgr.refine("conv-1", "make it tighter", current)
    assert result.ok
    assert result.document.meta.id == "fixed-id-123"
    # AI's own chosen name is kept (not overwritten with the old name)
    assert result.document.meta.name == "AI SMA Cross"


# ---- explain ------------------------------------------------------------------

async def test_explain_returns_plain_text_no_document(db):
    fake = FakeMessages([FakeResponse("RSI measures momentum on a 0-100 scale.")])
    mgr = AIManager(api_key="sk-test", db=db, client=fake)
    answer = await mgr.explain("what is RSI?")
    assert "momentum" in answer
    assert "output_config" not in fake.calls[0]  # explain is NOT structured output
