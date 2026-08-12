"""StrategyEvaluator — the ONE rule-evaluation core shared by backtest, paper,
and live (docs/plan/05). Indicators are precomputed (vectorized); expressions
are evaluated per bar index with no lookahead: bar i may read series values at
i and earlier only."""

from __future__ import annotations

import math

import pandas as pd

from ..indicators.registry import compute_indicator
from .schema import (AllExpr, AnyExpr, Comparison, Expr, IndOffsetOperand, IndOperand,
                     NotExpr, PriceOperand, StrategyDocument, ValueOperand)


class CompiledSymbol:
    """Precomputed indicator series for one symbol's candle frame."""

    def __init__(self, doc: StrategyDocument, df: pd.DataFrame):
        self.df = df
        self.series: dict[tuple[str, str], pd.Series] = {}
        for ind in doc.indicators:
            out = compute_indicator(df, ind.type, ind.params, ind.source)
            for col in out.columns:
                self.series[(ind.id, col)] = out[col]

    # ---- operand resolution -------------------------------------------
    def _series_for(self, ind_id: str, field: str | None) -> pd.Series:
        if field is None:
            # single-output indicators: the only key; lint guarantees uniqueness
            keys = [k for k in self.series if k[0] == ind_id]
            return self.series[keys[0]]
        return self.series[(ind_id, field)]

    def _offset_at(self, op, i: int) -> float:
        """The indicator-valued part of a series operand: `off_ind * off_mul`,
        or 0.0 when unused. Lets a rule express a level a moving distance from
        a series (`vwap - 1.5*ATR`), which a constant `add` cannot. NaN
        propagates so the comparison is simply not met during warm-up."""
        off = getattr(op, "off_ind", None)
        if not off:
            return 0.0
        return float(self._series_for(off, getattr(op, "off_field", None)).iloc[i]) * op.off_mul

    def operand_at(self, op, i: int) -> float:
        """Series operands resolve to `raw * mul + add + off_ind * off_mul`
        (all identity by default) so a comparison can be made against a
        MULTIPLE of another series — e.g. `volume > vol_sma * 1.5` — or against
        a volatility-relative level — e.g. `close < vwap - 1.5*ATR`. NaN
        propagates through the scaling untouched, so warm-up handling
        downstream is unchanged."""
        if isinstance(op, ValueOperand):
            return op.value
        if isinstance(op, PriceOperand):
            return float(self.df[op.price].iloc[i]) * op.mul + op.add + self._offset_at(op, i)
        if isinstance(op, IndOperand):
            return (float(self._series_for(op.ind, op.field).iloc[i]) * op.mul + op.add
                    + self._offset_at(op, i))
        if isinstance(op, IndOffsetOperand):
            j = i - op.ind_offset.bars_ago
            if j < 0:
                return math.nan
            raw = float(self._series_for(op.ind_offset.ind, op.ind_offset.field).iloc[j])
            # the offset indicator is read at the SAME shifted bar, so the whole
            # expression describes one consistent point in time
            return raw * op.mul + op.add + self._offset_at(op, j)
        raise TypeError(f"unknown operand {op!r}")

    # ---- expression evaluation ----------------------------------------
    def eval_expr(self, expr: Expr, i: int) -> bool:
        if isinstance(expr, AllExpr):
            return all(self.eval_expr(e, i) for e in expr.all)
        if isinstance(expr, AnyExpr):
            return any(self.eval_expr(e, i) for e in expr.any)
        if isinstance(expr, NotExpr):
            return not self.eval_expr(expr.not_, i)
        return self._eval_cmp(expr, i)

    def _eval_cmp(self, c: Comparison, i: int) -> bool:
        if c.op in ("rising", "falling"):
            j = i - (c.bars or 1)
            if j < 0:
                return False
            now, then = self.operand_at(c.left, i), self.operand_at(c.left, j)
            if math.isnan(now) or math.isnan(then):
                return False
            return now > then if c.op == "rising" else now < then

        left = self.operand_at(c.left, i)
        right = self.operand_at(c.right, i)
        if math.isnan(left) or math.isnan(right):
            return False  # warm-up: conditions are simply not met yet

        if c.op in ("crosses_above", "crosses_below"):
            if i == 0:
                return False
            pl, pr = self.operand_at(c.left, i - 1), self.operand_at(c.right, i - 1)
            if math.isnan(pl) or math.isnan(pr):
                return False
            if c.op == "crosses_above":
                return pl <= pr and left > right
            return pl >= pr and left < right

        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
            "eq": left == right,
        }[c.op]


class StrategyEvaluator:
    def __init__(self, doc: StrategyDocument):
        self.doc = doc
        self._compiled: dict[str, CompiledSymbol] = {}

    def compile_symbol(self, symbol: str, df: pd.DataFrame) -> None:
        self._compiled[symbol] = CompiledSymbol(self.doc, df)

    def sym(self, symbol: str) -> CompiledSymbol:
        return self._compiled[symbol]

    def entry_signal(self, symbol: str, i: int, side: str = "long") -> bool:
        """Entry rule for one side. Defaults to long so every existing caller
        (and every long-only document) behaves exactly as before."""
        block = self.doc.entry.long if side == "long" else self.doc.entry.short
        return block is not None and self._compiled[symbol].eval_expr(block.when, i)

    def exit_signal(self, symbol: str, i: int, side: str = "long") -> bool:
        """Exit rule for the open side. A short uses `exit.signal_short` when
        present, otherwise the shared `exit.signal` — so an asymmetric exit is
        possible without forcing every document to specify both."""
        block = self.doc.exit.signal
        if side == "short" and self.doc.exit.signal_short is not None:
            block = self.doc.exit.signal_short
        return block is not None and self._compiled[symbol].eval_expr(block.when, i)

    def atr_value(self, symbol: str, ind_id: str, i: int) -> float:
        return float(self._compiled[symbol]._series_for(ind_id, None).iloc[i])
