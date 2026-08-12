"""Semantic lint for strategy documents — cross-field checks Pydantic can't
express. Returns a list of human-readable problems (empty = clean). Used by
save/validate API, the editor UI, and the AI repair loop."""

from __future__ import annotations

from ..indicators.registry import INDICATORS
from .live_support import blocking_gaps
from .schema import (AllExpr, AnyExpr, Comparison, Expr, IndicatorDef, IndOffsetOperand,
                     IndOperand, NotExpr, PriceOperand, StrategyDocument, ValueOperand)
from .timeframe import BAR_SECONDS

MAX_DEPTH = 5
MAX_COMPARISONS = 40


def lint(doc: StrategyDocument) -> list[str]:
    problems: list[str] = []
    ind_map = {i.id: i for i in doc.indicators}

    _check_indicator_defs(doc, problems)

    for label, block in (("entry.long", doc.entry.long), ("entry.short", doc.entry.short),
                         ("exit.signal", doc.exit.signal),
                         ("exit.signal_short", doc.exit.signal_short)):
        if block is not None:
            stats = _walk(block.when, ind_map, problems, label, depth=1)
            if stats["max_depth"] > MAX_DEPTH:
                problems.append(f"{label}: expression nesting exceeds {MAX_DEPTH} levels")
            if stats["comparisons"] > MAX_COMPARISONS:
                problems.append(f"{label}: more than {MAX_COMPARISONS} comparisons")

    if doc.entry.long is None and doc.entry.short is None:
        problems.append("entry: at least one of entry.long / entry.short is required")

    _check_live_support(doc, problems)
    _check_short(doc, problems)
    _check_market_filter(doc, problems)
    _check_selection(doc, ind_map, problems)
    _check_exits(doc, ind_map, problems)
    _check_sizing(doc, problems)
    _check_timeframe(doc, problems)
    _check_options(doc, problems)
    return problems


def _check_options(doc: StrategyDocument, problems: list[str]) -> None:
    """Options checks (docs/plan/16-options-trading.md §7.4).

    Returns immediately for a shares document, which is every pre-existing
    strategy — a new spurious lint error would block edits to a strategy that
    is currently running.

    The strictest rules here enforce plan/16 decision D3: v1 trades LONG
    premium only. That is not a limitation to route around, it is what makes
    the position defined-risk, which in turn is what makes the whole live
    safety story hold together when an underlying-based stop cannot rest at the
    broker."""
    expr = getattr(doc, "expression", None)
    if expr is None or expr.instrument != "options":
        return

    if expr.long is None and expr.short is None:
        problems.append(
            "expression: instrument is 'options' but neither expression.long nor "
            "expression.short is set — no signal can be expressed as a contract")

    # A signal with no expression silently never trades, which is the single
    # most confusing possible failure: the strategy looks configured and does
    # nothing.
    if doc.entry.long is not None and expr.long is None:
        problems.append(
            "expression.long: entry.long fires but there is no options expression "
            "for it — those signals would be dropped silently")
    if doc.entry.short is not None and expr.short is None:
        problems.append(
            "expression.short: entry.short fires but there is no options expression "
            "for it — those signals would be dropped silently")

    for label, side in (("expression.long", expr.long), ("expression.short", expr.short)):
        if side is None:
            continue
        e = side.expiry
        if e.dte_min > e.dte_max:
            problems.append(f"{label}.expiry: dte_min {e.dte_min} > dte_max {e.dte_max}")
        elif not (e.dte_min <= e.dte_target <= e.dte_max):
            problems.append(
                f"{label}.expiry: dte_target {e.dte_target} is outside "
                f"[{e.dte_min}, {e.dte_max}]")
        if not e.allow:
            problems.append(f"{label}.expiry.allow: at least one expiration type "
                            "must be allowed")
        if side.structure == "vertical_debit" and side.wing_width is None:
            problems.append(
                f"{label}: structure 'vertical_debit' requires wing_width "
                "(the distance to the short strike)")
        if side.strike.by == "atr_multiple" and not side.strike.atr_indicator:
            problems.append(
                f"{label}.strike: by='atr_multiple' requires atr_indicator")
        elif side.strike.by == "atr_multiple":
            if side.strike.atr_indicator not in {i.id for i in doc.indicators}:
                problems.append(
                    f"{label}.strike: atr_indicator "
                    f"{side.strike.atr_indicator!r} is not a declared indicator")
        if side.strike.by == "delta" and not (0 < side.strike.target < 1):
            problems.append(
                f"{label}.strike: delta target {side.strike.target} must be "
                "between 0 and 1 (0.35 = a 35-delta contract)")

        # The position must be closed before it can be opened.
        if expr.exit.close_at_dte >= e.dte_min:
            problems.append(
                f"{label}: close_at_dte {expr.exit.close_at_dte} >= dte_min "
                f"{e.dte_min} — every contract selected would be closed "
                "immediately on the bar it opened")

    ex = expr.exit
    # ATR is a distance in UNDERLYING price and is meaningless against premium.
    if ex.stop_basis == "premium":
        for label, spec in (("exit.stop_loss", doc.exit.stop_loss),
                            ("exit.take_profit", doc.exit.take_profit),
                            ("exit.trailing_stop", doc.exit.trailing_stop)):
            if spec is not None and spec.type == "atr_multiple":
                problems.append(
                    f"{label}: type 'atr_multiple' cannot be used with "
                    "expression.exit.stop_basis='premium' — ATR measures the "
                    "underlying's price range, not option premium")

    # ---- warnings -------------------------------------------------------
    # The convention is `is_warning()`: a problem ENDING in "(warning)" is
    # advisory and does not block saving. Getting that wrong makes an advisory
    # into a hard save failure, which is how this was found.
    p = expr.pricing
    if p.vrp_mult < 1.0:
        problems.append(
            f"expression.pricing.vrp_mult={p.vrp_mult} is below 1.0. Implied "
            "volatility has exceeded subsequent realized volatility on index "
            "products in roughly 85% of months since 1990, so a multiplier under "
            "1.0 prices options CHEAPER than they have historically traded and "
            "will manufacture profit that does not exist (warning)")
    if p.fill_aggression < 0.5:
        problems.append(
            f"expression.pricing.fill_aggression={p.fill_aggression} assumes fills "
            "near the mid that a real order may never get. Option spreads are "
            "wide; 1.0 (cross the full spread) is the honest default (warning)")
    if expr.liquidity.min_open_interest == 0:
        problems.append(
            "expression.liquidity.min_open_interest=0 permits contracts with no "
            "open interest — these backtest fine and cannot be traded (warning)")
    if ex.stop_basis == "underlying" and ex.max_loss_pct_of_premium is None:
        problems.append(
            "with stop_basis='underlying' and max_loss_pct_of_premium disabled, NO "
            "protective order can rest at the broker — an underlying-triggered "
            "stop is monitored in-process only. The position is still capped at "
            "the premium paid, but a crash leaves it unmanaged (warning)")
    if doc.risk.short.enabled:
        problems.append(
            "risk.short.enabled is set on an OPTIONS document. A 'short' signal "
            "here BUYS PUTS — it needs no margin account and no borrow, and the "
            "short-selling risk controls do not apply to it (warning)")


def _check_indicator_defs(doc: StrategyDocument, problems: list[str]) -> None:
    for ind in doc.indicators:
        spec = INDICATORS[ind.type]
        valid_names = {p.name for p in spec.params}
        for name, val in ind.params.items():
            if name not in valid_names:
                problems.append(f"indicator {ind.id}: unknown param {name!r} for {ind.type}")
                continue
            p = next(p for p in spec.params if p.name == name)
            if not (p.min <= val <= p.max):
                problems.append(
                    f"indicator {ind.id}: {name}={val} outside [{p.min}, {p.max}]")
        if spec.intraday_only and doc.timeframe.bar == "1d":
            problems.append(f"indicator {ind.id}: {ind.type} requires an intraday timeframe")
        if ind.type == "opening_range":
            minutes = ind.params.get("minutes", 15)
            bar_min = BAR_SECONDS[doc.timeframe.bar] / 60
            if doc.timeframe.bar != "1d" and minutes < bar_min:
                problems.append(
                    f"indicator {ind.id}: opening_range minutes={minutes:g} is shorter than the "
                    f"{doc.timeframe.bar} bar — the range will cover a full bar anyway")
            elif doc.timeframe.bar != "1d" and minutes % bar_min:
                problems.append(
                    f"indicator {ind.id}: opening_range minutes={minutes:g} is not a multiple of "
                    f"the {doc.timeframe.bar} bar — the range ends at the next bar close (warning)")
        if ind.type == "macd" and "fast" in ind.params and "slow" in ind.params:
            if ind.params["fast"] >= ind.params["slow"]:
                problems.append(f"indicator {ind.id}: macd fast must be < slow")


def _walk(expr: Expr, ind_map: dict[str, IndicatorDef], problems: list[str],
          label: str, depth: int) -> dict:
    stats = {"max_depth": depth, "comparisons": 0}

    def merge(sub: dict) -> None:
        stats["max_depth"] = max(stats["max_depth"], sub["max_depth"])
        stats["comparisons"] += sub["comparisons"]

    if isinstance(expr, AllExpr):
        for e in expr.all:
            merge(_walk(e, ind_map, problems, label, depth + 1))
    elif isinstance(expr, AnyExpr):
        for e in expr.any:
            merge(_walk(e, ind_map, problems, label, depth + 1))
    elif isinstance(expr, NotExpr):
        merge(_walk(expr.not_, ind_map, problems, label, depth + 1))
    elif isinstance(expr, Comparison):
        stats["comparisons"] = 1
        _check_comparison(expr, ind_map, problems, label)
    return stats


def _check_scale(op, problems, label) -> None:
    """A series operand scaled by mul=0 is not a series any more — it collapses
    to the constant `add`, which is almost certainly a mistake (and would make
    `crosses_above`/`rising` compare against a flat line)."""
    if getattr(op, "mul", 1.0) == 0:
        problems.append(f"{label}: operand mul=0 collapses the series to a constant")


def _check_offset(op, ind_map, problems, label) -> None:
    """Validate the optional indicator-valued offset (`off_ind`)."""
    off = getattr(op, "off_ind", None)
    if not off:
        if getattr(op, "off_field", None) is not None:
            problems.append(f"{label}: off_field set without off_ind")
        return
    ind = ind_map.get(off)
    if ind is None:
        problems.append(f"{label}: references unknown offset indicator {off!r}")
        return
    outputs = INDICATORS[ind.type].outputs
    field = getattr(op, "off_field", None)
    if field is None and len(outputs) > 1:
        problems.append(
            f"{label}: offset indicator {off!r} has outputs {list(outputs)} — specify off_field")
    elif field is not None and field not in outputs:
        problems.append(f"{label}: offset indicator {off!r} has no output {field!r}")
    if getattr(op, "off_mul", 1.0) == 0:
        problems.append(f"{label}: off_mul=0 makes the offset indicator a no-op (warning)")


def _operand_ref(op, ind_map, problems, label) -> bool:
    """Validate an operand; returns True if it is a *series* (not a literal)."""
    _check_scale(op, problems, label)
    _check_offset(op, ind_map, problems, label)
    if isinstance(op, IndOperand) or isinstance(op, IndOffsetOperand):
        spec = op if isinstance(op, IndOperand) else op.ind_offset
        ind = ind_map.get(spec.ind)
        if ind is None:
            problems.append(f"{label}: references unknown indicator {spec.ind!r}")
            return True
        outputs = INDICATORS[ind.type].outputs
        field = spec.field
        if field is None and len(outputs) > 1:
            problems.append(
                f"{label}: indicator {spec.ind!r} has outputs {list(outputs)} — specify field")
        elif field is not None and field not in outputs:
            problems.append(f"{label}: indicator {spec.ind!r} has no output {field!r}")
        return True
    if isinstance(op, PriceOperand):
        return True
    if isinstance(op, ValueOperand):
        return False
    return False


def _check_comparison(c: Comparison, ind_map, problems, label) -> None:
    left_series = _operand_ref(c.left, ind_map, problems, label)
    if c.op in ("rising", "falling"):
        if c.right is not None:
            problems.append(f"{label}: {c.op} takes no right operand")
        if c.bars is None:
            problems.append(f"{label}: {c.op} requires bars")
        if not left_series:
            problems.append(f"{label}: {c.op} left operand must be a series")
        return
    if c.right is None:
        problems.append(f"{label}: {c.op} requires a right operand")
        return
    if c.bars is not None:
        problems.append(f"{label}: bars is only valid with rising/falling")
    right_series = _operand_ref(c.right, ind_map, problems, label)
    if c.op in ("crosses_above", "crosses_below") and not (left_series or right_series):
        problems.append(f"{label}: {c.op} needs at least one series operand")
    _check_unreachable_extreme(c, ind_map, problems, label)
    if isinstance(c.left, ValueOperand) and isinstance(c.right, ValueOperand):
        problems.append(f"{label}: comparing two literals is always constant")


_EXTREME = {
    "highest": ("value", "high", "above"),
    "lowest": ("value", "low", "below"),
    "donchian": ("upper", "high", "above"),
}


def _check_unreachable_extreme(c: Comparison, ind_map, problems, label) -> None:
    """Catch the breakout rule that can never fire.

    `highest`/`lowest`/`donchian` include the CURRENT bar, so the bar's own high
    is already inside the window: `close > highest(20)` is false by
    construction, and the strategy silently takes zero trades while merely
    looking unprofitable. Real bug found in the wild — it invalidated an entire
    research sweep before anyone noticed. `shift: 1` reads the level as of the
    prior bar, which is what a breakout means."""
    pairs = ((c.left, c.right, c.op), (c.right, c.left, _MIRROR.get(c.op)))
    for price_op, ind_op, op in pairs:
        if op is None or not isinstance(price_op, PriceOperand) or not isinstance(ind_op, IndOperand):
            continue
        ind = ind_map.get(ind_op.ind)
        if ind is None or ind.type not in _EXTREME:
            continue
        out_field, price_field, direction = _EXTREME[ind.type]
        if ind_op.field not in (None, out_field):
            continue
        if ind.params.get("shift", 0):
            continue
        # only the same-direction comparison is impossible (close > highest);
        # `close < highest` is perfectly meaningful
        breaking_up = op in ("gt", "gte", "crosses_above") and direction == "above"
        breaking_dn = op in ("lt", "lte", "crosses_below") and direction == "below"
        if not (breaking_up or breaking_dn):
            continue
        if price_op.price not in ("close", "high", "low", "open"):
            continue
        problems.append(
            f"{label}: comparing {price_op.price} against {ind.type} with shift=0 can never be "
            f"true — the indicator's window includes the current bar. Set shift=1 on indicator "
            f"{ind_op.ind!r} to use the prior bar's level (warning)")


_MIRROR = {"gt": "lt", "gte": "lte", "lt": "gt", "lte": "gte",
           "crosses_above": "crosses_below", "crosses_below": "crosses_above"}


def _check_market_filter(doc: StrategyDocument, problems: list[str]) -> None:
    """The regime gate is evaluated on its OWN indicator list, so its
    expression may only reference those — a common mistake is referencing an
    indicator declared for the traded symbols."""
    mf = doc.market_filter
    if mf is None:
        return
    mf_map = {i.id: i for i in mf.indicators}
    _walk(mf.when, mf_map, problems, "market_filter.when", depth=1)
    traded = {i.id for i in doc.indicators}
    for ref in _referenced_indicators(mf.when):
        if ref not in mf_map and ref in traded:
            problems.append(
                f"market_filter.when references {ref!r}, which is declared for the traded "
                "symbols — the filter is evaluated on its reference symbol and can only use "
                "indicators declared inside market_filter.indicators")
    if mf.symbol in doc.universe.symbols:
        problems.append(
            f"market_filter.symbol {mf.symbol!r} is also in the universe — that is allowed, "
            "but the gate then depends on a symbol you also trade (warning)")


def _referenced_indicators(expr) -> set[str]:
    out: set[str] = set()
    if isinstance(expr, AllExpr):
        for e in expr.all:
            out |= _referenced_indicators(e)
    elif isinstance(expr, AnyExpr):
        for e in expr.any:
            out |= _referenced_indicators(e)
    elif isinstance(expr, NotExpr):
        out |= _referenced_indicators(expr.not_)
    elif isinstance(expr, Comparison):
        for op in (expr.left, expr.right):
            if isinstance(op, IndOperand):
                out.add(op.ind)
            elif isinstance(op, IndOffsetOperand):
                out.add(op.ind_offset.ind)
            if getattr(op, "off_ind", None):
                out.add(op.off_ind)
    return out


def _check_selection(doc: StrategyDocument, ind_map, problems: list[str]) -> None:
    sel = doc.selection
    if sel is None:
        return
    if isinstance(sel.metric, ValueOperand):
        problems.append("selection.metric is a constant — every symbol would rank equally")
    for ref in ({sel.metric.ind} if isinstance(sel.metric, IndOperand) else set()):
        if ref not in ind_map:
            problems.append(f"selection.metric references unknown indicator {ref!r}")
    if sel.top_n >= len(doc.universe.symbols):
        problems.append(
            f"selection.top_n ({sel.top_n}) is >= the universe size "
            f"({len(doc.universe.symbols)}) — the ranking has no effect (warning)")


def _check_live_support(doc: StrategyDocument, problems: list[str]) -> None:
    """AUDIT C1 — surface live/backtest divergences this document is exposed to.
    Before this, `lint.py` had ZERO checks referencing live-runtime support, so a
    document could lint perfectly clean and then trade something materially
    different from what was backtested.

    Only BLOCKING gaps reach lint. The non-blocking ones are documented
    approximations that hold for a large share of all strategies (44 of them set
    `flatten_eod`), so surfacing them here would put a permanent advisory on most
    documents and train the user to ignore lint output. They are still reported
    where they are actionable rather than ambient: the run's own start event, the
    promotion gate's payload, and the Running badge."""
    for gap in blocking_gaps(doc):
        problems.append(f"{gap.field}: {gap.detail}")


def _check_short(doc: StrategyDocument, problems: list[str]) -> None:
    """Short-specific checks (plan/15 §B5).

    Deliberately stricter than the long side in one place: a short without a
    stop is an unbounded loss, so that is an ERROR here where the same omission
    on a long is only a warning."""
    sr = doc.risk.short
    if doc.entry.short is None:
        if sr.enabled:
            problems.append(
                "risk.short.enabled is set but there is no entry.short rule (warning)")
        if doc.exit.signal_short is not None:
            problems.append("exit.signal_short has no effect without entry.short (warning)")
        return

    if not sr.enabled:
        problems.append(
            "entry.short requires risk.short.enabled=true — shorting must be opted into "
            "explicitly so it can never begin through a config accident")
    if doc.exit.stop_loss is None:
        problems.append(
            "exit.stop_loss is REQUIRED when shorting: a short's loss is unbounded "
            "(the price can rise without limit), unlike a long's")
    if not doc.session.flatten_eod and doc.timeframe.bar != "1d":
        problems.append(
            "entry.short with flatten_eod=false holds shorts overnight — borrow can be "
            "recalled and gap risk is unbounded to the upside (warning)")
    if doc.sizing.type == "percent_of_equity" and doc.sizing.value > 50:
        problems.append(
            f"sizing: {doc.sizing.value:g}% of equity per short leaves little margin "
            "headroom before a forced buy-in (warning)")
    if sr.maintenance_margin_pct >= sr.initial_margin_pct:
        problems.append(
            "risk.short: maintenance_margin_pct should be below initial_margin_pct — "
            "otherwise a position is in a margin call the moment it opens")
    overlap = sorted(set(sr.hard_to_borrow) & set(doc.universe.symbols))
    if overlap:
        problems.append(
            f"risk.short.hard_to_borrow lists {', '.join(overlap)} which are in the "
            "universe — they will never be shorted (warning)")


def _check_exits(doc: StrategyDocument, ind_map, problems: list[str]) -> None:
    e = doc.exit
    # a market_filter carrying `exit_when` closes positions too, so it counts as
    # an exit path (round 6: a regime strategy's exit lives on the reference
    # symbol, not on the traded one)
    gate_exit = doc.market_filter is not None and doc.market_filter.exit_when is not None
    if not any([e.signal, e.stop_loss, e.trailing_stop, e.time_stop,
                doc.session.flatten_eod, gate_exit]):
        problems.append(
            "exit: no stop_loss, trailing_stop, time_stop, signal, market_filter.exit_when, "
            "or flatten_eod — position would have no exit path")
    if e.stop_loss is None and not gate_exit:
        problems.append("exit.stop_loss: strongly recommended — add a stop (warning)")
    # `trailing_stop.activate_at` is a StopSpec like any other, so it goes
    # through this same loop rather than getting its own copy of the
    # atr_indicator/percent-width checks
    trail_activate = e.trailing_stop.activate_at if e.trailing_stop else None
    for name, spec in (("stop_loss", e.stop_loss), ("take_profit", e.take_profit),
                       ("trailing_stop", e.trailing_stop),
                       ("trailing_stop.activate_at", trail_activate)):
        if spec is None:
            continue
        if spec.type == "percent" and spec.value > 50:
            problems.append(f"exit.{name}: {spec.value}% is implausibly wide")
        if spec.type == "atr_multiple":
            if spec.atr_indicator is None:
                problems.append(f"exit.{name}: atr_multiple requires atr_indicator")
            elif spec.atr_indicator not in ind_map:
                problems.append(f"exit.{name}: unknown indicator {spec.atr_indicator!r}")
            elif ind_map[spec.atr_indicator].type != "atr":
                problems.append(f"exit.{name}: {spec.atr_indicator!r} is not an atr indicator")
    _check_trail_activation(doc, problems)
    if e.stop_loss and e.take_profit and \
            e.stop_loss.type == "percent" and e.take_profit.type == "percent":
        if e.take_profit.value < e.stop_loss.value / 5:
            problems.append("exit: take_profit is <20% of stop distance — inverted risk/reward")
    _check_scale_outs(doc, ind_map, problems)


def _check_trail_activation(doc: StrategyDocument, problems: list[str]) -> None:
    """`exit.trailing_stop.activate_at` keeps the trail DORMANT until the
    position has run that far in the favourable direction. Two ways to get it
    wrong:

    1. An activation distance smaller than the trail's own distance means the
       trail arms while the position is still under water — it can then ratchet
       in from a level that was never profitable, which is almost never what
       "activate at" was reached for. Advisory, because a tiny-gate/wide-trail
       combination is expressible on purpose.
    2. A gated trail with NO `stop_loss` leaves the position completely
       unprotected for the whole dormant window. That is an ERROR, not a
       warning: it is the same unbounded-loss class as a short without a stop
       (see `_check_short`) — the risk is not that the strategy underperforms,
       it is that nothing at all closes a losing position."""
    t = doc.exit.trailing_stop
    if t is None or t.activate_at is None:
        return
    if doc.exit.stop_loss is None:
        problems.append(
            "exit.stop_loss is REQUIRED when trailing_stop has activate_at: the trail is "
            "dormant until the activation price is reached, so until then NOTHING protects "
            "the position")
    # only comparable when both distances are in the SAME units (and, for
    # atr_multiple, off the same ATR indicator)
    comparable = t.activate_at.type == t.type and (
        t.type != "atr_multiple" or t.activate_at.atr_indicator == t.atr_indicator)
    if comparable and t.activate_at.value < t.value:
        problems.append(
            f"exit.trailing_stop.activate_at ({t.activate_at.value:g}) is nearer than the "
            f"trail distance ({t.value:g}) — the trail would arm before the position could "
            "be profitable (warning)")


def _check_scale_outs(doc: StrategyDocument, ind_map, problems: list[str]) -> None:
    """Partial exits: each level is validated like any other target spec, and
    the levels must together describe a coherent ladder — strictly increasing
    distances, less than 100% of the position sold off, and (when a final
    take_profit exists) all levels nearer than it, since a scale-out at or
    beyond the full exit would never fire."""
    outs = doc.exit.scale_outs
    if not outs:
        return
    total = sum(o.percent for o in outs)
    if total >= 100:
        problems.append(
            f"exit.scale_outs: percentages total {total:g}% — must be under 100% "
            "(the remainder is what take_profit/trailing_stop/signal exits)")
    for n, out in enumerate(outs, 1):
        spec = out.target
        label = f"exit.scale_outs[{n}].target"
        if spec.type == "percent" and spec.value > 50:
            problems.append(f"{label}: {spec.value}% is implausibly wide")
        if spec.type == "atr_multiple":
            if spec.atr_indicator is None:
                problems.append(f"{label}: atr_multiple requires atr_indicator")
            elif spec.atr_indicator not in ind_map:
                problems.append(f"{label}: unknown indicator {spec.atr_indicator!r}")
            elif ind_map[spec.atr_indicator].type != "atr":
                problems.append(f"{label}: {spec.atr_indicator!r} is not an atr indicator")

    # Ordering comparisons are only meaningful between specs of the SAME type
    # (a 1.5x ATR and a 0.4% target aren't comparable without a price).
    kinds = {o.target.type for o in outs}
    if len(kinds) == 1:
        values = [o.target.value for o in outs]
        if any(b <= a for a, b in zip(values, values[1:])):
            problems.append(
                "exit.scale_outs: targets must be listed in increasing distance from entry")
        tp = doc.exit.take_profit
        if tp is not None and tp.type == outs[0].target.type and values[-1] >= tp.value:
            problems.append(
                "exit.scale_outs: the last scale target is at or beyond take_profit — "
                "it would never fire before the position fully exits")


def _check_sizing(doc: StrategyDocument, problems: list[str]) -> None:
    s = doc.sizing
    if s.type == "percent_of_equity" and s.value > 100:
        problems.append("sizing: percent_of_equity cannot exceed 100")
    if s.type == "risk_based" and s.value > 10:
        # Advisory only: >10% risk per trade is aggressive, not invalid. It used
        # to be a hard error, which silently dropped sweep/optimizer candidates
        # (study.py filters on non-warnings) and made high-risk levers untestable.
        problems.append(
            "sizing: risking >10% of equity per trade is very aggressive (warning)")
    if s.type == "risk_based" and doc.exit.stop_loss is None:
        problems.append("sizing: risk_based requires exit.stop_loss (defines risk distance)")
    if s.type == "fixed_dollars" and s.value > s.max_position_notional:
        problems.append("sizing: fixed_dollars exceeds max_position_notional")


def _check_timeframe(doc: StrategyDocument, problems: list[str]) -> None:
    if doc.timeframe.bar == "1d":
        if doc.session.entry_window is not None:
            problems.append("session.entry_window has no effect on daily bars (warning)")
        if doc.session.flatten_eod:
            problems.append("session.flatten_eod is incompatible with daily bars")
    ew = doc.session.entry_window
    if ew is not None and ew.start >= ew.end:
        problems.append("session.entry_window: start must be before end")


def is_warning(problem: str) -> bool:
    return problem.endswith("(warning)")
