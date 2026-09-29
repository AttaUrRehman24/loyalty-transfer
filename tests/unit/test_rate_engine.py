"""Unit tests for the rate engine: rounding, min/max, increment, effective dates, direction."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.errors import (
    AmountOutOfRange,
    ConversionTooSmall,
    InvalidIncrement,
    NoEffectiveRate,
)
from app.domain.models import Rate
from app.domain.rounding import round_down_points
from app.services.rate_engine import convert, quote, select_rate

JAN = datetime(2026, 1, 1, tzinfo=UTC)
FEB = datetime(2026, 2, 1, tzinfo=UTC)
MAR = datetime(2026, 3, 1, tzinfo=UTC)


def make_rate(
    ratio: str = "0.8",
    source: str = "BANKCARD",
    destination: str = "SKYMILES",
    version: int = 1,
    min_points: int = 1_000,
    max_points: int = 100_000,
    increment: int = 500,
    effective_from: datetime = JAN,
    effective_to: datetime | None = None,
) -> Rate:
    """Build a Rate with sensible defaults so each test only states what it cares about."""
    return Rate(
        id=uuid4(),
        source_program=source,
        destination_program=destination,
        version=version,
        ratio=Decimal(ratio),
        min_points=min_points,
        max_points=max_points,
        increment=increment,
        effective_from=effective_from,
        effective_to=effective_to,
    )


# ------------------------------------------------------------------ rounding


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", 0), ("1", 1), ("1.999999", 1), ("1234.5", 1234), ("99999.999999", 99999)],
)
def test_round_down_points_always_floors(value: str, expected: int) -> None:
    # Any fraction is dropped, never rounded up -> the engine never creates points.
    assert round_down_points(Decimal(value)) == expected


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity"])
def test_round_down_points_rejects_invalid_values(value: str) -> None:
    # Negative, NaN and infinite amounts are programming errors -> ValueError.
    with pytest.raises(ValueError, match="finite, non-negative"):
        round_down_points(Decimal(value))


def test_convert_rounds_fractional_result_down() -> None:
    # 1,500 x 0.333333 = 499.9995 -> 499 (not 500).
    rate = make_rate(ratio="0.333333", increment=1)
    assert convert(rate, 1_500) == 499


def test_convert_ratio_above_one() -> None:
    # Ratios above 1 work too: 1,000 x 2.5 = 2,500.
    assert convert(make_rate(ratio="2.5"), 1_000) == 2_500


def test_convert_rejects_result_that_rounds_to_zero() -> None:
    # 1 point x 0.5 = 0.5 -> 0 after rounding -> rejected, member would get nothing.
    rate = make_rate(ratio="0.5", min_points=1, increment=1)
    with pytest.raises(ConversionTooSmall):
        convert(rate, 1)


# ---------------------------------------------------------------- min / max


def test_convert_accepts_exact_min_and_max() -> None:
    # Both limits are inclusive.
    rate = make_rate()
    assert convert(rate, 1_000) == 800
    assert convert(rate, 100_000) == 80_000


@pytest.mark.parametrize("points", [500, 100_500])
def test_convert_rejects_amounts_outside_min_max(points: int) -> None:
    # One increment below min or above max -> AmountOutOfRange.
    with pytest.raises(AmountOutOfRange):
        convert(make_rate(), points)


# ---------------------------------------------------------------- increment


def test_convert_rejects_amount_not_on_increment() -> None:
    # 1,250 is not a multiple of 500 -> InvalidIncrement.
    with pytest.raises(InvalidIncrement):
        convert(make_rate(), 1_250)


def test_convert_accepts_amount_on_increment() -> None:
    # 1,500 is a multiple of 500 -> allowed.
    assert convert(make_rate(), 1_500) == 1_200


# ------------------------------------------------------------ effective dates


def test_select_rate_picks_version_for_the_moment() -> None:
    # v1 covers Jan-Feb, v2 covers Feb onwards -> each moment gets the right version.
    v1 = make_rate(version=1, effective_from=JAN, effective_to=FEB)
    v2 = make_rate(ratio="1.0", version=2, effective_from=FEB)

    assert select_rate([v1, v2], "BANKCARD", "SKYMILES", datetime(2026, 1, 15, tzinfo=UTC)) is v1
    assert select_rate([v1, v2], "BANKCARD", "SKYMILES", MAR) is v2


def test_select_rate_boundary_belongs_to_new_version() -> None:
    # Windows are half open: at exactly FEB, v1 has ended and v2 has started.
    v1 = make_rate(version=1, effective_from=JAN, effective_to=FEB)
    v2 = make_rate(version=2, effective_from=FEB)

    assert select_rate([v1, v2], "BANKCARD", "SKYMILES", FEB) is v2


def test_select_rate_before_first_version_fails() -> None:
    # Nothing is effective before the first version starts -> NoEffectiveRate.
    with pytest.raises(NoEffectiveRate):
        select_rate([make_rate(effective_from=FEB)], "BANKCARD", "SKYMILES", JAN)


def test_select_rate_after_last_version_closed_fails() -> None:
    # A closed version with no successor leaves a gap -> NoEffectiveRate.
    closed = make_rate(effective_from=JAN, effective_to=FEB)
    with pytest.raises(NoEffectiveRate):
        select_rate([closed], "BANKCARD", "SKYMILES", MAR)


# ----------------------------------------------------------------- direction


def test_each_direction_uses_its_own_rate() -> None:
    # BANKCARD->SKYMILES and SKYMILES->BANKCARD are separate rates with separate rules.
    outbound = make_rate(ratio="0.8", source="BANKCARD", destination="SKYMILES")
    inbound = make_rate(
        ratio="0.5", source="SKYMILES", destination="BANKCARD", increment=1_000, max_points=50_000
    )
    rates = [outbound, inbound]

    to_airline = quote(rates, "BANKCARD", "SKYMILES", 4_000, FEB)
    to_card = quote(rates, "SKYMILES", "BANKCARD", 4_000, FEB)

    assert (to_airline.rate, to_airline.destination_points) == (outbound, 3_200)
    assert (to_card.rate, to_card.destination_points) == (inbound, 2_000)


def test_missing_reverse_direction_is_not_inferred() -> None:
    # Having A->B never implies B->A; the reverse needs its own row.
    with pytest.raises(NoEffectiveRate):
        quote([make_rate()], "SKYMILES", "BANKCARD", 1_000, FEB)
