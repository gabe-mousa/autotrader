"""Objective function and trial constraints (docs/plan/14-optimizer.md §6).

The user's instinct was "reward = percent return, maybe combined with Sharpe
for risk". This module honours that instinct but refuses the naive form of
it, for two reasons:

* **Blending correlated metrics into one scalar just moves the overfitting.**
  `0.7*return + 0.3*sharpe` has two free weights nobody can justify, and the
  first thing you do when the answer looks wrong is tune the weights — which
  is fitting the objective to the data.

* **The dangerous trials are not the low-scoring ones, they are the
  structurally meaningless ones.** A parameter set that takes 4 trades and
  wins 3 shows a Sharpe of 5. No weighting scheme demotes it; only a hard
  minimum-trade constraint does.

So: constraints reject, then ONE primary metric is aggregated across the
out-of-sample folds with a penalty for inconsistency between them. Risk is
accounted for by the metric (Sharpe/Sortino/Calmar are already
risk-adjusted), by the drawdown constraint, and by the consistency penalty —
not by a hand-tuned blend.

Every input used here is already produced by `backtest/metrics.py`; this
module computes nothing about markets itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Metrics a study may optimize. All are "higher is better" — enforced here so
# the samplers and the leaderboard can assume maximization everywhere.
METRICS = ("sharpe", "sortino", "calmar", "total_return_pct", "profit_factor", "expectancy")
DEFAULT_METRIC = "sharpe"

# Weight on the std-dev of per-fold out-of-sample scores. 0 = pure mean (a
# strategy that scores 4.0, 0.1, 2.0 ties one that scores 2.0, 2.1, 1.9);
# 0.5 = the default, which prefers the consistent one.
DEFAULT_LAMBDA = 0.5


@dataclass(frozen=True)
class Constraints:
    """Structural minimums a trial must clear to be scored at all.

    Defaults are deliberately strict. Every one of these has produced a
    spectacular-looking, untradeable backtest at some point in the history of
    the field; `min_trades_per_fold` most of all.
    """

    min_trades_per_fold: int = 30
    min_trades_total: int = 150
    max_drawdown_pct: float = 40.0        # magnitude; trials worse than this are rejected
    max_losing_streak: int = 15
    require_all_folds_traded: bool = True

    @classmethod
    def from_json(cls, raw: dict | None) -> Constraints:
        raw = raw or {}
        try:
            c = cls(
                min_trades_per_fold=int(raw.get("min_trades_per_fold", 30)),
                min_trades_total=int(raw.get("min_trades_total", 150)),
                max_drawdown_pct=float(raw.get("max_drawdown_pct", 40.0)),
                max_losing_streak=int(raw.get("max_losing_streak", 15)),
                require_all_folds_traded=bool(raw.get("require_all_folds_traded", True)),
            )
        except (TypeError, ValueError) as e:
            raise ValueError(f"invalid constraints: {e}")
        if c.min_trades_per_fold < 0 or c.min_trades_total < 0:
            raise ValueError("trade minimums cannot be negative")
        if not (0 < c.max_drawdown_pct <= 100):
            raise ValueError("max_drawdown_pct must be in (0, 100]")
        if c.max_losing_streak < 1:
            raise ValueError("max_losing_streak must be >= 1")
        return c

    def to_json(self) -> dict:
        return {
            "min_trades_per_fold": self.min_trades_per_fold,
            "min_trades_total": self.min_trades_total,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_losing_streak": self.max_losing_streak,
            "require_all_folds_traded": self.require_all_folds_traded,
        }


@dataclass(frozen=True)
class Objective:
    metric: str = DEFAULT_METRIC
    lam: float = DEFAULT_LAMBDA
    constraints: Constraints = Constraints()

    @classmethod
    def from_json(cls, raw: dict | None) -> Objective:
        raw = raw or {}
        metric = raw.get("metric", DEFAULT_METRIC)
        if metric not in METRICS:
            raise ValueError(f"metric must be one of {', '.join(METRICS)} (got {metric!r})")
        try:
            lam = float(raw.get("lambda", DEFAULT_LAMBDA))
        except (TypeError, ValueError):
            raise ValueError("lambda must be a number")
        if not (0 <= lam <= 5):
            raise ValueError("lambda must be between 0 and 5")
        return cls(metric=metric, lam=lam,
                   constraints=Constraints.from_json(raw.get("constraints")))

    def to_json(self) -> dict:
        return {"metric": self.metric, "lambda": self.lam,
                "constraints": self.constraints.to_json()}


def metric_value(metrics: dict, name: str) -> float | None:
    """Pull one metric out of a `compute_metrics` result as a finite float.

    `compute_metrics` legitimately returns None for several metrics (Sharpe
    with zero variance, Sortino with no losing bars, profit factor with no
    losses). Those are NOT zeros — a `None` Sortino means "no downside
    observed", which on a 3-trade sample is meaningless, not excellent.
    Returning None here routes the trial to rejection rather than letting an
    absent denominator top the leaderboard.
    """
    v = metrics.get(name)
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def check_constraints(fold_metrics: list[dict], c: Constraints) -> str | None:
    """Return a rejection reason, or None if the trial is scoreable.

    `fold_metrics` is the list of OUT-OF-SAMPLE (test-slice) metric dicts, one
    per fold, in fold order.
    """
    if not fold_metrics:
        return "no folds produced results"

    counts = [int(m.get("trade_count") or 0) for m in fold_metrics]
    total = sum(counts)

    if c.require_all_folds_traded and any(n == 0 for n in counts):
        idle = [i for i, n in enumerate(counts) if n == 0]
        return (f"no trades in fold(s) {', '.join(map(str, idle))} — a parameter set that only "
                "trades in some regimes isn't a strategy")
    # Per-fold before total: it's the more diagnostic message. A trial can
    # clear the total on the back of one busy fold while another fold has
    # four trades, and "the thinnest fold has 4" tells the user what's wrong
    # where "120 total, need 150" does not.
    worst = min(counts)
    if worst < c.min_trades_per_fold:
        return (f"only {worst} trades in the thinnest fold, need >= {c.min_trades_per_fold} "
                "(too few to distinguish edge from luck)")
    if total < c.min_trades_total:
        return f"{total} trades total across folds, need >= {c.min_trades_total}"

    for m in fold_metrics:
        dd = m.get("max_drawdown_pct")
        if dd is not None and abs(float(dd)) > c.max_drawdown_pct:
            return f"max drawdown {abs(float(dd)):.1f}% exceeds the {c.max_drawdown_pct:.0f}% limit"
        streak = m.get("longest_losing_streak")
        if streak is not None and int(streak) > c.max_losing_streak:
            return f"{int(streak)} consecutive losses exceeds the {c.max_losing_streak} limit"

    return None


@dataclass
class TrialScore:
    score: float
    oos_mean: float
    oos_std: float
    is_mean: float | None
    per_fold: list[float]

    def to_json(self) -> dict:
        return {"score": self.score, "oos_mean": self.oos_mean, "oos_std": self.oos_std,
                "is_mean": self.is_mean, "per_fold": self.per_fold}


def score_trial(oos_metrics: list[dict], is_metrics: list[dict] | None,
                obj: Objective) -> TrialScore | str:
    """Score a constraint-passing trial, or return a rejection string.

    score = mean(per-fold OOS metric) - lambda * std(per-fold OOS metric)

    The penalty term is what encodes "I want returns, but not luck". A
    parameter set that only works in one of three regimes has a high std and
    loses to a duller, steadier one — which is the behaviour you want when the
    thing is going to run forward into a regime you haven't seen.
    """
    per_fold: list[float] = []
    for m in oos_metrics:
        v = metric_value(m, obj.metric)
        if v is None:
            return (f"{obj.metric} is undefined in at least one fold (usually zero variance or "
                    "no losing trades — too few observations to be meaningful)")
        per_fold.append(v)

    n = len(per_fold)
    mean = sum(per_fold) / n
    # Population std: we are describing THESE folds, not estimating a wider
    # population from a sample of 3.
    var = sum((x - mean) ** 2 for x in per_fold) / n
    std = math.sqrt(var)

    is_mean = None
    if is_metrics:
        vals = [v for v in (metric_value(m, obj.metric) for m in is_metrics) if v is not None]
        if vals:
            is_mean = sum(vals) / len(vals)

    return TrialScore(score=mean - obj.lam * std, oos_mean=mean, oos_std=std,
                      is_mean=is_mean, per_fold=per_fold)


def aggregate_oos_metrics(oos_metrics: list[dict]) -> dict:
    """A single summary row for the trials table: per-fold means of the
    headline metrics, plus summed trade counts.

    Averaging ratios across folds is not the same as computing the ratio over
    the concatenated equity curve, and it is the right choice here: it treats
    each regime as one observation instead of letting the longest or most
    volatile fold dominate. The trials table labels these as fold averages.
    """
    out: dict[str, Any] = {}
    keys = ("total_return_pct", "sharpe", "sortino", "calmar", "max_drawdown_pct",
            "win_rate_pct", "profit_factor", "expectancy", "exposure_pct")
    for k in keys:
        vals = [v for v in (metric_value(m, k) for m in oos_metrics) if v is not None]
        out[k] = round(sum(vals) / len(vals), 4) if vals else None
    out["trade_count"] = sum(int(m.get("trade_count") or 0) for m in oos_metrics)
    out["folds"] = len(oos_metrics)
    return out
