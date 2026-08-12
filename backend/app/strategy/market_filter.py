"""Shared evaluation path for `market_filter` — the regime gate (plan/15 §C1).

This lives in `strategy/` rather than in either runtime because BOTH the
backtest engine and the live/paper runner must evaluate the gate through the
SAME object. It used to be a private class inside `backtest/engine.py`
(`_MarketFilterEval`) and the runner ignored `market_filter` entirely, which is
audit item A4: every live entry ran with the gate wide open. A second copy of
the evaluation logic is exactly how that class of parity bug comes back, so
there is deliberately only one.

Note what is NOT here: the `applies_to` routing, the fail-closed-on-missing-data
rule and the `searchsorted` look-ahead guard live in each caller
(`engine.market_ok`/`market_exit`, `runner._market_ok`/`_market_exit`) because
they need that caller's index/timestamp types. Those are mirrored line-for-line
and each side carries a comment saying so.
"""

from __future__ import annotations

import pandas as pd

from .evaluator import CompiledSymbol


class MarketFilterEval:
    """Evaluates a MarketFilter's expression on the reference symbol's frame.

    Reuses CompiledSymbol so the reference symbol's indicators and comparison
    semantics are IDENTICAL to a traded symbol's — a regime gate must not have
    its own subtly-different evaluation path."""

    def __init__(self, mf, df: pd.DataFrame):
        class _Shim:                     # CompiledSymbol only needs .indicators
            def __init__(self, inds):
                self.indicators = inds

        self._compiled = CompiledSymbol(_Shim(mf.indicators), df)
        self._when = mf.when
        self._exit_when = mf.exit_when

    def ok(self, i: int) -> bool:
        return self._compiled.eval_expr(self._when, i)

    def exit_now(self, i: int) -> bool:
        """True when the gate's separate EXIT condition fires. Kept distinct
        from `not ok()` so a filter can carry hysteresis: enter above one
        threshold, leave below a lower one."""
        if self._exit_when is None:
            return False
        return self._compiled.eval_expr(self._exit_when, i)
