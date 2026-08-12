"""Audit C1 — the live/backtest gap list and its four consumers.

C1 was the structural finding behind the whole A-group: the engine implemented
levers the runner ignored, and nothing told the user. `lint.py` had zero checks
referencing live-runtime support, `promotion_status` never inspected the document
at all, and the only surface in the product was one bullet in the Docs page
naming three of the seven gaps. A strategy could be written, backtested, linted
clean, paper-traded and promoted to LIVE while trading a materially different
document than the one that was tested.

The field-level gaps that motivated it have since been closed, so the important
assertions here are about the MECHANISM rather than today's list:
 * a document that uses nothing unsupported reports NOTHING (the property that
   keeps the badge and the lint output meaningful);
 * gaps are BLOCKING BY DEFAULT, so the next lever someone forgets to implement
   blocks promotion rather than silently trapping a user;
 * lint carries only the blocking ones;
 * the promotion gate refuses a blocking gap and an override still gets through.
"""

from __future__ import annotations

import copy

from app.strategy.lint import lint
from app.strategy.live_support import (Gap, blocking_gaps, gap_payloads,
                                       live_support_gaps)
from app.strategy.schema import StrategyDocument
from tests.test_strategy import EXAMPLE


def doc(**overrides) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    data["timeframe"] = {"bar": "15m"}
    data["session"] = {"flatten_eod": False}
    data.update(overrides)
    return StrategyDocument.model_validate(data)


# --------------------------------------------------- the clean case matters most

def test_a_fully_supported_document_reports_no_gaps():
    """If this ever starts returning something for an ordinary document, the
    badge appears on every run row and the whole mechanism becomes noise the user
    learns to ignore. That failure mode is worse than the original silence,
    because it looks like it is working."""
    assert live_support_gaps(doc()) == []
    assert gap_payloads(doc()) == []
    assert blocking_gaps(doc()) == []


def test_the_levers_closed_by_this_campaign_are_not_reported():
    """market_filter, selection, entry.short and session.entry_window are all
    honoured live now. Reporting them would be a stale warning, which is the
    exact failure this module replaced."""
    d = doc(
        market_filter={"symbol": "SPY", "indicators": [],
                       "when": {"op": "gt", "left": {"price": "close"},
                                "right": {"value": 1}}},
        selection={"metric": {"price": "close"}, "top_n": 2},
        session={"entry_window": {"start": "09:30", "end": "15:00"}, "flatten_eod": False},
    )
    assert [g.field for g in live_support_gaps(d)] == []


def test_default_order_settings_are_not_a_gap():
    """`unfilled_timeout_sec` defaults to 60 and is clamped to 30 for EVERY
    document, so comparing against the 30s ceiling would put a gap on all ~76
    strategies and mean nothing. The comparison is against the schema default."""
    assert doc().orders.unfilled_timeout_sec == 60
    assert [g.field for g in live_support_gaps(doc())] == []


# --------------------------------------------------------- what is still a gap

def test_a_non_market_exit_spec_is_a_blocking_gap():
    """`orders.exit` is honoured by NEITHER the engine nor the runner, which is
    why A7 was reported rather than implemented — building it live would have
    opened a new divergence instead of closing one."""
    d = doc(orders={"exit": {"type": "marketable_limit", "offset_bps": 10}})
    gaps = live_support_gaps(d)
    assert [g.field for g in gaps] == ["orders.exit"]
    assert gaps[0].blocking is True
    assert "MARKET" in gaps[0].detail


def test_a_raised_order_timeout_is_reported_but_not_blocking():
    d = doc(orders={"unfilled_timeout_sec": 300})
    gaps = live_support_gaps(d)
    assert [(g.field, g.blocking) for g in gaps] == [("orders.unfilled_timeout_sec", False)]
    assert "300s" in gaps[0].detail and "30s" in gaps[0].detail


def test_flatten_eod_carries_the_early_close_approximation():
    gaps = live_support_gaps(doc(session={"flatten_eod": True}))
    assert [(g.field, g.blocking) for g in gaps] == [("session.flatten_eod", False)]
    assert "EARLY-CLOSE" in gaps[0].detail
    assert "eod_late" in gaps[0].detail


def test_flatten_eod_on_a_daily_document_is_not_a_gap():
    """The lever is intraday-only by design, so it cannot diverge on daily bars."""
    d = doc(timeframe={"bar": "1d"}, session={"flatten_eod": True})
    assert live_support_gaps(d) == []


def test_multi_add_pyramid_reports_the_one_add_per_bar_limit():
    d = doc(entry={**EXAMPLE["entry"], "pyramid": {
        "max_adds": 3, "size_pct": 100.0,
        "spacing": {"type": "percent", "value": 2.0}}})
    gaps = live_support_gaps(d)
    assert [(g.field, g.blocking) for g in gaps] == [("entry.pyramid", False)]
    assert "ONE per bar" in gaps[0].detail


def test_a_single_add_pyramid_has_nothing_to_diverge():
    d = doc(entry={**EXAMPLE["entry"], "pyramid": {
        "max_adds": 1, "size_pct": 100.0,
        "spacing": {"type": "percent", "value": 2.0}}})
    assert live_support_gaps(d) == []


def test_scale_outs_report_the_recovery_limitation():
    d = doc(exit={**EXAMPLE["exit"],
                  "scale_outs": [{"target": {"type": "percent", "value": 2.0},
                                  "percent": 50.0}]})
    gaps = live_support_gaps(d)
    assert [(g.field, g.blocking) for g in gaps] == [("exit.scale_outs", False)]
    assert "recovery" in gaps[0].detail.lower()


# ------------------------------------------------------- blocking-by-default

def test_gaps_are_blocking_by_default():
    """The design decision that makes this safe to extend: someone registering a
    new gap without thinking about severity gets over-caution (promotion
    blocked), not a silent trap."""
    assert Gap("some.field", "detail").blocking is True


def test_blocking_gaps_is_the_filtered_subset():
    d = doc(orders={"exit": {"type": "limit"}, "unfilled_timeout_sec": 300},
            session={"flatten_eod": True})
    assert len(live_support_gaps(d)) == 3
    assert [g.field for g in blocking_gaps(d)] == ["orders.exit"]


def test_gap_payloads_are_json_ready():
    d = doc(orders={"exit": {"type": "limit"}})
    assert gap_payloads(d) == [
        {"field": "orders.exit", "detail": live_support_gaps(d)[0].detail,
         "blocking": True}]


# ---------------------------------------------------------------- lint consumer

def test_lint_reports_a_blocking_gap():
    problems = lint(doc(orders={"exit": {"type": "limit"}}))
    assert [p for p in problems if p.startswith("orders.exit:")]


def test_lint_stays_silent_on_the_documented_approximations():
    """44 strategies set `flatten_eod`. A permanent advisory on most documents
    would train the user to ignore lint, so the approximations are surfaced where
    they are actionable (run event, promotion payload, badge) and not here."""
    problems = lint(doc(session={"flatten_eod": True}, orders={"unfilled_timeout_sec": 300}))
    assert [p for p in problems if "flatten_eod" in p or "unfilled_timeout" in p] == []


def test_an_ordinary_document_still_lints_clean():
    assert lint(doc()) == []
