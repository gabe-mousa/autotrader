"""Unit tests for the optimizer's pure logic: search space, walk-forward
planning, objective/constraints, and samplers (docs/plan/14-optimizer.md).

These carry more weight than usual. The whole premise of this feature is that
it does NOT fool itself, and every guard that enforces that -- the holdout
split, the trade-count minimums, the consistency penalty, the refusal on thin
data -- is plain arithmetic that a test can pin down exactly.
"""

from __future__ import annotations

import copy
import math

import pytest

from app.optimize.objective import (
    Constraints,
    Objective,
    aggregate_oos_metrics,
    check_constraints,
    metric_value,
    score_trial,
)
from app.optimize.samplers import (
    Observation,
    RandomSampler,
    TPESampler,
    param_importance,
    stability_scores,
)
from app.optimize.sensitivity import (
    CAPITAL_MULTIPLIERS,
    capital_levels,
    reference_price,
    sizing_notes,
    summarize,
)
from app.optimize.space import (
    Dimension,
    apply_params,
    clamp,
    parse_space,
    suggest_space,
    validate_against_document,
)
from app.optimize.walkforward import (
    HOLDOUT_FRACTION,
    MIN_TEST_BARS,
    MIN_TOTAL_BARS,
    Fold,
    InsufficientDataError,
    plan_folds,
)
from app.strategy.schema import StrategyDocument
from app.strategy.timeframe import BAR_MS
from tests.test_strategy import EXAMPLE


def _doc(**overrides) -> StrategyDocument:
    data = copy.deepcopy(EXAMPLE)
    data.update(overrides)
    return StrategyDocument.model_validate(data)


# ---- search space ---------------------------------------------------------

def test_parse_space_accepts_typed_dimensions():
    dims = parse_space([
        {"path": "indicators.0.params.period", "type": "int", "low": 5, "high": 30},
        {"path": "exit.stop_loss.value", "type": "float", "low": 0.5, "high": 5, "log": True},
        {"path": "timeframe.bar", "type": "categorical", "choices": ["5m", "1h"]},
    ])
    assert [d.type for d in dims] == ["int", "float", "categorical"]
    assert dims[0].step == 1
    assert dims[1].log is True


def test_universe_paths_are_rejected():
    """Same prohibition sweep.py enforces: every trial shares ONE candle
    fetch, so a trial changing the universe would silently score 0 trades."""
    with pytest.raises(ValueError, match="universe"):
        parse_space([{"path": "universe.symbols.0", "type": "categorical",
                      "choices": ["SPY", "QQQ"]}])


def test_timeframe_bar_is_allowed_but_other_timeframe_paths_are_not():
    parse_space([{"path": "timeframe.bar", "type": "categorical", "choices": ["5m", "1h"]}])
    with pytest.raises(ValueError, match="timeframe"):
        parse_space([{"path": "timeframe.evaluate_on", "type": "categorical",
                      "choices": ["bar_close", "x"]}])


def test_space_size_is_capped():
    dims = [{"path": f"indicators.0.params.p{i}", "type": "int", "low": 1, "high": 9}
            for i in range(9)]
    with pytest.raises(ValueError, match="at most"):
        parse_space(dims)


def test_bad_ranges_are_rejected():
    with pytest.raises(ValueError, match="must be <"):
        parse_space([{"path": "exit.stop_loss.value", "type": "float", "low": 5, "high": 1}])
    with pytest.raises(ValueError, match="log sampling needs low > 0"):
        parse_space([{"path": "exit.stop_loss.value", "type": "float", "low": 0,
                      "high": 5, "log": True}])
    with pytest.raises(ValueError, match="duplicate dimension paths"):
        parse_space([{"path": "sizing.value", "type": "float", "low": 1, "high": 5},
                     {"path": "sizing.value", "type": "float", "low": 2, "high": 6}])


def test_validate_against_document_catches_a_path_that_does_not_exist():
    doc = _doc()
    dims = parse_space([{"path": "exit.nonexistent.value", "type": "float",
                         "low": 1, "high": 5}])
    with pytest.raises(ValueError, match="no such field"):
        validate_against_document(dims, doc)


def test_validate_against_document_catches_an_invalid_extreme():
    """A range whose END produces an invalid document must fail at study
    creation, not as a wall of rejected trials 300 trials later."""
    doc = _doc()
    dims = parse_space([{"path": "sizing.value", "type": "float", "low": -5, "high": 10}])
    with pytest.raises(ValueError, match="invalid strategy"):
        validate_against_document(dims, doc)


def test_clamp_snaps_to_the_grid_and_bounds():
    d = Dimension(path="p", type="int", low=5, high=30, step=5)
    assert clamp(d, 12.4) == 10
    assert clamp(d, 999) == 30
    assert clamp(d, -3) == 5
    f = Dimension(path="q", type="float", low=0.5, high=5.0)
    assert clamp(f, 9.9) == 5.0


def test_apply_params_does_not_mutate_the_base():
    doc = _doc()
    base = doc.model_dump(mode="json", by_alias=True)
    out = apply_params(base, {"sizing.value": 999})
    assert out["sizing"]["value"] == 999
    assert base["sizing"]["value"] != 999


def test_suggest_space_derives_editable_dimensions_from_the_strategy():
    doc = _doc()
    space = suggest_space(doc)
    assert space, "a strategy with indicators and sizing should yield suggestions"
    paths = {d["path"] for d in space}
    assert any(p.startswith("indicators.") for p in paths)
    assert "sizing.value" in paths
    # every suggestion must itself be parseable and valid against the doc
    dims = parse_space([{k: v for k, v in d.items() if k not in ("label", "current")}
                        for d in space])
    validate_against_document(dims, doc)


# ---- walk-forward planning ------------------------------------------------

def _coverage(bars: int, bar: str = "1d", start: int = 1_000_000_000_000,
              gappy: bool = False) -> dict:
    """Synthetic coverage. `gappy` mimics real market data (weekends/holidays
    for daily bars, overnight gaps for intraday) so the planner is exercised
    against timelines that are NOT contiguous multiples of the bar duration."""
    step = BAR_MS[bar]
    ts = []
    t = start
    for i in range(bars):
        ts.append(t)
        # every 5th bar is followed by a 3-bar gap, like a weekend
        t += step * (4 if gappy and i % 5 == 4 else 1)
    return {"start": ts[0], "end": ts[-1], "bars": bars, "timestamps": ts,
            "bar": bar, "per_symbol": {}}


def test_plan_reserves_an_untouched_holdout_at_the_end():
    plan = plan_folds(_coverage(5000), ["SPY"], folds=5)
    assert plan.holdout_bars == pytest.approx(5000 * HOLDOUT_FRACTION, rel=0.01)
    # the holdout sits strictly after every fold's test window
    assert plan.holdout_start == plan.search_end
    for f in plan.folds:
        assert f.test_end <= plan.holdout_start


def test_folds_are_forward_in_time_and_do_not_overlap_their_own_train():
    plan = plan_folds(_coverage(5000), ["SPY"], folds=5)
    assert len(plan.folds) == 5
    for f in plan.folds:
        assert f.train_end == f.test_start, "test must start where train ends"
        assert f.train_start < f.train_end < f.test_end
    for a, b in zip(plan.folds, plan.folds[1:]):
        assert b.test_start >= a.test_end, "test slices must march forward"


def test_the_holdout_is_the_most_recent_data_even_when_the_timeline_has_gaps():
    """Regression (found in an end-to-end run against the real DB): fold
    boundaries were computed as bar_count * bar_duration, which silently
    assumes a contiguous timeline. With real daily data (weekends/holidays)
    that put the "most recent 20%" holdout years before the end of the data
    and threw away everything after it."""
    cov = _coverage(5000, gappy=True)
    plan = plan_folds(cov, ["SPY"], folds=5)
    last_bar = cov["timestamps"][-1]
    # the holdout must actually reach the end of the stored history
    assert plan.holdout_end > last_bar
    assert plan.holdout_start < last_bar
    # and it must be the TAIL, not a middle slice
    assert plan.holdout_start == cov["timestamps"][plan.total_bars - plan.holdout_bars]
    # no fold may leak into the holdout
    for f in plan.folds:
        assert f.test_end <= plan.holdout_start


def test_fold_boundaries_land_on_real_bars():
    cov = _coverage(5000, gappy=True)
    plan = plan_folds(cov, ["SPY"], folds=4)
    real = set(cov["timestamps"])
    for f in plan.folds:
        assert f.train_start in real and f.test_start in real


def test_anchored_mode_keeps_every_train_slice_starting_at_the_origin():
    plan = plan_folds(_coverage(5000), ["SPY"], folds=4, mode="anchored")
    assert {f.train_start for f in plan.folds} == {plan.search_start}


def test_thin_history_is_refused_with_the_numbers_that_explain_why():
    """The 1m case from the real DB: ~6 weeks of data cannot support an
    honest walk-forward study, and a warning is not enough -- an
    authoritative-looking number computed from noise is worse than nothing."""
    with pytest.raises(InsufficientDataError) as e:
        plan_folds(_coverage(MIN_TOTAL_BARS - 1, "1m"), ["SPY"])
    assert e.value.detail["required_bars"] == MIN_TOTAL_BARS
    assert "backfill" in str(e.value).lower()


def test_fold_count_is_reduced_to_what_the_data_supports_and_says_so():
    """The real 5m case: ~8.5 months of history can't carry 5 folds."""
    plan = plan_folds(_coverage(2200, "5m"), ["SPY"], folds=5)
    assert len(plan.folds) < 5
    assert any("supports" in w for w in plan.warnings)
    for f in plan.folds:
        span_bars = (f.test_end - f.test_start) // BAR_MS["5m"]
        assert span_bars >= MIN_TEST_BARS


def test_a_one_or_two_fold_plan_warns_that_the_evidence_is_weak():
    plan = plan_folds(_coverage(1300), ["SPY"], folds=1)
    assert len(plan.folds) == 1
    assert any("weak evidence" in w for w in plan.warnings)


# ---- objective & constraints ----------------------------------------------

def _m(trades=100, sharpe=1.5, dd=-10.0, streak=3, **extra) -> dict:
    return {"trade_count": trades, "sharpe": sharpe, "max_drawdown_pct": dd,
            "longest_losing_streak": streak, "total_return_pct": 12.0, **extra}


def test_a_trial_with_too_few_trades_is_rejected_not_scored():
    """The single most important guard in the system: 4 trades that happen to
    win produce a spectacular Sharpe and mean nothing."""
    reason = check_constraints([_m(trades=4, sharpe=6.0)] * 3, Constraints())
    assert reason and "thinnest fold" in reason


def test_a_trial_that_sits_out_a_fold_is_rejected():
    reason = check_constraints([_m(), _m(trades=0), _m()], Constraints())
    assert reason and "no trades in fold" in reason


def test_drawdown_and_losing_streak_limits_reject():
    assert "drawdown" in check_constraints([_m(dd=-70.0)] * 3, Constraints())
    assert "consecutive losses" in check_constraints([_m(streak=40)] * 3, Constraints())


def test_a_healthy_trial_passes():
    assert check_constraints([_m()] * 3, Constraints()) is None


def test_consistency_penalty_prefers_the_steady_parameter_set():
    """The core of the user's 'return + Sharpe' instinct, done properly: a
    strategy that works in every regime beats one that works spectacularly in
    exactly one."""
    obj = Objective(metric="sharpe", lam=0.5)
    spiky = score_trial([_m(sharpe=4.0), _m(sharpe=0.1), _m(sharpe=2.0)], None, obj)
    steady = score_trial([_m(sharpe=2.0), _m(sharpe=2.1), _m(sharpe=1.9)], None, obj)
    assert spiky.oos_mean > steady.oos_mean, "the spiky one has the better raw mean"
    assert steady.score > spiky.score, "but the steady one wins the objective"


def test_lambda_zero_reduces_to_the_plain_mean():
    obj = Objective(metric="sharpe", lam=0.0)
    s = score_trial([_m(sharpe=1.0), _m(sharpe=3.0)], None, obj)
    assert s.score == pytest.approx(2.0)
    assert s.oos_std == pytest.approx(1.0)


def test_an_undefined_metric_is_a_rejection_not_a_zero_and_not_a_win():
    """compute_metrics returns None for Sortino when nothing lost. On a tiny
    sample that means 'no evidence', not 'infinitely good' -- it must never
    top the leaderboard."""
    obj = Objective(metric="sortino")
    out = score_trial([_m(sortino=None)] * 3, None, obj)
    assert isinstance(out, str) and "undefined" in out


def test_metric_value_rejects_non_finite_numbers():
    assert metric_value({"sharpe": float("inf")}, "sharpe") is None
    assert metric_value({"sharpe": float("nan")}, "sharpe") is None
    assert metric_value({"sharpe": 1.25}, "sharpe") == 1.25


def test_in_sample_mean_is_tracked_for_the_overfitting_gap():
    obj = Objective(metric="sharpe")
    s = score_trial([_m(sharpe=1.0)] * 2, [_m(sharpe=3.0)] * 2, obj)
    assert s.is_mean == pytest.approx(3.0)
    assert s.oos_mean == pytest.approx(1.0)


def test_objective_from_json_validates():
    with pytest.raises(ValueError, match="metric must be"):
        Objective.from_json({"metric": "vibes"})
    with pytest.raises(ValueError, match="lambda"):
        Objective.from_json({"lambda": 99})
    o = Objective.from_json({"metric": "calmar", "lambda": 1.0,
                             "constraints": {"min_trades_per_fold": 10}})
    assert o.constraints.min_trades_per_fold == 10


def test_aggregate_reports_fold_averages_and_summed_trades():
    agg = aggregate_oos_metrics([_m(trades=10, sharpe=1.0), _m(trades=20, sharpe=3.0)])
    assert agg["trade_count"] == 30
    assert agg["sharpe"] == pytest.approx(2.0)
    assert agg["folds"] == 2


# ---- samplers -------------------------------------------------------------

DIMS = parse_space([
    {"path": "a", "type": "float", "low": 0.0, "high": 10.0},
    {"path": "b", "type": "int", "low": 1, "high": 20},
    {"path": "c", "type": "categorical", "choices": ["x", "y", "z"]},
])


def test_samplers_respect_bounds_types_and_choices():
    for sampler in (RandomSampler(DIMS, seed=1), TPESampler(DIMS, seed=1)):
        for _ in range(50):
            p = sampler.suggest([])
            assert 0.0 <= p["a"] <= 10.0
            assert 1 <= p["b"] <= 20 and isinstance(p["b"], int)
            assert p["c"] in ("x", "y", "z")


def test_the_same_seed_reproduces_the_same_search():
    """A study records its seed; re-running it must give the identical
    sequence, otherwise none of this is auditable."""
    a = [RandomSampler(DIMS, seed=7).suggest([]) for _ in range(5)]
    b = [RandomSampler(DIMS, seed=7).suggest([]) for _ in range(5)]
    c = [RandomSampler(DIMS, seed=8).suggest([]) for _ in range(5)]
    assert a == b
    assert a != c


def _objective(p: dict) -> float:
    """Synthetic ground truth with a single broad optimum at a=7, b=5, c='y'."""
    return (-((p["a"] - 7.0) ** 2) / 10.0
            - ((p["b"] - 5) ** 2) / 20.0
            + (1.0 if p["c"] == "y" else 0.0))


def _drive(sampler, n: int) -> list[Observation]:
    hist: list[Observation] = []
    for _ in range(n):
        p = sampler.suggest(hist)
        hist.append(Observation(params=p, score=_objective(p)))
    return hist


def test_tpe_concentrates_on_the_optimum_better_than_random():
    """TPE earns its place only if it beats the unbiased baseline on a problem
    where the answer is known."""
    tpe = _drive(TPESampler(DIMS, seed=3, n_startup=20), 120)
    rnd = _drive(RandomSampler(DIMS, seed=3), 120)
    best_tpe = max(o.score for o in tpe)
    best_rnd = max(o.score for o in rnd)
    assert best_tpe > best_rnd
    # and its later trials should cluster near the optimum, not wander
    late = [o.params["a"] for o in tpe[-30:]]
    assert abs(sum(late) / len(late) - 7.0) < 2.0


def test_tpe_learns_to_avoid_regions_that_only_produce_rejected_trials():
    """Constraint-violating trials have no score; without feeding them to the
    'bad' model the sampler would propose them forever."""
    dims = parse_space([{"path": "a", "type": "float", "low": 0.0, "high": 10.0}])
    sampler = TPESampler(dims, seed=5, n_startup=15)
    hist: list[Observation] = []
    for _ in range(90):
        p = sampler.suggest(hist)
        # everything below 5 is "illegal" (rejected); above 5 scores fine
        hist.append(Observation(params=p, score=None if p["a"] < 5.0 else p["a"]))
    late_rejected = sum(1 for o in hist[-30:] if o.score is None)
    early_rejected = sum(1 for o in hist[:30] if o.score is None)
    assert late_rejected < early_rejected


def test_stability_demotes_a_lucky_spike_and_promotes_a_plateau():
    """The plateau-vs-spike test. The spike has the best raw score; the
    plateau should win once neighbourhoods are taken into account."""
    dims = parse_space([{"path": "a", "type": "float", "low": 0.0, "high": 10.0}])
    hist = [
        # a broad good region around a=2
        *[Observation({"a": v}, 2.0) for v in (1.6, 1.8, 2.0, 2.2, 2.4, 2.6)],
        # an isolated spike at a=8 surrounded by terrible neighbours
        Observation({"a": 8.0}, 5.0),
        *[Observation({"a": v}, -3.0) for v in (7.4, 7.6, 8.4, 8.6, 8.8)],
    ]
    stab = stability_scores(dims, hist, k=4)
    plateau = stab[2]     # a = 2.0
    spike = stab[6]       # a = 8.0
    assert spike is not None and plateau is not None
    assert hist[6].score > hist[2].score, "the spike has the better raw score"
    assert plateau > spike, "but the plateau wins on stability"


def test_stability_returns_none_for_unscored_trials():
    dims = parse_space([{"path": "a", "type": "float", "low": 0.0, "high": 10.0}])
    hist = [Observation({"a": 1.0}, 1.0), Observation({"a": 2.0}, None),
            Observation({"a": 3.0}, 2.0)]
    assert stability_scores(dims, hist)[1] is None


def test_param_importance_ranks_the_dimension_that_actually_matters():
    dims = parse_space([
        {"path": "a", "type": "float", "low": 0.0, "high": 10.0},
        {"path": "noise", "type": "float", "low": 0.0, "high": 10.0},
    ])
    rng = __import__("numpy").random.default_rng(0)
    hist = []
    for _ in range(120):
        a, noise = float(rng.uniform(0, 10)), float(rng.uniform(0, 10))
        hist.append(Observation({"a": a, "noise": noise}, score=-abs(a - 7.0)))
    imp = param_importance(dims, hist)
    assert imp["a"] > imp["noise"]


def test_param_importance_is_quiet_when_there_is_too_little_data():
    dims = parse_space([{"path": "a", "type": "float", "low": 0, "high": 10}])
    assert param_importance(dims, [Observation({"a": 1.0}, 1.0)]) == {"a": 0.0}


def test_fold_json_roundtrip_is_plain_data():
    f = Fold(index=0, train_start=1, train_end=2, test_start=2, test_end=3)
    assert f.to_json()["test_start"] == 2
    assert math.isfinite(HOLDOUT_FRACTION)


def test_folds_report_trading_days_not_calendar_arithmetic():
    """The fold table on the Optimize page shows trading days so the user can
    set `min_trades_per_fold` against something meaningful. Those counts must
    come from distinct session dates in the real timestamps — deriving them
    from (end - start) / bar_duration would over-count every weekend."""
    import datetime as dt

    # 6 bars per session, 400 sessions, weekends skipped: a realistic 30m-ish
    # intraday timeline where calendar span and trading days differ a lot.
    step = BAR_MS["30m"]
    ts: list[int] = []
    day = dt.datetime(2024, 1, 1, 14, 30, tzinfo=dt.UTC)
    sessions = 0
    while sessions < 400:
        if day.weekday() < 5:
            base = int(day.timestamp() * 1000)
            ts.extend(base + i * step for i in range(6))
            sessions += 1
        day += dt.timedelta(days=1)

    cov = {"start": ts[0], "end": ts[-1], "bars": len(ts), "timestamps": ts,
           "bar": "30m", "per_symbol": {}}
    plan = plan_folds(cov, ["SPY"], folds=3)

    assert plan.total_days == 400
    assert plan.bars_per_day == pytest.approx(6.0)
    # every fold reports a positive, plausible number of trading days
    for f in plan.folds:
        assert f.test_days > 0
        assert f.test_bars == pytest.approx(f.test_days * 6, rel=0.05)
        # calendar span is much larger than trading days (weekends) — the
        # whole reason this is measured rather than computed
        calendar = (f.test_end - f.test_start) / 86_400_000
        assert calendar > f.test_days
    # the folds plus the holdout account for the sessions they cover
    assert plan.holdout_days > 0
    assert sum(f.test_days for f in plan.folds) + plan.holdout_days <= plan.total_days


def test_labels_disambiguate_repeated_indicator_params():
    """A strategy with three EMAs rendered three identical `params.period`
    columns in the champion panel and the leaderboard (reported from a real
    screenshot). Labels resolve the indicator's own id instead."""
    from app.optimize.space import label_for, with_labels

    data = copy.deepcopy(EXAMPLE)
    data["indicators"] = [
        {"id": "ema5", "type": "ema", "params": {"period": 5.0}, "source": "close"},
        {"id": "ema20", "type": "ema", "params": {"period": 20.0}, "source": "close"},
        {"id": "ema200", "type": "ema", "params": {"period": 200.0}, "source": "close"},
    ]
    data["entry"] = {"long": {"when": {"op": "gt", "left": {"ind": "ema5"},
                                       "right": {"ind": "ema20"}}}}
    doc = StrategyDocument.model_validate(data)

    labels = [label_for(f"indicators.{i}.params.period", doc) for i in range(3)]
    assert labels == ["ema5.period", "ema20.period", "ema200.period"]
    assert len(set(labels)) == 3, "labels must be distinguishable"

    assert label_for("exit.stop_loss.value", doc) == "stop loss"
    assert label_for("sizing.value", doc).startswith("sizing (")

    dims = with_labels(parse_space([
        {"path": "indicators.0.params.period", "type": "int", "low": 2, "high": 50},
        {"path": "indicators.2.params.period", "type": "int", "low": 50, "high": 300},
    ]), doc)
    assert [d.name for d in dims] == ["ema5.period", "ema200.period"]
    assert dims[0].to_json()["label"] == "ema5.period"


def test_a_caller_supplied_label_is_preserved():
    doc = _doc()
    from app.optimize.space import with_labels
    dims = with_labels(
        parse_space([{"path": "sizing.value", "type": "float", "low": 1, "high": 5,
                      "label": "my own name"}]), doc)
    assert dims[0].label == "my own name"


# ---- capital sensitivity (docs/plan/14-optimizer.md §1) -------------------

def _cap_level(mult: float, ret: float | None, trades: int = 50, **extra) -> dict:
    """One rung of the capital ladder, shaped like the runner produces it."""
    metrics = None if ret is None else {"total_return_pct": ret, "trade_count": trades,
                                        "sharpe": 1.0, "max_drawdown_pct": -10.0}
    return {"multiplier": mult, "capital": 100_000 * mult, "metrics": metrics,
            "notes": [], **extra}


def _ladder(rets: dict[float, float | None], trades: dict[float, int] | None = None) -> list[dict]:
    trades = trades or {}
    return [_cap_level(m, rets.get(m), trades.get(m, 50)) for m in CAPITAL_MULTIPLIERS]


def test_capital_levels_are_a_geometric_ladder_centred_on_the_study_capital():
    """Geometric, because every mechanism capital acts through (a notional cap,
    a share price, a per-order fee) is multiplicative — equal ratios are equal
    amounts of stress, equal dollar steps are not."""
    assert capital_levels(100_000) == [25_000, 50_000, 100_000, 200_000, 400_000]
    assert 1.0 in CAPITAL_MULTIPLIERS, "the study's own capital must be a rung"
    assert capital_levels(3_333.33)[2] == 3_333.33
    with pytest.raises(ValueError):
        capital_levels(0)


def test_a_champion_that_holds_up_at_half_and_double_is_robust():
    s = summarize(_ladder({0.25: 12.0, 0.5: 18.0, 1.0: 20.0, 2.0: 19.0, 4.0: 15.0}),
                  base_capital=100_000, metric="sharpe")
    assert s["verdict"] == "robust"
    assert s["worst_retention"] == pytest.approx(0.9)
    assert "not an artifact" in s["headline"]


def test_a_champion_that_only_works_at_its_own_capital_is_called_fragile():
    s = summarize(_ladder({0.25: 2.0, 0.5: 6.0, 1.0: 20.0, 2.0: 18.0, 4.0: 12.0}),
                  base_capital=100_000, metric="sharpe")
    assert s["verdict"] == "fragile"
    assert s["worst_multiplier"] == 0.5
    assert s["worst_retention"] == pytest.approx(0.3)
    # blunt, and it names the level -- same contract as the holdout card
    assert "0.5×" in s["headline"] and "$100,000" in s["headline"]


def test_a_gain_that_becomes_a_loss_at_double_capital_is_broken():
    s = summarize(_ladder({0.25: 5.0, 0.5: 15.0, 1.0: 20.0, 2.0: -3.0, 4.0: -8.0}),
                  base_capital=100_000, metric="sharpe")
    assert s["verdict"] == "broken"
    assert "2×" in s["headline"]
    assert "artifact of the starting-capital" in s["headline"]


def test_a_level_that_cannot_fund_a_single_trade_is_broken_not_merely_weak():
    """0 trades is categorically different from a small return: the account is
    too small to open the position at all, which no percentage conveys."""
    s = summarize(_ladder({0.25: 0.0, 0.5: 0.0, 1.0: 20.0, 2.0: 21.0, 4.0: 19.0},
                          trades={0.5: 0, 0.25: 0}),
                  base_capital=100_000, metric="sharpe")
    assert s["verdict"] == "broken"
    assert "no trades at all" in s["headline"]


def test_falling_apart_only_at_the_extremes_is_reported_but_not_alarming():
    """A fixed notional cap makes 4x dilute by construction. Calling that
    'overfit' would be crying wolf, so it gets its own softer verdict."""
    s = summarize(_ladder({0.25: 3.0, 0.5: 17.0, 1.0: 20.0, 2.0: 18.0, 4.0: 19.0}),
                  base_capital=100_000, metric="sharpe")
    assert s["verdict"] == "extremes-only"
    assert "0.25×" in s["headline"]
    assert "working range" in s["headline"]


def test_same_returns_but_a_different_number_of_trades_is_still_fragile():
    s = summarize(_ladder({0.25: 19.0, 0.5: 19.0, 1.0: 20.0, 2.0: 20.5, 4.0: 20.0},
                          trades={2.0: 90}),
                  base_capital=100_000, metric="sharpe")
    assert s["verdict"] == "fragile"
    assert "not taking the same trades" in s["headline"]


def test_a_losing_champion_makes_the_ladder_inconclusive_not_robust():
    """Retention against a negative baseline is arithmetic nonsense: -1% at 2x
    against -10% at 1x is a 'retention' of 0.1, which would read as broken."""
    s = summarize(_ladder({0.25: -3.0, 0.5: -8.0, 1.0: -10.0, 2.0: -1.0, 4.0: -12.0}),
                  base_capital=100_000, metric="sharpe")
    assert s["verdict"] == "inconclusive"
    assert "no profit" in s["headline"]


def test_a_baseline_that_never_traded_is_inconclusive():
    s = summarize(_ladder({0.25: 0.0, 0.5: 0.0, 1.0: 0.0, 2.0: 0.0, 4.0: 0.0},
                          trades=dict.fromkeys(CAPITAL_MULTIPLIERS, 0)),
                  base_capital=100_000, metric="sharpe")
    assert s["verdict"] == "inconclusive"
    assert "no trades" in s["headline"]


def test_a_failed_baseline_run_is_unknown_and_says_why():
    levels = _ladder({0.25: 5.0, 0.5: 15.0, 1.0: None, 2.0: 18.0, 4.0: 12.0})
    levels[2]["error"] = "engine blew up"
    s = summarize(levels, base_capital=100_000, metric="sharpe")
    assert s["verdict"] == "unknown"
    assert "engine blew up" in s["headline"]


def test_one_failed_rung_does_not_sink_the_whole_check():
    levels = _ladder({0.25: 14.0, 0.5: 18.0, 1.0: 20.0, 2.0: 19.0, 4.0: None})
    levels[4]["error"] = "boom"
    s = summarize(levels, base_capital=100_000, metric="sharpe")
    assert s["verdict"] == "robust"
    assert s["levels"][4]["retention"] is None


def test_summarize_is_deterministic_for_identical_input():
    """The whole optimizer is reproducible by construction (seeded samplers, a
    fixed multiplier tuple, one shared candle slice). The summary must not
    introduce set/dict-ordering nondeterminism into that guarantee."""
    import json
    rets = {0.25: 3.0, 0.5: 12.0, 1.0: 20.0, 2.0: 17.5, 4.0: 9.0}
    first = json.dumps(summarize(_ladder(rets), base_capital=100_000, metric="sharpe"),
                       sort_keys=False)
    for _ in range(5):
        again = json.dumps(summarize(_ladder(rets), base_capital=100_000, metric="sharpe"),
                           sort_keys=False)
        assert again == first
    # and the ladder order itself is fixed, ascending -- the chart's x axis
    assert list(CAPITAL_MULTIPLIERS) == sorted(CAPITAL_MULTIPLIERS)


def test_reference_price_is_the_median_and_ignores_junk():
    assert reference_price([10.0, 20.0, 30.0]) == 20.0
    assert reference_price([10.0, 20.0, 30.0, 40.0]) == 25.0
    assert reference_price([float("nan"), -5.0, 0.0, 7.0]) == 7.0
    assert reference_price([]) is None


def test_notional_cap_binding_is_named_at_high_capital():
    """`max_position_notional` is the single most common reason a champion
    stops scaling: above cap/(value/100) of equity the extra dollars just sit
    in cash and dilute the percentage."""
    doc = _doc(sizing={"type": "percent_of_equity", "value": 20,
                       "max_position_notional": 25_000})
    assert not any("CLIPS" in n for n in sizing_notes(doc, 100_000, 100.0))
    notes = sizing_notes(doc, 400_000, 100.0)
    assert any("CLIPS" in n and "max_position_notional" in n for n in notes)


def test_share_rounding_is_named_at_low_capital():
    """$500 per position on a $600 stock is zero shares; $1200 is two, wasting
    a fifth of the intended size. Neither is the strategy that was optimized."""
    doc = _doc(sizing={"type": "percent_of_equity", "value": 20,
                       "max_position_notional": 25_000})
    assert any("cannot open a position at all" in n for n in sizing_notes(doc, 2_500, 600.0))
    assert any("share rounding wastes" in n for n in sizing_notes(doc, 7_000, 600.0))
    # at a sane size the quantization is negligible and stays quiet
    assert not any("share rounding" in n for n in sizing_notes(doc, 100_000, 100.0))


def test_a_concurrency_target_that_cannot_be_funded_is_named():
    doc = _doc(sizing={"type": "percent_of_equity", "value": 40,
                       "max_position_notional": 1_000_000},
               risk={"max_open_positions": 5, "max_daily_loss": 500})
    notes = sizing_notes(doc, 100_000, 50.0)
    assert any("effectively a 2-position strategy" in n for n in notes)


def test_absolute_dollar_sizing_is_flagged_as_not_scaling():
    doc = _doc(sizing={"type": "fixed_dollars", "value": 10_000})
    assert any("does not scale with capital" in n for n in sizing_notes(doc, 400_000, 100.0))


def test_a_fixed_per_order_commission_is_flagged_on_a_small_account():
    doc = _doc(sizing={"type": "percent_of_equity", "value": 20})
    notes = sizing_notes(doc, 2_000, 10.0, commission_per_order=5.0)
    assert any("bps of a" in n and "entry AND exit" in n for n in notes)
    assert not any("bps of a" in n
                   for n in sizing_notes(doc, 500_000, 10.0, commission_per_order=5.0))
