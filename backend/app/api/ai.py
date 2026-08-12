"""AI strategy generation/refinement/explain endpoints. Degrades gracefully
(503 with a clear message) when no Anthropic key is configured — the rest of
the app (manual editor) is unaffected."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from ..ai.client import AIRefusalError, GenerationResult, NotConfiguredError
from ..logging import get_logger

router = APIRouter(prefix="/api/ai")
log = get_logger("api-ai")


def _result_payload(conversation_id: str, result: GenerationResult) -> dict:
    return {
        "conversation_id": conversation_id,
        "ok": result.ok,
        "document": (result.document.model_dump(mode="json", by_alias=True, exclude_none=True)
                    if result.document else None),
        "content_hash": None,  # computed by caller after save, not here
        "assumptions": result.draft.assumptions if result.draft else [],
        "unsupported": [u.model_dump() for u in result.draft.unsupported] if result.draft else [],
        "notes": result.raw_notes,
        "errors": result.errors,
        "warnings": result.warnings,
    }


@router.get("/status")
async def ai_status(request: Request) -> dict:
    return {"configured": request.app.state.ai.configured}


@router.post("/generate")
async def ai_generate(request: Request, body: dict) -> dict:
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if len(prompt) > 4000:
        raise HTTPException(status_code=400, detail="prompt too long (max 4000 chars)")
    ai = request.app.state.ai
    try:
        conversation_id, result = await ai.generate(prompt, body.get("conversation_id"))
    except NotConfiguredError:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured — "
                                                     "add ANTHROPIC_API_KEY to .env")
    except AIRefusalError as e:
        raise HTTPException(status_code=422, detail=f"Model declined: {e.explanation or e.category}")
    return _result_payload(conversation_id, result)


@router.post("/refine")
async def ai_refine(request: Request, body: dict) -> dict:
    # No pre-existing conversation_id is expected the first time a user
    # refines a strategy they didn't just AI-generate in this session (e.g.
    # editing an existing strategy loaded straight from the list) — mint one
    # rather than rejecting, same as /generate does when it's omitted.
    conversation_id = body.get("conversation_id") or str(uuid.uuid4())
    message = str(body.get("message", "")).strip()
    document = body.get("document")
    if not message or not document:
        raise HTTPException(status_code=400, detail="message and document are required")
    ai = request.app.state.ai
    try:
        conversation_id, result = await ai.refine(conversation_id, message, document)
    except NotConfiguredError:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured — "
                                                     "add ANTHROPIC_API_KEY to .env")
    except AIRefusalError as e:
        raise HTTPException(status_code=422, detail=f"Model declined: {e.explanation or e.category}")
    return _result_payload(conversation_id, result)


@router.post("/explain")
async def ai_explain(request: Request, body: dict) -> dict:
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    ai = request.app.state.ai
    try:
        answer = await ai.explain(question, str(body.get("context", "")))
    except NotConfiguredError:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured — "
                                                     "add ANTHROPIC_API_KEY to .env")
    except AIRefusalError as e:
        raise HTTPException(status_code=422, detail=f"Model declined: {e.explanation or e.category}")
    return {"answer": answer}
