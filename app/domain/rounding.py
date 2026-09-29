"""The single rounding rule for points.

Where it fits: the domain layer. Every conversion from a decimal amount to whole points
must call `round_down_points`, so there is exactly one place that decides rounding.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal


def round_down_points(value: Decimal) -> int:
    """Turn a decimal amount of points into whole points by always rounding down.

    Business rule: points are whole numbers and we never give a member a fraction we do
    not have, so any fraction is dropped (1234.99 -> 1234). Rounding down also means the
    engine can never create points out of nothing through rounding.

    Args:
        value: A non-negative decimal amount of points.

    Returns:
        The largest whole number that is not greater than `value`.

    Raises:
        ValueError: If `value` is negative or not a finite number.
    """
    if not value.is_finite() or value < 0:
        raise ValueError(f"points must be a finite, non-negative number, got {value}")
    return int(value.to_integral_value(rounding=ROUND_FLOOR))
