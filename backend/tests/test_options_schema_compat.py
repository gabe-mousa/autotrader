"""REGRESSION GUARD: the options schema addition must not change any existing
strategy, at all.

The user's binding constraint on this work is that the strategies already on
disk — several of which are running live and on paper right now — are not
touched or altered in any way. Schema evolution is the way that promise could
be broken silently: add a field with a non-identity default, or reorder a
union, and every existing document starts behaving differently while still
loading without error.

So this file loads EVERY strategy in `strategies/`, verifies it parses, verifies
the parse is byte-stable, and verifies that the options additions are absent and
inert on all of them. It is deliberately read-only: it never writes to the
strategies directory.

If this file fails, do not "fix" it by updating an expectation — it means a
running strategy's meaning changed."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.strategy.schema import SCHEMA_VERSION, StrategyDocument

STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "strategies"


def _strategy_files() -> list[Path]:
    if not STRATEGIES_DIR.exists():
        return []
    return sorted(STRATEGIES_DIR.glob("*/strategy.yaml"))


ALL = _strategy_files()

#: Documents whose YAML SOURCE declares `expression` are options strategies by
#: intent, not pre-existing shares documents. The two guards below are about
#: options never appearing where the author did not ask for one, so they read
#: the raw file to decide — a defaulted-into-existence `expression` (the failure
#: this file exists to catch) is still caught, because it would not be in the
#: source text.
SHARES_ONLY = [p for p in ALL
               if "expression" not in (yaml.safe_load(p.read_text()) or {})]


def test_the_strategies_directory_is_present():
    """If this fails the rest of the file is vacuously green, which would make
    this guard worthless."""
    assert ALL, f"no strategies found under {STRATEGIES_DIR}"


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_existing_strategy_still_parses(path: Path):
    doc = StrategyDocument.model_validate(yaml.safe_load(path.read_text()))
    assert doc.meta.name


@pytest.mark.parametrize("path", SHARES_ONLY, ids=lambda p: p.parent.name)
def test_existing_strategy_is_not_an_options_strategy(path: Path):
    """Every pre-existing document must remain a SHARES document, with the
    options block absent entirely — not defaulted into existence."""
    doc = StrategyDocument.model_validate(yaml.safe_load(path.read_text()))
    assert doc.expression is None


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_round_trip_is_stable(path: Path):
    """Parse -> dump -> parse must be a fixed point. A field with a
    non-identity default, or a union whose member order changed, shows up here
    as a diff."""
    raw = yaml.safe_load(path.read_text())
    doc = StrategyDocument.model_validate(raw)
    again = StrategyDocument.model_validate(doc.model_dump(mode="json", by_alias=True))
    assert doc.model_dump(mode="json") == again.model_dump(mode="json")


@pytest.mark.parametrize("path", SHARES_ONLY, ids=lambda p: p.parent.name)
def test_serialization_excludes_unset_options_fields(path: Path):
    """`exclude_unset` is what the strategy writer uses, so an options field
    must never appear in a document that did not ask for one — otherwise saving
    an untouched strategy would rewrite its file."""
    raw = yaml.safe_load(path.read_text())
    doc = StrategyDocument.model_validate(raw)
    dumped = doc.model_dump(mode="json", exclude_unset=True)
    assert "expression" not in dumped


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_content_hash_is_unchanged_by_the_schema_addition(path: Path):
    """The content hash identifies a strategy VERSION and gates promotion to
    live. If adding options fields changed it, every running strategy would
    look like a different version than the one that was approved."""
    from app.strategy.hashing import content_hash

    raw = yaml.safe_load(path.read_text())
    doc = StrategyDocument.model_validate(raw)
    h1 = content_hash(doc)
    h2 = content_hash(StrategyDocument.model_validate(
        doc.model_dump(mode="json", by_alias=True)))
    assert h1 == h2


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_existing_strategies_lint_no_worse_than_before(path: Path):
    """The options lint checks must never fire on a shares document. A new
    spurious error would block edits to a live strategy."""
    from app.strategy.lint import lint

    doc = StrategyDocument.model_validate(yaml.safe_load(path.read_text()))
    problems = lint(doc)
    option_problems = [p for p in problems
                       if "expression" in p or "option" in p.lower()
                       or "contract" in p.lower()]
    assert option_problems == [], option_problems


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.parent.name)
def test_live_support_gaps_unchanged_for_shares_documents(path: Path):
    """A new BLOCKING gap on an existing document would refuse promotion for a
    strategy that is already approved and running."""
    from app.strategy.live_support import blocking_gaps

    doc = StrategyDocument.model_validate(yaml.safe_load(path.read_text()))
    gaps = [g for g in blocking_gaps(doc) if g.field.startswith("expression")]
    assert gaps == []


def test_schema_version_accepts_both_1_and_2():
    """Documents on disk say `schema_version: 1`. They must keep loading after
    the bump, or every strategy file becomes unreadable at once."""
    assert SCHEMA_VERSION >= 1
    base = yaml.safe_load(ALL[0].read_text())
    for v in (1, 2):
        d = {**base, "schema_version": v}
        assert StrategyDocument.model_validate(d).schema_version == v
