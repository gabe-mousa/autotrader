"""Parameter sweeps (docs/plan/09-ui-ux.md §4 / 11-roadmap.md Phase 7):
generate every combination of 1-2 "levers" over a strategy document by
poking values into a dotted JSON path, re-validating + linting each result
the SAME way POST /api/backtests already does (never skip lint), and letting
the caller (app/api/backtests.py's sweep endpoints) run/persist each valid
combination.

Grid spec shape (up to 2 levers):
    {"lever_a": {"path": "indicators.0.params.period", "values": [10, 14, 20]},
     "lever_b": {"path": "timeframe.bar", "values": ["5m", "15m", "1h"]}}

`path` is a dotted path into `StrategyDocument.model_dump(mode="json",
by_alias=True)` — list indices are plain ints in the path (e.g.
"indicators.0.params.period")."""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass
from typing import Any

MAX_LEVERS = 2

# app/api/backtests.py's sweep runner fetches candles per DISTINCT
# universe+timeframe across the combos, not once for the whole sweep, so
# sweeping `timeframe.bar` is supported: each bar size gets its own fetch
# (with its own warm-up history).
#
# `universe` is still rejected. A combo requesting a symbol the shared fetch
# didn't cover would just get an empty candles lookup and run_backtest()
# would report 0 trades for it (skipped, not corrupted -- see
# run_backtest's `if df is None or df.empty: continue`), a confusing silent
# failure; and "compare the same strategy across different symbols" is a
# different feature from "tune this strategy's levers", with a results table
# that wouldn't mean the same thing.
_FORBIDDEN_PATH_PREFIXES = ("universe",)

# Everything under `timeframe` except `bar` is either fixed (evaluate_on has
# one legal value) or not a tunable parameter.
_ALLOWED_TIMEFRAME_PATHS = ("timeframe.bar",)


@dataclass
class LeverSpec:
    name: str
    path: str
    values: list[Any]  # numbers for most levers; bar strings for timeframe.bar


def parse_grid(grid: dict) -> list[LeverSpec]:
    if not isinstance(grid, dict) or not grid:
        raise ValueError("grid must be a non-empty object of {lever_name: {path, values}}")
    if len(grid) > MAX_LEVERS:
        raise ValueError(f"at most {MAX_LEVERS} levers are supported, got {len(grid)}")
    levers = []
    for name, spec in grid.items():
        if not isinstance(spec, dict) or "path" not in spec or "values" not in spec:
            raise ValueError(f"lever {name!r} needs 'path' and 'values'")
        values = spec["values"]
        if not isinstance(values, list) or not values:
            raise ValueError(f"lever {name!r}: 'values' must be a non-empty list")
        path = spec["path"]
        if path.split(".")[0] in _FORBIDDEN_PATH_PREFIXES:
            raise ValueError(
                f"lever {name!r}: sweeping '{path}' is not supported — all combos in a "
                "sweep must share the same universe; sweep timeframe.bar or "
                "indicator/exit/sizing params instead")
        if path.split(".")[0] == "timeframe" and path not in _ALLOWED_TIMEFRAME_PATHS:
            raise ValueError(
                f"lever {name!r}: sweeping '{path}' is not supported — the only "
                f"sweepable timeframe path is {_ALLOWED_TIMEFRAME_PATHS[0]!r}")
        levers.append(LeverSpec(name=name, path=path, values=list(values)))
    return levers


def get_by_path(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def set_by_path(data: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def generate_combos(levers: list[LeverSpec]) -> list[dict[str, Any]]:
    """Cartesian product across all levers -> list of {lever_name: value}."""
    names = [lv.name for lv in levers]
    out = []
    for values in itertools.product(*(lv.values for lv in levers)):
        out.append(dict(zip(names, values)))
    return out


def apply_combo(doc_data: dict, levers: list[LeverSpec], combo: dict[str, Any]) -> dict:
    """Return a NEW doc dict with `combo`'s values poked into `doc_data` at
    each lever's dotted path — does not mutate the input."""
    out = copy.deepcopy(doc_data)
    for lv in levers:
        set_by_path(out, lv.path, combo[lv.name])
    return out
