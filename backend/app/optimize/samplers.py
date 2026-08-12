"""Samplers: how the optimizer chooses the next parameter set to try
(docs/plan/14-optimizer.md §7).

Written in-house rather than pulling in Optuna. Optuna would drag in
sqlalchemy + alembic and its own storage layer, while the two samplers we
actually need are a couple of hundred lines. Writing them here also buys
**exact seeded determinism**, which matters more than usual for this feature:
a system whose entire value proposition is "it doesn't fool itself" needs
samplers that can be unit-tested, not a black box.

Both samplers maximize. Rejected (constraint-violating) trials are still fed
back in with a sentinel score so TPE learns to avoid those regions rather
than repeatedly proposing parameter sets that take four trades.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .space import Dimension, clamp


@dataclass
class Observation:
    params: dict[str, Any]     # {path: value}
    score: float | None        # None == rejected/errored trial


class BaseSampler:
    name = "base"

    def __init__(self, dims: list[Dimension], seed: int = 0):
        self.dims = dims
        self.rng = np.random.default_rng(seed)

    def _random_point(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for d in self.dims:
            if d.type == "categorical":
                out[d.path] = d.choices[int(self.rng.integers(len(d.choices)))]
            elif d.log:
                lo, hi = math.log(d.low), math.log(d.high)
                out[d.path] = clamp(d, math.exp(self.rng.uniform(lo, hi)))
            else:
                out[d.path] = clamp(d, self.rng.uniform(d.low, d.high))
        return out

    def suggest(self, history: list[Observation]) -> dict[str, Any]:
        raise NotImplementedError


class RandomSampler(BaseSampler):
    """Uniform (or log-uniform) random search.

    Not a strawman: for a handful of dimensions random search is a strong,
    unbiased baseline, and crucially it does NOT concentrate its trials around
    an early lucky point — which makes its leaderboard less overfit than a
    guided search's, even when its best score is lower.
    """

    name = "random"

    def suggest(self, history: list[Observation]) -> dict[str, Any]:
        return self._random_point()


class TPESampler(BaseSampler):
    """Tree-structured Parzen Estimator.

    Split the observed trials at the top-gamma quantile into "good" and "bad".
    Fit a density over each group, per dimension: l(x) from the good ones,
    g(x) from the bad. Then draw candidates from l(x) and keep the one
    maximizing l(x)/g(x) — i.e. the point most characteristic of good trials
    and least characteristic of bad ones.

    Dimensions are modelled independently (the standard TPE simplification).
    Parameter *interactions* are still exploited, just indirectly: the good
    set contains only jointly-good combinations, so each marginal is
    conditioned on the others being good.
    """

    name = "tpe"

    def __init__(self, dims: list[Dimension], seed: int = 0, *, n_startup: int = 20,
                 gamma: float = 0.25, n_candidates: int = 24):
        super().__init__(dims, seed)
        self.n_startup = n_startup
        self.gamma = gamma
        self.n_candidates = n_candidates

    def suggest(self, history: list[Observation]) -> dict[str, Any]:
        scored = [h for h in history if h.score is not None and math.isfinite(h.score)]
        rejected = [h for h in history if h.score is None]

        # Cold start: TPE has nothing to model until it has seen a spread of
        # outcomes. Random-sample until then.
        if len(scored) < self.n_startup:
            return self._random_point()

        scored.sort(key=lambda h: h.score, reverse=True)
        n_good = max(1, min(len(scored) - 1, int(math.ceil(self.gamma * len(scored)))))
        good = scored[:n_good]
        # Rejected trials join the "bad" set: they are regions we know produce
        # untradeable results, and without this the sampler happily keeps
        # proposing them because they never got a (bad) score.
        bad = scored[n_good:] + rejected

        return {d.path: self._suggest_dim(d, good, bad) for d in self.dims}

    # -- per-dimension modelling -------------------------------------------

    def _suggest_dim(self, d: Dimension, good: list[Observation],
                     bad: list[Observation]) -> Any:
        if d.type == "categorical":
            return self._suggest_categorical(d, good, bad)
        return self._suggest_numeric(d, good, bad)

    def _suggest_categorical(self, d: Dimension, good: list[Observation],
                             bad: list[Observation]) -> Any:
        k = len(d.choices)
        idx = {str(c): i for i, c in enumerate(d.choices)}

        def counts(obs: list[Observation]) -> np.ndarray:
            # +1 Laplace prior: an unexplored choice keeps a real chance of
            # being tried rather than being locked out by one bad early draw.
            c = np.ones(k)
            for o in obs:
                i = idx.get(str(o.params.get(d.path)))
                if i is not None:
                    c[i] += 1
            return c / c.sum()

        ratio = counts(good) / counts(bad)
        return d.choices[int(np.argmax(ratio))]

    def _values(self, d: Dimension, obs: list[Observation]) -> np.ndarray:
        vals = []
        for o in obs:
            v = o.params.get(d.path)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(math.log(v) if d.log and v > 0 else float(v))
        return np.asarray(vals, dtype=float)

    def _suggest_numeric(self, d: Dimension, good: list[Observation],
                         bad: list[Observation]) -> Any:
        lo, hi = (math.log(d.low), math.log(d.high)) if d.log else (d.low, d.high)
        g_vals, b_vals = self._values(d, good), self._values(d, bad)
        if g_vals.size == 0:
            return clamp(d, self.rng.uniform(d.low, d.high) if not d.log
                         else math.exp(self.rng.uniform(lo, hi)))

        # Bandwidth: Scott's rule, floored at a fraction of the range so a
        # cluster of identical good values doesn't collapse to a spike and
        # freeze the search on one point.
        def bandwidth(v: np.ndarray) -> float:
            span = hi - lo
            if v.size < 2:
                return span / 5.0
            bw = 1.06 * float(np.std(v)) * (v.size ** -0.2)
            return max(bw, span / 50.0)

        bw_g, bw_b = bandwidth(g_vals), bandwidth(b_vals) if b_vals.size else bandwidth(g_vals)

        # Draw candidates from l(x): pick a good observation, jitter it.
        picks = self.rng.integers(0, g_vals.size, self.n_candidates)
        cands = g_vals[picks] + self.rng.normal(0.0, bw_g, self.n_candidates)
        cands = np.clip(cands, lo, hi)

        log_l = _log_kde(cands, g_vals, bw_g, lo, hi)
        log_g = (_log_kde(cands, b_vals, bw_b, lo, hi) if b_vals.size
                 else np.zeros_like(cands))
        best = cands[int(np.argmax(log_l - log_g))]
        return clamp(d, math.exp(best) if d.log else best)


def _log_kde(x: np.ndarray, samples: np.ndarray, bw: float,
             lo: float, hi: float) -> np.ndarray:
    """log density of a Gaussian-mixture KDE, plus a wide uniform component.

    The uniform component (weight `_PRIOR_W`) is what keeps the sampler
    exploring: without it, density far from every observation underflows to
    zero and the ratio l/g becomes numerically meaningless out there.
    """
    _PRIOR_W = 0.15
    span = max(hi - lo, 1e-12)
    z = (x[:, None] - samples[None, :]) / bw
    comp = np.exp(-0.5 * z * z) / (bw * math.sqrt(2 * math.pi))
    dens = (1 - _PRIOR_W) * comp.mean(axis=1) + _PRIOR_W / span
    return np.log(np.maximum(dens, 1e-300))


SAMPLERS = {"random": RandomSampler, "tpe": TPESampler}


def make_sampler(name: str, dims: list[Dimension], seed: int) -> BaseSampler:
    cls = SAMPLERS.get(name)
    if cls is None:
        raise ValueError(f"unknown sampler {name!r}; valid: {', '.join(SAMPLERS)}")
    return cls(dims, seed=seed)


# ---- post-hoc stability ---------------------------------------------------

def stability_scores(dims: list[Dimension], history: list[Observation],
                     k: int = 5) -> list[float | None]:
    """For each scored trial, the mean score of its k nearest neighbours in
    normalized parameter space (docs/plan/14-optimizer.md §6.3).

    This is the plateau-vs-spike test, and it is the single most useful number
    the optimizer produces. A parameter set whose neighbours also score well
    sits on a broad plateau: the edge survives you being slightly wrong about
    the parameter, which you always are. A parameter set surrounded by bad
    neighbours is a spike — an artifact of this particular price path that
    will not survive contact with next month's data.

    Free: it reuses trials the study already ran, no extra backtests.

    Returns a list aligned with `history`; None for unscored trials.
    """
    idxs = [i for i, h in enumerate(history) if h.score is not None]
    out: list[float | None] = [None] * len(history)
    if len(idxs) < 2:
        return out

    # Min-max normalize each dimension so a period in [5,60] and a stop in
    # [0.5,5.0] contribute comparably to the distance.
    mat = np.zeros((len(idxs), len(dims)))
    for j, d in enumerate(dims):
        col = []
        for i in idxs:
            v = history[i].params.get(d.path)
            if d.type == "categorical":
                col.append(float(list(d.choices).index(v)) if v in d.choices else 0.0)
            else:
                fv = float(v)
                col.append(math.log(fv) if d.log and fv > 0 else fv)
        arr = np.asarray(col, dtype=float)
        span = arr.max() - arr.min()
        mat[:, j] = (arr - arr.min()) / span if span > 0 else 0.0

    scores = np.asarray([history[i].score for i in idxs], dtype=float)
    kk = min(k, len(idxs) - 1)
    for row in range(len(idxs)):
        d2 = ((mat - mat[row]) ** 2).sum(axis=1)
        d2[row] = np.inf
        nearest = np.argpartition(d2, kk - 1)[:kk]
        # Include the point itself: a trial IS part of its own neighbourhood,
        # and excluding it would rank a mediocre point in a great region above
        # a great point in a great region.
        out[idxs[row]] = float((scores[nearest].sum() + scores[row]) / (kk + 1))
    return out


def param_importance(dims: list[Dimension], history: list[Observation]) -> dict[str, float]:
    """Crude but honest variance-based importance: how much of the spread in
    scores is explained by each dimension alone.

    Bins each dimension into quartiles and measures between-bin variance of
    the mean score, normalized by total score variance (a one-way ANOVA eta^2
    per dimension). Values sum to no particular total — they are read
    relatively, as "which knobs mattered", which is all the UI claims.
    """
    scored = [h for h in history if h.score is not None]
    out: dict[str, float] = {d.path: 0.0 for d in dims}
    if len(scored) < 8:
        return out
    scores = np.asarray([h.score for h in scored], dtype=float)
    total_var = float(scores.var())
    if total_var <= 0:
        return out

    for d in dims:
        if d.type == "categorical":
            keys = [str(h.params.get(d.path)) for h in scored]
        else:
            vals = np.asarray([float(h.params.get(d.path)) for h in scored])
            edges = np.quantile(vals, [0.25, 0.5, 0.75])
            keys = [str(int(np.searchsorted(edges, v))) for v in vals]
        groups: dict[str, list[float]] = {}
        for key, s in zip(keys, scores):
            groups.setdefault(key, []).append(float(s))
        if len(groups) < 2:
            continue
        grand = float(scores.mean())
        between = sum(len(v) * (sum(v) / len(v) - grand) ** 2 for v in groups.values())
        out[d.path] = round(min(1.0, between / (total_var * len(scores))), 4)
    return out
