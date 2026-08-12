"""Search space for strategy optimization (docs/plan/14-optimizer.md §4).

This is the generalization of `backtest/sweep.py`'s `LeverSpec`: instead of a
lever with an explicit list of 3-5 values (fine for a 2-D heatmap you read by
eye), a Dimension describes a CONTINUOUS or discrete RANGE that a sampler
draws from — which is what lets an optimizer search 6-8 parameters jointly
and model how they interact, rather than taking a Cartesian product that
explodes.

Dotted paths, and the rules about which ones are legal, are shared with
sweep.py on purpose — `get_by_path`/`set_by_path` are imported from there
rather than reimplemented, so there is exactly one path-poking implementation
in the codebase.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, replace
from typing import Any, Literal

from ..backtest.sweep import get_by_path, set_by_path
from ..indicators.registry import INDICATORS
from ..strategy.schema import StrategyDocument

# Beyond this, the trial budget needed to say anything honest about the space
# exceeds what a laptop will run in an evening -- and the "best" point found
# would be noise regardless. This is an anti-overfitting limit, not a
# performance one.
MAX_DIMS = 8

# Same prohibition sweep.py enforces, for the same reason: all trials in a
# study share ONE candle fetch, so a trial requesting a symbol the fetch
# didn't cover would silently score 0 trades. "Compare across symbols" is a
# different feature from "tune this strategy's parameters".
_FORBIDDEN_PATH_PREFIXES = ("universe", "meta", "schema_version")

# Unlike sweep.py, `timeframe.bar` IS allowed here (see the study planner:
# a study containing it clamps its fold plan to the SHORTEST-history bar in
# the choices). Everything else under timeframe is fixed or not tunable.
_ALLOWED_TIMEFRAME_PATHS = ("timeframe.bar",)

DimType = Literal["int", "float", "categorical"]


@dataclass(frozen=True)
class Dimension:
    """One tunable axis: a dotted path into the strategy document plus the
    range a sampler may draw from."""

    path: str
    type: DimType
    low: float | None = None
    high: float | None = None
    step: float | None = None       # int/float only; None = continuous (float)
    log: bool = False               # sample log-uniformly (float only)
    choices: tuple[Any, ...] = ()   # categorical only
    # Human label, resolved against the strategy document at study-creation
    # time and persisted with the study (see `label_for`). Persisted rather
    # than derived on read because a study outlives the document it was run
    # against -- the strategy can be edited or its indicators reordered, and
    # a label resolved later would then be wrong rather than merely terse.
    label: str = ""

    @property
    def name(self) -> str:
        """Short human label for charts/tables."""
        if self.label:
            return self.label
        parts = self.path.split(".")
        return ".".join(parts[-2:]) if len(parts) > 1 else self.path

    def to_json(self) -> dict:
        out: dict[str, Any] = {"path": self.path, "type": self.type,
                               "label": self.label or self.name}
        if self.type == "categorical":
            out["choices"] = list(self.choices)
        else:
            out.update(low=self.low, high=self.high, log=self.log)
            if self.step is not None:
                out["step"] = self.step
        return out


def _fail(path: str, msg: str) -> None:
    raise ValueError(f"dimension {path!r}: {msg}")


def parse_dimension(spec: dict) -> Dimension:
    if not isinstance(spec, dict):
        raise ValueError("each dimension must be an object")
    path = spec.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("dimension needs a 'path'")

    head = path.split(".")[0]
    if head in _FORBIDDEN_PATH_PREFIXES:
        _fail(path, f"optimizing {head!r} is not supported — every trial in a study must share "
                    "the same universe (they share one candle fetch). Optimize indicator, "
                    "exit, sizing or risk parameters instead.")
    if head == "timeframe" and path not in _ALLOWED_TIMEFRAME_PATHS:
        _fail(path, f"the only optimizable timeframe path is {_ALLOWED_TIMEFRAME_PATHS[0]!r}")

    dtype = spec.get("type")
    if dtype not in ("int", "float", "categorical"):
        _fail(path, f"type must be int, float or categorical (got {dtype!r})")

    if dtype == "categorical":
        choices = spec.get("choices")
        if not isinstance(choices, list) or len(choices) < 2:
            _fail(path, "categorical needs a 'choices' list with at least 2 entries")
        if len(set(map(str, choices))) != len(choices):
            _fail(path, "duplicate choices")
        return Dimension(path=path, type="categorical", choices=tuple(choices),
                         label=str(spec.get("label") or ""))

    low, high = spec.get("low"), spec.get("high")
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        _fail(path, "numeric dimensions need numeric 'low' and 'high'")
    if not (low < high):
        _fail(path, f"low ({low}) must be < high ({high})")
    log = bool(spec.get("log", False))
    if log and low <= 0:
        _fail(path, "log sampling needs low > 0")

    step = spec.get("step")
    if dtype == "int":
        step = 1 if step is None else step
        if step <= 0 or step != int(step):
            _fail(path, "int step must be a positive whole number")
        step = int(step)
        if log:
            _fail(path, "log sampling is only supported for float dimensions")
        if int(high) - int(low) < 1:
            _fail(path, "int range must contain at least 2 values")
    elif step is not None:
        if step <= 0:
            _fail(path, "step must be positive")
        if log:
            _fail(path, "step and log are mutually exclusive")

    return Dimension(path=path, type=dtype, low=float(low), high=float(high),
                     step=step, log=log, label=str(spec.get("label") or ""))


def parse_space(raw: Any) -> list[Dimension]:
    """Parse+validate the search space. Does NOT check the paths resolve — that
    needs the strategy document, see `validate_against_document`."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("space must be a non-empty list of dimensions")
    if len(raw) > MAX_DIMS:
        raise ValueError(
            f"at most {MAX_DIMS} dimensions are supported, got {len(raw)} — a larger space "
            "needs more trials than is practical here, and its best point would be noise")
    dims = [parse_dimension(s) for s in raw]
    paths = [d.path for d in dims]
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate dimension paths")
    return dims


def clamp(dim: Dimension, value: Any) -> Any:
    """Snap a raw sampled value onto the dimension's grid/bounds. Samplers
    produce values in the dimension's own units; this is the one place that
    rounding and bound-clipping happens, so every sampler stays simple."""
    if dim.type == "categorical":
        return value
    v = float(value)
    v = min(max(v, dim.low), dim.high)
    if dim.type == "int":
        step = int(dim.step or 1)
        v = dim.low + round((v - dim.low) / step) * step
        return int(min(max(v, math.ceil(dim.low)), math.floor(dim.high)))
    if dim.step:
        v = dim.low + round((v - dim.low) / dim.step) * dim.step
        v = min(max(v, dim.low), dim.high)
    return round(float(v), 10)


def apply_params(base_data: dict, params: dict[str, Any]) -> dict:
    """Return a NEW document dict with `params` ({path: value}) poked in.
    Does not mutate the input."""
    out = copy.deepcopy(base_data)
    for path, value in params.items():
        set_by_path(out, path, value)
    return out


def label_for(path: str, doc: StrategyDocument) -> str:
    """A human label for a dotted path, resolved against the document.

    Exists because the naive "last two segments" fallback renders every
    indicator parameter as `params.period` — a strategy with three EMAs shows
    three identical columns, which is unreadable in the champion panel and the
    leaderboard. Resolving `indicators.2.params.period` to `ema200.period`
    uses the id the user already chose.
    """
    parts = path.split(".")
    if parts[0] == "indicators" and len(parts) >= 4 and parts[1].isdigit():
        i = int(parts[1])
        if 0 <= i < len(doc.indicators):
            ind = doc.indicators[i]
            return f"{ind.id}.{parts[-1]}"
    friendly = {
        "exit.stop_loss.value": "stop loss",
        "exit.take_profit.value": "take profit",
        "exit.trailing_stop.value": "trailing stop",
        "exit.time_stop.bars": "time stop (bars)",
        "entry.cooldown_bars": "entry cooldown",
        "entry.max_entries_per_day": "max entries/day",
        "sizing.value": f"sizing ({doc.sizing.type})",
        "sizing.max_position_notional": "max position $",
        "risk.max_open_positions": "max open positions",
        "timeframe.bar": "bar size",
    }
    if path in friendly:
        return friendly[path]
    return ".".join(parts[-2:]) if len(parts) > 1 else path


def with_labels(dims: list[Dimension], doc: StrategyDocument) -> list[Dimension]:
    """Attach resolved labels, preserving any the caller already supplied."""
    return [d if d.label else replace(d, label=label_for(d.path, doc)) for d in dims]


def validate_against_document(dims: list[Dimension], doc: StrategyDocument) -> None:
    """Fail fast at study-creation time rather than at trial 300.

    Checks each path actually resolves in the base document, and that the
    dimension's extremes produce a schema-valid document. A path that only
    breaks at one end of its range (e.g. a stop value of 0) is caught here
    instead of showing up as a wall of rejected trials later.
    """
    base = doc.model_dump(mode="json", by_alias=True)
    for dim in dims:
        try:
            get_by_path(base, dim.path)
        except (KeyError, IndexError, TypeError, ValueError):
            _fail(dim.path, "no such field in this strategy document — check the path against "
                            "the strategy JSON (the Optimize page shows it)")
        probes = (list(dim.choices) if dim.type == "categorical"
                  else [clamp(dim, dim.low), clamp(dim, dim.high)])
        for probe in probes:
            candidate = apply_params(base, {dim.path: probe})
            try:
                StrategyDocument.model_validate(candidate)
            except Exception as e:  # noqa: BLE001 — pydantic raises a rich union of errors
                _fail(dim.path, f"value {probe!r} produces an invalid strategy: {e}")


# ---- suggested space ------------------------------------------------------
#
# Hand-typing dotted paths is a terrible first-run experience and invites
# typos that only surface as validation errors. The Optimize page calls
# suggest_space() to prefill an editable table of the parameters this
# particular strategy actually has.

# Fractional widths around the current value. Deliberately generous (the
# point of optimizing is to escape the neighborhood you hand-picked) but
# always intersected with the registry's hard min/max so a suggestion can
# never propose an invalid value.
_WIDTH_DOWN, _WIDTH_UP = 0.5, 2.0


def _num_range(current: float, lo: float, hi: float) -> tuple[float, float]:
    return max(lo, current * _WIDTH_DOWN), min(hi, current * _WIDTH_UP)


def suggest_space(doc: StrategyDocument) -> list[dict]:
    """Derive a plausible starting search space from the strategy itself.

    Indicator params get their ranges from the SAME registry metadata that
    validates them (`ParamSpec.min/max/step`), so a suggestion can never fall
    outside what the indicator accepts. Exit/sizing/entry parameters have no
    registry, so their bounds are hardcoded here to the schema's own limits.

    Returns raw dicts (not Dimensions) because this feeds a UI table the user
    edits before anything is parsed.
    """
    out: list[dict] = []

    for i, ind in enumerate(doc.indicators):
        spec = INDICATORS.get(ind.type)
        if spec is None:
            continue
        for pspec in spec.params:
            if pspec.name not in ind.params:
                continue
            current = float(ind.params[pspec.name])
            lo, hi = _num_range(current, pspec.min, pspec.max)
            if hi - lo <= 0:
                continue
            is_int = float(pspec.step).is_integer() and float(current).is_integer()
            out.append({
                "path": f"indicators.{i}.params.{pspec.name}",
                "type": "int" if is_int else "float",
                "low": round(lo) if is_int else round(lo, 4),
                "high": round(hi) if is_int else round(hi, 4),
                "step": int(pspec.step) if is_int else None,
                "log": False,
                "label": f"{ind.id}.{pspec.name}",
                "current": current,
            })

    def _stop(attr: str, label: str) -> None:
        spec = getattr(doc.exit, attr, None)
        if spec is None:
            return
        current = float(spec.value)
        # percent stops live on a different scale from ATR multiples; both are
        # searched log-uniformly because what matters is the ratio (1%->2% is
        # the same kind of change as 4%->8%), not the absolute difference.
        lo, hi = _num_range(current, 0.05, 100.0)
        out.append({"path": f"exit.{attr}.value", "type": "float",
                    "low": round(lo, 4), "high": round(hi, 4), "step": None, "log": True,
                    "label": label, "current": current})

    _stop("stop_loss", "stop loss")
    _stop("take_profit", "take profit")
    _stop("trailing_stop", "trailing stop")

    if doc.exit.time_stop is not None:
        current = float(doc.exit.time_stop.bars)
        lo, hi = _num_range(current, 1, 10_000)
        out.append({"path": "exit.time_stop.bars", "type": "int",
                    "low": max(1, round(lo)), "high": round(hi), "step": 1, "log": False,
                    "label": "time stop (bars)", "current": current})

    if doc.entry.cooldown_bars > 0:
        current = float(doc.entry.cooldown_bars)
        lo, hi = _num_range(current, 0, 500)
        out.append({"path": "entry.cooldown_bars", "type": "int",
                    "low": max(0, round(lo)), "high": round(hi), "step": 1, "log": False,
                    "label": "entry cooldown", "current": current})

    current = float(doc.sizing.value)
    lo, hi = _num_range(current, 0.01, 1_000_000)
    out.append({"path": "sizing.value", "type": "float",
                "low": round(lo, 4), "high": round(hi, 4), "step": None, "log": True,
                "label": f"sizing ({doc.sizing.type})", "current": current})

    # Drop anything degenerate (current value sitting on a registry bound can
    # collapse a range to a point) and respect the dimension cap: indicator
    # params come first, so a strategy with many indicators keeps those.
    out = [d for d in out if d["type"] == "categorical" or d["high"] > d["low"]]
    return out[:MAX_DIMS]
