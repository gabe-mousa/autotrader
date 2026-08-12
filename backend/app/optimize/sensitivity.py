"""Capital sensitivity of the champion (docs/plan/14-optimizer.md §1).

The plan doc rules capital OUT as a search axis and IN as a post-hoc check,
for a reason worth restating: `percent_of_equity` sizing is *nearly*
scale-invariant, so sweeping capital during the search would spend thousands
of backtests mapping a mostly-flat surface. But "nearly" is doing real work in
that sentence. Capital bites in four specific, mechanical places:

* **`max_position_notional` clips.** Above `max_position_notional / (value/100)`
  of equity, a percent-of-equity strategy stops scaling — every extra dollar
  of capital sits in cash and dilutes the return percentage. A champion tuned
  at $100k with a $25k cap is a *different strategy* at $400k.

* **`round_lot` + whole-share rounding quantizes.** `compute_position_size`
  floors to whole shares (then to the lot). On a $600 stock with $2k per
  position that is 3 shares — the realized position is 90% of the intended
  one, and the error is not a constant fraction, it moves with price. At small
  capital this is the dominant effect and it is *not* linear.

* **Absolute-dollar levers don't scale at all.** `fixed_dollars` /
  `fixed_shares` sizing, `commission.per_order`, and `risk.max_daily_loss` are
  all fixed numbers. Quadrupling capital quarters the fraction of it deployed,
  quarters the relative commission drag, and quarters the effective daily-loss
  leash in percentage terms.

* **Concurrency has a funding floor.** A strategy allowed
  `risk.max_open_positions = 5` needs roughly 5 positions' worth of cash to
  ever hold 5. Below that it silently becomes a 2-position strategy — same
  document, different behaviour, and nothing in the metrics says so.

So the check is: re-run the champion, unchanged, over ONE fixed window at a
geometric ladder of capital levels, and report retention relative to the
level the study was actually run at. Everything in this module is pure
arithmetic over metric dicts plus a static read of the strategy document; the
simulation itself is the ordinary `run_backtest_serialized` path, driven from
`study.py`.
"""

from __future__ import annotations

from typing import Any

from ..strategy.schema import StrategyDocument

# A geometric ladder centered on the study's own capital. Geometric because
# every mechanism above is multiplicative in capital (a notional cap, a share
# price, a per-order fee): equal *ratios* are equal amounts of stress, equal
# dollar steps are not.
#
# 0.5x/2x are the levels that decide the verdict -- they are the plausible
# "what if I fund this with half / twice what I said" question. 0.25x/4x are
# reported but treated as context: breaking at a quarter of the capital is
# expected for anything with a notional floor, and saying "overfit" about that
# would be crying wolf.
CAPITAL_MULTIPLIERS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0)
NEIGHBOUR_MULTIPLIERS: tuple[float, ...] = (0.5, 2.0)

# Fraction of the 1x return a neighbour level must retain to count as holding
# up. 0.5 is deliberately generous: this is a blunt "does it fall apart"
# screen, not a precision instrument.
FRAGILE_RETENTION = 0.5
# Below this, the neighbour hasn't merely shrunk, it has stopped working.
BROKEN_RETENTION = 0.1
# Relative change in trade count that means the strategy is no longer taking
# the same trades at all (positions it can't fund get skipped entirely).
TRADE_COUNT_DRIFT = 0.25

VERDICTS = ("robust", "extremes-only", "fragile", "broken", "inconclusive", "unknown")


def capital_levels(base_capital: float) -> list[float]:
    """The ladder as absolute dollars, rounded to cents.

    Rounded so the value that goes into `BacktestConfig.initial_capital` is
    exactly reproducible from the persisted number rather than depending on
    float formatting -- the whole optimizer is reproducible-by-construction
    and this is one more place that could quietly stop being true.
    """
    if not (base_capital > 0):
        raise ValueError("base capital must be positive")
    return [round(base_capital * m, 2) for m in CAPITAL_MULTIPLIERS]


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def sizing_notes(doc: StrategyDocument, capital: float,
                 ref_price: float | None = None,
                 commission_per_order: float = 0.0) -> list[str]:
    """Static, simulation-free reasons this capital level is or isn't the same
    strategy as the base one.

    These are the *explanation* half of the check. A retention number tells the
    user the champion fell apart; these tell them which lever did it, which is
    the difference between an alarming chart and an actionable one. Everything
    here is read off the document and one reference price -- no market
    assumptions beyond "positions get opened near this price".
    """
    notes: list[str] = []
    s = doc.sizing
    cap = float(s.max_position_notional)

    # intended dollars per position at this capital
    if s.type == "percent_of_equity":
        intended = capital * s.value / 100
    elif s.type == "fixed_dollars":
        intended = float(s.value)
    elif s.type == "risk_based":
        # risk_based scales with equity like percent_of_equity does, but the
        # position notional depends on the stop distance, which we don't know
        # statically. Report the risk budget, not a fake notional.
        intended = capital * s.value / 100
        notes.append(
            f"risk_based sizing risks {s.value:g}% of equity (${intended:,.0f}) per trade; the "
            "resulting position size also depends on the stop distance, so the notional cap below "
            "may bind at some prices and not others")
    else:  # fixed_shares
        intended = float(s.value) * ref_price if ref_price else 0.0

    if s.type in ("fixed_dollars", "fixed_shares"):
        notes.append(
            f"{s.type} sizing is an ABSOLUTE amount — it does not scale with capital, so at "
            f"${capital:,.0f} the strategy deploys a different fraction of the account than it did "
            "at the study's level. Return percentage moves purely because the denominator moved.")

    effective = min(intended, cap) if intended > 0 else 0.0
    if intended > cap:
        notes.append(
            f"max_position_notional (${cap:,.0f}) CLIPS the intended ${intended:,.0f} per position. "
            f"Only ${cap * doc.risk.max_open_positions:,.0f} of the ${capital:,.0f} account can ever "
            "be at work; the rest dilutes the return percentage and nothing else.")

    if ref_price and ref_price > 0 and effective > 0:
        lot = max(1, int(s.round_lot))
        shares = int(effective // ref_price)
        shares = (shares // lot) * lot
        if shares <= 0:
            notes.append(
                f"at a reference price of ${ref_price:,.2f}, ${effective:,.0f} per position buys "
                f"ZERO whole {'lots of ' + str(lot) if lot > 1 else 'shares'} — this capital level "
                "cannot open a position at all.")
        else:
            waste = 1.0 - (shares * ref_price) / effective
            if waste > 0.05:
                notes.append(
                    f"share rounding wastes {waste * 100:.0f}% of each position at a ${ref_price:,.2f} "
                    f"reference price ({shares} share{'s' if shares != 1 else ''} of an intended "
                    f"${effective:,.0f}"
                    + (f", in lots of {lot}" if lot > 1 else "")
                    + "). Below a few thousand dollars per position this quantization, not the "
                      "signal, sets the returns.")

    if doc.risk.max_open_positions > 1 and effective > 0:
        needed = effective * doc.risk.max_open_positions
        if needed > capital * 1.001:
            fundable = max(0, int(capital // effective))
            notes.append(
                f"risk.max_open_positions is {doc.risk.max_open_positions}, which needs "
                f"~${needed:,.0f} of cash to ever be reached. At ${capital:,.0f} only {fundable} "
                "can be funded at once (no margin in v1), so this is effectively a "
                f"{fundable}-position strategy here — a different one from what was optimized.")

    if commission_per_order > 0 and effective > 0:
        drag_bps = (commission_per_order / effective) * 10_000
        if drag_bps > 5:
            notes.append(
                f"commission of ${commission_per_order:,.2f} per order is {drag_bps:.0f} bps of a "
                f"${effective:,.0f} position — a fixed fee is a much bigger tax on a small account, "
                "and it is charged on entry AND exit.")

    if doc.risk.max_daily_loss > 0:
        leash = doc.risk.max_daily_loss / capital * 100
        if leash < 0.5 or leash > 10:
            notes.append(
                f"risk.max_daily_loss is a fixed ${doc.risk.max_daily_loss:,.0f}, which is "
                f"{leash:.2f}% of ${capital:,.0f}. Same document, very different leash — this guard "
                "is not scale-invariant either. (It gates the live runner, not the backtest, so it "
                "will not show up in the numbers above.)")

    return notes


def reference_price(closes: list[float]) -> float | None:
    """Median close, as the one price the static notes reason about.

    Median, not mean: a 20-year daily series that 10x'd has a mean dominated by
    the recent end, and the question "can this capital level buy a whole share"
    is about the typical bar, not the last one. Sorted-index selection keeps it
    exactly reproducible (no float accumulation).
    """
    vals = sorted(v for v in (_num(c) for c in closes) if v is not None and v > 0)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def summarize(levels: list[dict], *, base_capital: float, metric: str) -> dict:
    """Turn the per-level runs into a verdict and one blunt sentence.

    Deliberately mirrors `HoldoutCard`'s contract: a single categorical verdict
    plus a headline that states the problem in words, because the failure mode
    this guards against ("the champion only works at exactly $100k") is
    invisible in a table of five similar-looking percentages until you divide
    them.

    `levels` is the list of per-level dicts produced by the runner, each with
    `multiplier`, `capital`, and either `metrics` or `error`.
    """
    by_mult = {round(float(lv["multiplier"]), 6): lv for lv in levels}
    base = by_mult.get(1.0)

    out: dict[str, Any] = {
        "base_capital": round(float(base_capital), 2),
        "metric": metric,
        "multipliers": list(CAPITAL_MULTIPLIERS),
        "levels": levels,
        "worst_multiplier": None,
        "worst_retention": None,
    }

    def ret_of(lv: dict | None) -> float | None:
        if not lv or not lv.get("metrics"):
            return None
        return _num(lv["metrics"].get("total_return_pct"))

    def trades_of(lv: dict | None) -> int | None:
        if not lv or not lv.get("metrics"):
            return None
        v = lv["metrics"].get("trade_count")
        return int(v) if v is not None else None

    base_ret = ret_of(base)
    base_trades = trades_of(base)

    if base is None or base.get("error") or base_ret is None:
        out["verdict"] = "unknown"
        out["headline"] = (
            "The champion could not be re-run at its own capital level, so there is nothing to "
            "compare the other levels against."
            + (f" ({base['error']})" if base and base.get("error") else ""))
        return out

    if not base_trades:
        out["verdict"] = "inconclusive"
        out["headline"] = (
            "Over this window the champion took no trades even at its own capital level, so the "
            "capital ladder says nothing. Read the fold results instead.")
        return out

    # Per-level retention, computed only where it means something.
    for lv in levels:
        r = ret_of(lv)
        lv["retention"] = (r / base_ret) if (r is not None and base_ret != 0) else None
        t = trades_of(lv)
        lv["trade_drift"] = ((t - base_trades) / base_trades) if t is not None else None

    if base_ret <= 0:
        out["verdict"] = "inconclusive"
        out["headline"] = (
            f"The champion returns {base_ret:.1f}% over this window at its own capital level, so "
            "there is no profit whose scale-dependence could be tested. Fix that first; the capital "
            "ladder is a question about a working strategy.")
        return out

    neighbours = [by_mult[m] for m in NEIGHBOUR_MULTIPLIERS if m in by_mult]
    wide = [lv for m, lv in by_mult.items() if m not in NEIGHBOUR_MULTIPLIERS and m != 1.0]

    def worst(group: list[dict]) -> tuple[dict | None, float | None]:
        scored = [(lv, lv.get("retention")) for lv in group if lv.get("retention") is not None]
        if not scored:
            return (group[0] if group else None), None
        return min(scored, key=lambda p: p[1])

    n_worst, n_ret = worst(neighbours)
    if n_worst is not None:
        out["worst_multiplier"] = n_worst["multiplier"]
        out["worst_retention"] = n_ret

    def fmt_mult(m: float) -> str:
        return f"{m:g}×"

    dead = [lv for lv in neighbours
            if lv.get("error") or not trades_of(lv) or (ret_of(lv) or 0) <= 0]
    if dead:
        lv = dead[0]
        out["verdict"] = "broken"
        why = ("errored" if lv.get("error")
               else "took no trades at all" if not trades_of(lv)
               else f"turned a {base_ret:.1f}% gain into {ret_of(lv):.1f}%")
        out["headline"] = (
            f"At {fmt_mult(lv['multiplier'])} capital (${lv['capital']:,.0f}) the champion {why}. "
            "This result is an artifact of the starting-capital number, not a property of the "
            "strategy — do not promote it on the strength of the 1× column.")
        return out

    if n_ret is not None and n_ret < BROKEN_RETENTION:
        out["verdict"] = "broken"
        out["headline"] = (
            f"At {fmt_mult(out['worst_multiplier'])} capital the champion keeps only "
            f"{n_ret * 100:.0f}% of its {base_ret:.1f}% return. It is fitted to the capital "
            "assumption as much as to the price history.")
        return out

    if n_ret is not None and n_ret < FRAGILE_RETENTION:
        out["verdict"] = "fragile"
        out["headline"] = (
            f"Halving or doubling the capital costs the champion most of its edge: at "
            f"{fmt_mult(out['worst_multiplier'])} it retains {n_ret * 100:.0f}% of the "
            f"{base_ret:.1f}% it makes at ${base_capital:,.0f}. Size the account to what you "
            "actually have and re-run, rather than trusting the 1× number.")
        return out

    drift = [lv for lv in neighbours
             if lv.get("trade_drift") is not None and abs(lv["trade_drift"]) > TRADE_COUNT_DRIFT]
    if drift:
        lv = drift[0]
        out["verdict"] = "fragile"
        out["headline"] = (
            f"Returns hold up, but at {fmt_mult(lv['multiplier'])} capital the champion takes "
            f"{abs(lv['trade_drift']) * 100:.0f}% "
            f"{'more' if lv['trade_drift'] > 0 else 'fewer'} trades "
            f"({trades_of(lv)} vs {base_trades}) — it is not taking the same trades, so the "
            "fold statistics were measured on a strategy you would not be running.")
        return out

    w_worst, w_ret = worst(wide)
    if w_ret is not None and w_ret < FRAGILE_RETENTION:
        out["verdict"] = "extremes-only"
        out["headline"] = (
            f"The champion holds up at half and double its capital, but at "
            f"{fmt_mult(w_worst['multiplier'])} (${w_worst['capital']:,.0f}) it retains only "
            f"{w_ret * 100:.0f}% of its return. That is normal for a strategy with a fixed "
            "notional cap or lot size — just know the working range.")
        return out

    out["verdict"] = "robust"
    out["headline"] = (
        f"The champion returns {base_ret:.1f}% at ${base_capital:,.0f} and keeps at least "
        f"{(n_ret or 1) * 100:.0f}% of that at both half and double the capital. The result is not "
        "an artifact of the starting-capital assumption.")
    return out
