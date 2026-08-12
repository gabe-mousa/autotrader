"""RunRiskTracker — implements execution.types.RiskContext by reading a
StrategyRunner's own in-memory state. Kept separate from the runner class so
its (small, pure) logic is independently testable."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import StrategyRunner


class RunRiskTracker:
    def __init__(self, runner: "StrategyRunner"):
        self._runner = runner

    @property
    def run_id(self) -> str:
        return self._runner.run_id

    @property
    def mode(self) -> str:
        return self._runner.mode

    @property
    def max_open_positions(self) -> int:
        return self._runner.doc.risk.max_open_positions

    @property
    def max_daily_orders(self) -> int:
        return self._runner.doc.risk.max_daily_orders

    @property
    def max_daily_loss(self) -> float:
        return self._runner.doc.risk.max_daily_loss

    def open_position_count(self) -> int:
        return len(self._runner._positions)

    def orders_today_count(self) -> int:
        return self._runner._orders_today

    def daily_pnl(self) -> float:
        return self._runner._realized_pnl_today + self._unrealized_pnl()

    def has_open_entry_order(self, symbol: str) -> bool:
        return symbol in self._runner._pending_entries

    # ---- short-selling context (plan/15 §B4) -------------------------------

    @property
    def short_enabled(self) -> bool:
        """Both the explicit opt-in AND a short rule must be present."""
        risk = self._runner.doc.risk
        return bool(risk.short.enabled and self._runner.doc.entry.short)

    @property
    def account_type(self) -> str:
        """MARGIN | CASH. A cash account can never short — the gateway rejects
        `sell_short` unless this reads MARGIN. Paper runs simulate a margin
        account; live reads it from the broker's account snapshot."""
        if self._runner.mode == "paper":
            return "MARGIN"
        return str(getattr(self._runner, "_account_type", "") or "").upper()

    @property
    def hard_to_borrow(self) -> frozenset[str]:
        return frozenset(self._runner.doc.risk.short.hard_to_borrow)

    @property
    def max_short_notional(self) -> float:
        return self._runner.doc.risk.short.max_short_notional

    def short_qty(self, symbol: str) -> float:
        """Open short size for `symbol` (positive number, 0 if not short).
        The gateway clamps covers against this so a cover can never buy through
        zero into an unintended long.

        Reads `_LivePosition.side`, NOT the sign of `qty`. `_LivePosition`
        follows plan/15 decision #1 — positive quantity plus an explicit side —
        so the previous `-pos.qty if pos.qty < 0` test could never be true and
        this returned 0.0 for every symbol including genuine shorts. That made
        the gateway's `cover_exceeds_short` clamp permanently unreachable in a
        real run: it is a safety check, so silently passing was the worst
        possible failure mode."""
        pos = self._runner._positions.get(symbol)
        if pos is None or getattr(pos, "side", "long") != "short":
            return 0.0
        return abs(pos.qty)

    # ---- options context (plan/16 §11.2) -----------------------------------
    # The gateway reads these with DENYING defaults, so what matters here is
    # that they are only permissive when the document genuinely asks for
    # options.

    @property
    def options_enabled(self) -> bool:
        expr = getattr(self._runner.doc, "expression", None)
        return expr is not None and expr.instrument == "options"

    @property
    def max_contracts_per_position(self) -> int:
        return self._runner.doc.risk.options.max_contracts_per_position

    @property
    def max_premium_at_risk_remaining(self) -> float:
        """Headroom on total open option debit.

        Computed from the positions actually held rather than tracked
        incrementally: a counter would drift out of step with reality after a
        partial fill or a crash recovery, and this is a safety limit."""
        opt = getattr(self._runner, "_opt", None)
        cap = self._runner.doc.risk.options.max_premium_at_risk
        if opt is None:
            return 0.0
        return max(cap - opt.premium_at_risk(self._runner._positions), 0.0)

    def contract_qty(self, symbol: str) -> float:
        """Contracts held for an OCC symbol (0 if none).

        The gateway clamps closes against this so a close can never sell
        through zero into a NAKED SHORT OPTION. `_positions` is keyed by the
        UNDERLYING, so this searches by contract symbol — matching on the key
        would return 0 for every contract and leave the clamp unreachable,
        which is exactly the bug `short_qty` had before it was fixed."""
        for pos in self._runner._positions.values():
            if getattr(pos, "contract_symbol", None) == symbol:
                return abs(pos.qty)
        return 0.0

    def portfolio_delta(self) -> float:
        opt = getattr(self._runner, "_opt", None)
        if opt is None:
            return 0.0
        return opt.portfolio_greeks(self._runner._positions)["delta"]

    def portfolio_vega(self) -> float:
        opt = getattr(self._runner, "_opt", None)
        if opt is None:
            return 0.0
        return opt.portfolio_greeks(self._runner._positions)["vega"]

    def _unrealized_pnl(self) -> float:
        """Mark-to-market on open positions. Direction comes from the position's
        `side` field (positive quantities, per plan/15 decision #1), so a SHORT
        gains when the price FALLS. The previous signed-qty formula gave a short
        the sign of a long, i.e. reported a winning short as a loss — which feeds
        `daily_pnl()` and therefore the gateway's `max_daily_loss` entry block."""
        total = 0.0
        for symbol, pos in self._runner._positions.items():
            if getattr(pos, "is_option", False):
                # An option marks against its CONTRACT's premium, not the
                # underlying's price, and moves by the multiplier. Using the
                # underlying's last trade here would report a number that has
                # nothing to do with the position's value — and daily_pnl()
                # feeds the gateway's max_daily_loss entry block.
                opt = getattr(self._runner, "_opt", None)
                q = opt.quote(pos.contract_symbol) if opt is not None else None
                if q is None:
                    continue
                total += (q.mid - pos.avg_price) * abs(pos.qty) * pos.multiplier
                continue
            last = self._runner._last_price.get(symbol)
            if last is None:
                continue
            move = last - pos.avg_price
            if getattr(pos, "side", "long") == "short":
                move = -move
            total += move * abs(pos.qty)
        return total
