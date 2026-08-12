"""Shared stop/target price computation — the backtest engine and the
live/paper runtime derive stop_loss/take_profit/trailing_stop prices
identically (same "one evaluator drives everything" principle as sizing.py).

Direction is a PARAMETER, never a fork: a short's stop is the same distance as
a long's, just on the other side of entry. Expressing it that way is what keeps
long and short semantics from drifting apart (plan/15 §B2)."""

from __future__ import annotations

from typing import Callable, Literal

from .schema import ScaleOut, StopSpec, TrailingStop

Side = Literal["long", "short"]
StopKind = Literal["stop", "target"]


def is_above(side: Side, kind: StopKind) -> bool:
    """Does this price level sit ABOVE the entry?

        long  stop   -> below     short stop   -> above
        long  target -> above     short target -> below

    i.e. a protective stop is always in the ADVERSE direction and a target
    always in the favourable one; which way that is depends on the side."""
    adverse_is_up = side == "short"
    return adverse_is_up if kind == "stop" else not adverse_is_up


def compute_stop_price(spec: StopSpec, entry_px: float, atr_value: float | None,
                       below: bool | None = None, *,
                       side: Side = "long", kind: StopKind | None = None) -> float | None:
    """Price for a stop/target spec.

    Preferred call: `compute_stop_price(spec, px, atr, side=..., kind=...)`.
    The legacy `below=` boolean is still accepted (True = below entry) so
    existing call sites keep working; when given, it wins.

    `atr_value` is only consulted for atr_multiple specs — pass None otherwise.
    """
    if below is None:
        if kind is None:
            raise ValueError("compute_stop_price requires either below= or kind=")
        below = not is_above(side, kind)

    if spec.type == "percent":
        return entry_px * (1 - spec.value / 100) if below else entry_px * (1 + spec.value / 100)
    if spec.type == "fixed_price_offset":
        return entry_px - spec.value if below else entry_px + spec.value
    if spec.type == "atr_multiple":
        if atr_value is None or atr_value != atr_value:  # None or NaN (warm-up)
            return None
        return entry_px - spec.value * atr_value if below else entry_px + spec.value * atr_value
    return None


def compute_trail_activation_px(spec: TrailingStop | None, entry_px: float,
                                atr_value: float | None, *,
                                side: Side = "long") -> float | None:
    """Price at which a trailing stop ARMS, or None when it arms immediately.

    None means "no activation gate" — both an absent `activate_at` and a spec
    whose price can't be resolved yet (ATR warming up). Callers treat None as
    armed-from-entry, which is the pre-activation behaviour and therefore the
    safe fallback: the trail protects MORE, never less."""
    if spec is None or spec.activate_at is None:
        return None
    return compute_stop_price(spec.activate_at, entry_px, atr_value,
                              side=side, kind="target")


def trail_is_armed(activate_px: float | None, high_water: float, low_water: float,
                   *, side: Side = "long") -> bool:
    """Has the favourable excursion reached the activation level?

    A long arms on the high-water mark reaching UP to it, a short on the
    low-water mark reaching DOWN to it. `activate_px is None` means no gate,
    so the trail is armed from entry."""
    if activate_px is None:
        return True
    return high_water >= activate_px if side == "long" else low_water <= activate_px


def compute_trail_px(trail_px: float | None, trail_dist: float,
                     high_water: float, low_water: float,
                     *, side: Side = "long") -> float:
    """Ratchet the trail one step. Monotonic by construction — a long's trail
    only ever rises, a short's only ever falls — so a retrace can never widen
    the stop back out."""
    if side == "long":
        candidate = high_water - trail_dist
        return max(trail_px, candidate) if trail_px is not None else candidate
    candidate = low_water + trail_dist
    return min(trail_px, candidate) if trail_px is not None else candidate


def compute_scale_levels(scale_outs: list[ScaleOut], entry_px: float, qty: int,
                         atr_for: Callable[[StopSpec], float | None],
                         *, side: Side = "long") -> list[tuple[float, int]]:
    """The (target_price, shares) ladder for `exit.scale_outs`, shared by the
    backtest engine and the live/paper runtime so a scaled exit can't behave
    differently between them.

    For a short the ladder runs DOWNWARD from entry (profit is a falling
    price), which `side` handles via the same direction rule as stops.

    Shares come off the ORIGINAL entry quantity and are floored, so a level
    that rounds to zero shares (tiny position, small percentage) is dropped
    rather than emitting a zero-share order. The ladder always leaves at least
    one share for the final exit path: lint caps the percentages under 100%,
    and the running clamp here is the second line of defence. `atr_for` yields
    the ATR value for an atr_multiple spec (None otherwise); a level whose
    price can't be computed yet (ATR still warming up) is skipped."""
    levels: list[tuple[float, int]] = []
    remaining = qty
    for out in scale_outs:
        px = compute_stop_price(out.target, entry_px, atr_for(out.target),
                                side=side, kind="target")
        if px is None:
            continue
        shares = min(int(qty * out.percent / 100), remaining - 1)
        if shares <= 0:
            continue
        levels.append((px, shares))
        remaining -= shares
    return levels
