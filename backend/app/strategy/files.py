"""On-disk strategy persistence (docs/plan/10-data-model.md).

strategies/<slug>/strategy.yaml            current working version (source of truth)
strategies/<slug>/versions/v{n}-{hash8}.yaml   immutable frozen snapshots

Disk wins over the DB index; hand-edits are first-class (rescan validates and
re-indexes). Saving freezes a snapshot when the content hash changed."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from ..db import Database
from ..logging import get_logger
from .hashing import content_hash
from .schema import StrategyDocument

log = get_logger("strategy-files")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:60] or "strategy"


@dataclass
class LoadResult:
    ok: list[tuple[str, StrategyDocument]]          # (slug, doc)
    errors: list[tuple[str, str]]                    # (path, message)


class StrategyFiles:
    def __init__(self, root: Path, db: Database):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._db = db

    # ---- disk ----------------------------------------------------------
    def _doc_path(self, slug: str) -> Path:
        return self.root / slug / "strategy.yaml"

    def load(self, slug: str) -> StrategyDocument:
        raw = yaml.safe_load(self._doc_path(slug).read_text())
        return StrategyDocument.model_validate(raw)

    def scan(self) -> LoadResult:
        ok, errors = [], []
        for d in sorted(p for p in self.root.iterdir() if p.is_dir()):
            f = d / "strategy.yaml"
            if not f.exists():
                continue
            try:
                ok.append((d.name, StrategyDocument.model_validate(yaml.safe_load(f.read_text()))))
            except (yaml.YAMLError, ValidationError) as e:
                errors.append((str(f), str(e)[:500]))
        return LoadResult(ok, errors)

    def write(self, slug: str, doc: StrategyDocument, freeze: bool) -> Path:
        """Write strategy.yaml; if freeze and the content hash is new for this
        strategy, bump meta.version and snapshot into versions/.

        `doc.meta.version` is SERVER-authoritative, never trusted from the
        caller: it's reset here to whatever's currently on disk before the
        freeze logic (which may then bump it) ever runs. Without this, a
        caller that saves an unchanged (freeze=False) edit with a stale
        `meta.version` in its payload — e.g. a UI that fetched the document
        once and never re-synced its local copy after a prior save bumped
        the version server-side — would silently regress the file's stored
        version number on every subsequent no-op save, even though the
        content itself is unaffected. Found live (2026-07-24): "save, save
        again with no further edits, version drops back to v1"."""
        d = self.root / slug
        (d / "versions").mkdir(parents=True, exist_ok=True)
        path = self._doc_path(slug)
        if path.exists():
            try:
                doc.meta.version = StrategyDocument.model_validate(
                    yaml.safe_load(path.read_text())).meta.version
            except (yaml.YAMLError, ValidationError):
                pass  # corrupt existing file — freeze logic below still sets a sane version
        h8 = content_hash(doc)[:8]
        if freeze:
            existing = sorted((d / "versions").glob("v*-*.yaml"))
            already = any(p.stem.endswith(h8) for p in existing)
            if not already:
                doc.meta.version = len(existing) + 1
                snap = d / "versions" / f"v{doc.meta.version}-{h8}.yaml"
                snap.write_text(self._dump(doc))
        path.write_text(self._dump(doc))
        return path

    @staticmethod
    def _dump(doc: StrategyDocument) -> str:
        data = doc.model_dump(mode="json", by_alias=True, exclude_none=True)
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

    # ---- DB index (disk wins) ------------------------------------------
    async def reindex(self) -> LoadResult:
        result = self.scan()
        seen_ids = set()
        for slug, doc in result.ok:
            seen_ids.add(doc.meta.id)
            h = content_hash(doc)
            await self._db.execute(
                """INSERT INTO strategies (id, slug, name, current_version, state, file_path, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET slug=excluded.slug, name=excluded.name,
                     current_version=excluded.current_version, file_path=excluded.file_path,
                     updated_at=excluded.updated_at""",
                (doc.meta.id, slug, doc.meta.name, doc.meta.version, "saved",
                 str(self._doc_path(slug)), int(time.time() * 1000)),
            )
            await self._db.execute(
                """INSERT OR IGNORE INTO strategy_versions
                   (strategy_id, version, content_hash, file_path, created_at)
                   VALUES (?,?,?,?,?)""",
                (doc.meta.id, doc.meta.version, h, str(self._doc_path(slug)),
                 int(time.time() * 1000)),
            )
        # drop index rows whose files vanished
        rows = await self._db.fetch_all("SELECT id FROM strategies")
        for r in rows:
            if r["id"] not in seen_ids:
                await self._db.execute("DELETE FROM strategies WHERE id=?", (r["id"],))
        if result.errors:
            log.warning("strategy_scan_errors", count=len(result.errors))
        return result
