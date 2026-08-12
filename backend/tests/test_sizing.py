"""Shared sizing tests — this function's output must be identical whether
called from the backtest engine or the live/paper runtime."""

from __future__ import annotations


from app.strategy.schema import Sizing
from app.strategy.sizing import compute_position_size


def test_percent_of_equity():
    s = Sizing(type="percent_of_equity", value=20, max_position_notional=1_000_000)
    qty = compute_position_size(s, entry_px=100, stop_px=None, equity=100_000,
                                available_cash=100_000)
    assert qty == 200  # 20% of 100k = 20k / 100 = 200 shares


def test_risk_based_uses_stop_distance():
    s = Sizing(type="risk_based", value=1, max_position_notional=1_000_000)  # risk 1% of equity
    qty = compute_position_size(s, entry_px=100, stop_px=98, equity=100_000,
                                available_cash=100_000)
    # risk_dollars=1000, stop distance=2 -> 500 shares notional 50000
    assert qty == 500


def test_risk_based_without_stop_returns_zero():
    s = Sizing(type="risk_based", value=1, max_position_notional=1_000_000)
    assert compute_position_size(s, 100, None, 100_000, 100_000) == 0


def test_never_exceeds_available_cash():
    s = Sizing(type="fixed_dollars", value=50_000, max_position_notional=1_000_000)
    qty = compute_position_size(s, entry_px=100, stop_px=None, equity=100_000,
                                available_cash=1_000)
    assert qty * 100 <= 1_000


def test_max_position_notional_caps_fixed_dollars():
    s = Sizing(type="fixed_dollars", value=50_000, max_position_notional=5_000)
    qty = compute_position_size(s, entry_px=100, stop_px=None, equity=1_000_000,
                                available_cash=1_000_000)
    assert qty == 50  # capped to $5,000 / $100


def test_round_lot_applied():
    s = Sizing(type="fixed_dollars", value=1_050, round_lot=100, max_position_notional=1_000_000)
    qty = compute_position_size(s, entry_px=10, stop_px=None, equity=100_000, available_cash=100_000)
    assert qty == 100  # 105 shares would fit, but rounds down to the nearest 100-lot


def test_commission_reserved_from_cash():
    s = Sizing(type="fixed_dollars", value=1_000, max_position_notional=1_000_000)
    without_commission = compute_position_size(s, 100, None, 100_000, 1_000)
    with_commission = compute_position_size(s, 100, None, 100_000, 1_000,
                                            commission_per_order=50)
    assert with_commission < without_commission


def test_zero_or_negative_entry_price_is_safe():
    s = Sizing(type="fixed_dollars", value=1_000, max_position_notional=1_000_000)
    assert compute_position_size(s, 0, None, 100_000, 100_000) == 0
    assert compute_position_size(s, -5, None, 100_000, 100_000) == 0


def test_risk_based_sizes_shorts():
    """A short's stop sits ABOVE entry. `entry - stop` is therefore negative,
    which used to trip the `dist <= 0` guard and return qty=0, making
    risk_based sizing unable to open any short. Regression test for that."""
    from app.strategy.schema import Sizing
    from app.strategy.sizing import compute_position_size

    sizing = Sizing(type="risk_based", value=1.0, max_position_notional=25_000)
    long_qty = compute_position_size(sizing, entry_px=100.0, stop_px=95.0,
                                     equity=100_000, available_cash=100_000)
    short_qty = compute_position_size(sizing, entry_px=100.0, stop_px=105.0,
                                      equity=100_000, available_cash=100_000)
    assert long_qty > 0
    assert short_qty > 0, "risk_based sizing must be able to size a short"
    # same 5-point risk distance -> same size on both sides
    assert long_qty == short_qty
