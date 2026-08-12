"""Strategy schema / lint / hashing / files tests."""

from __future__ import annotations

import copy

import pytest
import yaml

from app.db import Database
from app.strategy.files import StrategyFiles, slugify
from app.strategy.hashing import content_hash
from app.strategy.lint import is_warning, lint
from app.strategy.schema import StrategyDocument

EXAMPLE = {
    "meta": {"name": "SPY RSI dip buyer", "description": "demo", "tags": ["mean-reversion"]},
    "universe": {"symbols": ["spy"]},
    "timeframe": {"bar": "1d"},
    "indicators": [
        {"id": "rsi14", "type": "rsi", "params": {"period": 14}},
    ],
    "entry": {
        "long": {"when": {"all": [
            {"op": "lt", "left": {"ind": "rsi14"}, "right": {"value": 30}},
        ]}},
    },
    "exit": {
        "signal": {"when": {"op": "gt", "left": {"ind": "rsi14"}, "right": {"value": 55}}},
        "stop_loss": {"type": "percent", "value": 2.0},
        "take_profit": {"type": "percent", "value": 5.0},
    },
    "sizing": {"type": "percent_of_equity", "value": 20},
}


def make_doc(overrides: dict | None = None) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    if overrides:
        for k, v in overrides.items():
            data[k] = v
    return StrategyDocument.model_validate(data)


# ---- schema ---------------------------------------------------------------

def test_example_validates_and_lints_clean():
    doc = make_doc()
    errors = [p for p in lint(doc) if not is_warning(p)]
    assert errors == []
    assert doc.universe.symbols == ["SPY"]  # normalized upper


def test_unknown_indicator_type_rejected():
    bad = copy.deepcopy(EXAMPLE)
    bad["indicators"][0]["type"] = "supertrend"
    with pytest.raises(Exception):
        StrategyDocument.model_validate(bad)


def test_extra_fields_forbidden():
    bad = copy.deepcopy(EXAMPLE)
    bad["surprise"] = 1
    with pytest.raises(Exception):
        StrategyDocument.model_validate(bad)


def test_yaml_roundtrip_stable():
    doc = make_doc()
    dumped = yaml.safe_dump(doc.model_dump(mode="json", by_alias=True, exclude_none=True))
    doc2 = StrategyDocument.model_validate(yaml.safe_load(dumped))
    assert content_hash(doc) == content_hash(doc2)


# ---- lint -----------------------------------------------------------------

def test_lint_unknown_indicator_ref():
    doc = make_doc({"entry": {"long": {"when": {
        "op": "lt", "left": {"ind": "nope"}, "right": {"value": 1}}}}})
    assert any("unknown indicator 'nope'" in p for p in lint(doc))


def test_lint_multi_output_needs_field():
    data = copy.deepcopy(EXAMPLE)
    data["indicators"].append({"id": "m", "type": "macd"})
    data["entry"] = {"long": {"when": {
        "op": "gt", "left": {"ind": "m"}, "right": {"value": 0}}}}
    doc = StrategyDocument.model_validate(data)
    assert any("specify field" in p for p in lint(doc))


def test_lint_no_exit_path():
    doc = make_doc({"exit": {}})
    problems = lint(doc)
    assert any("no exit path" in p for p in problems)


def test_lint_missing_stop_is_warning_only():
    doc = make_doc({"exit": {"signal": {"when": {
        "op": "gt", "left": {"ind": "rsi14"}, "right": {"value": 55}}}}})
    problems = lint(doc)
    stops = [p for p in problems if "stop_loss" in p]
    assert stops and all(is_warning(p) for p in stops)


def test_lint_risk_based_needs_stop():
    doc = make_doc({"sizing": {"type": "risk_based", "value": 1},
                    "exit": {"time_stop": {"bars": 10}}})
    assert any("risk_based requires exit.stop_loss" in p for p in lint(doc))


def test_lint_flatten_eod_daily_incompatible():
    data = copy.deepcopy(EXAMPLE)
    data["session"] = {"flatten_eod": True}
    doc = StrategyDocument.model_validate(data)
    assert any("flatten_eod is incompatible" in p for p in lint(doc))


def test_lint_two_literals():
    doc = make_doc({"entry": {"long": {"when": {
        "op": "gt", "left": {"value": 1}, "right": {"value": 2}}}}})
    assert any("two literals" in p for p in lint(doc))


# ---- hashing --------------------------------------------------------------

def test_hash_ignores_cosmetics_but_not_levers():
    a = make_doc()
    b = make_doc()
    b.meta.name = "renamed"
    b.meta.description = "totally different"
    b.meta.tags = ["x"]
    assert content_hash(a) == content_hash(b)
    c = make_doc()
    c.exit.stop_loss.value = 2.5
    assert content_hash(a) != content_hash(c)


def test_hash_int_float_equivalence():
    a = make_doc()
    b = make_doc()
    b.indicators[0].params["period"] = 14.0
    assert content_hash(a) == content_hash(b)


# ---- files ----------------------------------------------------------------

@pytest.fixture
async def files(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    yield StrategyFiles(tmp_path / "strategies", db), db
    await db.close()


async def test_write_freeze_and_scan(files):
    sf, db = files
    doc = make_doc()
    slug = slugify(doc.meta.name)
    assert slug == "spy-rsi-dip-buyer"
    sf.write(slug, doc, freeze=True)
    assert (sf.root / slug / "strategy.yaml").exists()
    versions = list((sf.root / slug / "versions").glob("v1-*.yaml"))
    assert len(versions) == 1
    # unchanged content: no second snapshot
    sf.write(slug, doc, freeze=True)
    assert len(list((sf.root / slug / "versions").glob("*.yaml"))) == 1
    # lever change: v2 snapshot
    doc.exit.stop_loss.value = 3.0
    sf.write(slug, doc, freeze=True)
    snaps = sorted((sf.root / slug / "versions").glob("*.yaml"))
    assert len(snaps) == 2 and doc.meta.version == 2

    result = await sf.reindex()
    assert len(result.ok) == 1 and not result.errors
    rows = await db.fetch_all("SELECT * FROM strategy_versions")
    assert len(rows) >= 1


async def test_saving_unchanged_content_never_regresses_the_stored_version(files):
    """Regression, reported live 2026-07-24: edit -> save (v1->v2) -> save
    again with no further edits -> version silently dropped back to v1.
    Root cause: a caller (the editor UI) that never re-syncs its local
    meta.version from the save response will resubmit the OLD version
    number on the next save; write() used to trust that caller-supplied
    value verbatim whenever freeze=False (unchanged content), silently
    overwriting the correct on-disk version. meta.version must be server-
    authoritative regardless of what the caller's copy says."""
    sf, db = files
    doc = make_doc()
    slug = slugify(doc.meta.name)
    sf.write(slug, doc, freeze=True)
    assert sf.load(slug).meta.version == 1

    edited = make_doc()
    edited.meta.version = 1  # caller's stale copy, as if freshly loaded pre-edit
    edited.exit.stop_loss.value = 5.0
    changed = content_hash(sf.load(slug)) != content_hash(edited)
    assert changed
    sf.write(slug, edited, freeze=changed)
    assert sf.load(slug).meta.version == 2
    assert sf.load(slug).exit.stop_loss.value == 5.0

    # caller saves AGAIN with no further edits, but its local copy still says
    # version=1 (never synced from the v2 response) -- must NOT regress
    resend = make_doc()
    resend.meta.version = 1
    resend.exit.stop_loss.value = 5.0  # same content as what's now on disk
    changed2 = content_hash(sf.load(slug)) != content_hash(resend)
    assert not changed2
    sf.write(slug, resend, freeze=changed2)
    assert sf.load(slug).meta.version == 2  # NOT regressed to 1
    assert sf.load(slug).exit.stop_loss.value == 5.0
    assert len(list((sf.root / slug / "versions").glob("*.yaml"))) == 2  # no bogus snapshot


async def test_scan_reports_invalid_files_without_crashing(files):
    sf, db = files
    bad_dir = sf.root / "broken"
    bad_dir.mkdir(parents=True)
    (bad_dir / "strategy.yaml").write_text("indicators: [not valid")
    doc = make_doc()
    sf.write("good", doc, freeze=False)
    result = sf.scan()
    assert len(result.ok) == 1 and len(result.errors) == 1
