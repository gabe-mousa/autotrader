"""Strategy CRUD + catalog API. Documents are validated (schema + lint) on
every write; saving freezes an immutable version snapshot when levers changed."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from ..indicators.registry import catalog
from ..logging import get_logger
from ..strategy.files import slugify
from ..strategy.hashing import content_hash
from ..strategy.lint import is_warning, lint
from ..strategy.schema import StrategyDocument

router = APIRouter(prefix="/api")
log = get_logger("api-strategies")


@router.get("/catalog")
async def get_catalog() -> dict:
    """Indicator + schema metadata: drives UI levers, docs, and the AI prompt."""
    return {"indicators": catalog(), "schema_version": 1}


async def _latest_backtests(db) -> tuple[dict, dict]:
    """Latest finished standalone backtest per strategy, for the list page's
    at-a-glance return column. Sweep members are excluded (sweep_id IS NULL):
    a 200-combo sweep would otherwise decide the headline number by whichever
    combo happened to run last, which says nothing about the saved strategy.

    Returns (by strategy_id, by strategy_version_hash). The hash map is the
    fallback for rows written before backtests.strategy_id existed; like
    list_backtests' equivalent it can collide across copies/renames, so
    callers must prefer the id map.
    """
    sql = ("SELECT b.id, b.strategy_id, b.strategy_version_hash, b.config_json, "
           "b.metrics_json, b.started_at, b.finished_at FROM backtests b "
           "JOIN (SELECT {key} AS k, MAX(started_at) AS m FROM backtests "
           "      WHERE status = 'done' AND sweep_id IS NULL AND {key} IS NOT NULL "
           "      GROUP BY {key}) g "
           "  ON g.k = b.{key} AND g.m = b.started_at "
           "WHERE b.status = 'done' AND b.sweep_id IS NULL")

    def summarize(row) -> dict:
        cfg = json.loads(row["config_json"])
        metrics = json.loads(row["metrics_json"])["metrics"] if row["metrics_json"] else {}
        return {
            "id": row["id"],
            "total_return_pct": metrics.get("total_return_pct"),
            "max_drawdown_pct": metrics.get("max_drawdown_pct"),
            "sharpe": metrics.get("sharpe"),
            "start": cfg.get("start"),
            "end": cfg.get("end"),
            "finished_at": row["finished_at"],
        }

    # MAX(started_at) can tie (same-second runs); last write wins, which is
    # as good a tiebreak as any for two runs of the same strategy.
    by_id = {r["strategy_id"]: summarize(r)
             for r in await db.fetch_all(sql.format(key="strategy_id"))}
    by_hash = {r["strategy_version_hash"]: summarize(r)
               for r in await db.fetch_all(sql.format(key="strategy_version_hash"))}
    return by_id, by_hash


@router.get("/strategies")
async def list_strategies(request: Request, archived: bool = False) -> dict:
    """Archived strategies are hidden from the default list (and therefore
    from every picker that calls this with no args — Backtests' strategy
    dropdown, the sweep form, etc.) without being deleted from disk. Pass
    ?archived=true to see the archive instead."""
    files = request.app.state.strategy_files
    result = files.scan()
    bt_by_id, bt_by_hash = await _latest_backtests(request.app.state.db)
    out = []
    for slug, doc in result.ok:
        if doc.meta.archived != archived:
            continue
        full_hash = content_hash(doc)
        out.append({
            "starred": doc.meta.starred,
            "last_backtest": bt_by_id.get(doc.meta.id) or bt_by_hash.get(full_hash),
            "slug": slug,
            "id": doc.meta.id,
            "name": doc.meta.name,
            "description": doc.meta.description,
            "tags": doc.meta.tags,
            "version": doc.meta.version,
            "content_hash": full_hash[:8],
            "symbols": doc.universe.symbols,
            "bar": doc.timeframe.bar,
            "archived": doc.meta.archived,
            # Instrument is part of a strategy's identity, not a detail: the
            # list must show which of these trade contracts without the user
            # opening each one.
            "instrument": ("options"
                           if getattr(doc, "expression", None) is not None
                           and doc.expression.instrument == "options"
                           else "shares"),
        })
    # deliberately unsorted: starring is a marker, not a reordering — rows stay
    # where they were so the list you learned doesn't shuffle under you
    return {"strategies": out,
            "errors": [{"path": p, "message": m} for p, m in result.errors]}


@router.get("/strategies/{slug}")
async def get_strategy(request: Request, slug: str) -> dict:
    files = request.app.state.strategy_files
    try:
        doc = files.load(slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"strategy {slug!r} not found")
    problems = lint(doc)
    return {
        "slug": slug,
        "document": doc.model_dump(mode="json", by_alias=True, exclude_none=True),
        "content_hash": content_hash(doc),
        "warnings": [p for p in problems if is_warning(p)],
        "errors": [p for p in problems if not is_warning(p)],
    }


def _validate_payload(payload: dict) -> tuple[StrategyDocument, list[str], list[str]]:
    try:
        doc = StrategyDocument.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=[
            {"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]}
            for err in e.errors()
        ])
    problems = lint(doc)
    return doc, [p for p in problems if not is_warning(p)], [p for p in problems if is_warning(p)]


@router.post("/strategies/validate")
async def validate_strategy(body: dict) -> dict:
    doc, errors, warnings = _validate_payload(body.get("document", body))
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "content_hash": content_hash(doc)}


@router.post("/strategies")
async def create_strategy(request: Request, body: dict) -> dict:
    files = request.app.state.strategy_files
    doc, errors, warnings = _validate_payload(body.get("document", body))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    slug = slugify(doc.meta.name)
    if (files.root / slug / "strategy.yaml").exists():
        raise HTTPException(status_code=409, detail=f"strategy {slug!r} already exists")
    doc.meta.version = 1
    files.write(slug, doc, freeze=True)
    await files.reindex()
    await request.app.state.db.audit("user", "strategy",
                                     {"event": "created", "slug": slug})
    return {"slug": slug, "content_hash": content_hash(doc), "warnings": warnings}


@router.put("/strategies/{slug}")
async def save_strategy(request: Request, slug: str, body: dict) -> dict:
    files = request.app.state.strategy_files
    if not (files.root / slug / "strategy.yaml").exists():
        raise HTTPException(status_code=404, detail=f"strategy {slug!r} not found")
    doc, errors, warnings = _validate_payload(body.get("document", body))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    existing = files.load(slug)
    if existing.meta.id != doc.meta.id:
        raise HTTPException(status_code=400, detail="meta.id mismatch — wrong strategy?")
    changed = content_hash(existing) != content_hash(doc)
    files.write(slug, doc, freeze=changed)
    await files.reindex()
    await request.app.state.db.audit("user", "strategy",
                                     {"event": "saved", "slug": slug, "new_version": changed})
    return {"slug": slug, "content_hash": content_hash(doc),
            "new_version": changed, "version": doc.meta.version, "warnings": warnings}


@router.post("/strategies/{slug}/archive")
async def set_strategy_archived(request: Request, slug: str, body: dict) -> dict:
    """Toggle meta.archived. Not a content edit — never freezes a version
    snapshot, so archiving/restoring never bumps meta.version or writes to
    versions/."""
    files = request.app.state.strategy_files
    if not (files.root / slug / "strategy.yaml").exists():
        raise HTTPException(status_code=404, detail=f"strategy {slug!r} not found")
    archived = bool(body.get("archived", True))
    doc = files.load(slug)
    doc.meta.archived = archived
    files.write(slug, doc, freeze=False)
    await files.reindex()
    await request.app.state.db.audit(
        "user", "strategy",
        {"event": "archived" if archived else "unarchived", "slug": slug})
    return {"slug": slug, "archived": archived}


@router.post("/strategies/{slug}/star")
async def set_strategy_starred(request: Request, slug: str, body: dict) -> dict:
    """Toggle meta.starred. Like archive, this is not a content edit — no
    version snapshot is frozen and meta.version is untouched."""
    files = request.app.state.strategy_files
    if not (files.root / slug / "strategy.yaml").exists():
        raise HTTPException(status_code=404, detail=f"strategy {slug!r} not found")
    starred = bool(body.get("starred", True))
    doc = files.load(slug)
    doc.meta.starred = starred
    files.write(slug, doc, freeze=False)
    await files.reindex()
    await request.app.state.db.audit(
        "user", "strategy",
        {"event": "starred" if starred else "unstarred", "slug": slug})
    return {"slug": slug, "starred": starred}


@router.post("/strategies/rescan")
async def rescan(request: Request) -> dict:
    result = await request.app.state.strategy_files.reindex()
    return {"loaded": len(result.ok),
            "errors": [{"path": p, "message": m} for p, m in result.errors]}
