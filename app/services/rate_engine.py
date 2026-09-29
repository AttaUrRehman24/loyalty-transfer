"""Conversion rate engine: pick the right rate version and convert points.

Where it fits: the services layer. These are pure functions with no I/O, so every rule
(effective dates, min/max, increment, rounding, direction) is unit-tested without a
database. `RateService` and `TransferOrchestrator` load rates and then call these.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from app.domain.errors import (
    AmountOutOfRange,
    ConversionTooSmall,
    InvalidIncrement,
    NoEffectiveRate,
)
from app.domain.models import Quote, Rate
from app.domain.rounding import round_down_points


def select_rate(
    rates: Sequence[Rate], source_program: str, destination_program: str, at: datetime
) -> Rate:
    """Pick the rate version that applies to one direction at one moment.

    Business rule: rates are per direction. A BANKCARD -> SKYMILES rate never prices a
    SKYMILES -> BANKCARD transfer. Among that direction's versions, the one whose window
    contains `at` wins. The database forbids overlapping windows, so at most one matches.

    Args:
        rates: Candidate rate versions (may include other pairs; they are ignored).
        source_program: Program the points leave.
        destination_program: Program the points go to.
        at: The moment to price at (timezone-aware).

    Returns:
        The matching rate version.

    Raises:
        NoEffectiveRate: If no version of this direction applies at `at`.
    """
    matches = [
        rate
        for rate in rates
        if rate.source_program == source_program
        and rate.destination_program == destination_program
        and rate.is_effective_at(at)
    ]
    if not matches:
        raise NoEffectiveRate(
            f"no rate from {source_program} to {destination_program} is effective at "
            f"{at.isoformat()}"
        )
    # Defensive: if bad data ever slipped past the DB constraint, use the newest version.
    return max(matches, key=lambda rate: rate.version)


def validate_amount(rate: Rate, source_points: int) -> None:
    """Check that an amount is allowed by the rate's min, max and increment.

    Business rule: `source_points` must be between `min_points` and `max_points`
    (both inclusive) and a whole multiple of `increment`.

    Args:
        rate: The rate version being used.
        source_points: Points the member wants to convert.

    Raises:
        AmountOutOfRange: If below min or above max.
        InvalidIncrement: If not a multiple of the increment.
    """
    if source_points < rate.min_points or source_points > rate.max_points:
        raise AmountOutOfRange(
            f"source_points must be between {rate.min_points} and {rate.max_points}"
        )
    if source_points % rate.increment != 0:
        raise InvalidIncrement(f"source_points must be a multiple of {rate.increment}")


def convert(rate: Rate, source_points: int) -> int:
    """Convert source points to destination points with one rate version.

    Business rule: destination = source x ratio, rounded down to whole points by the one
    shared rounding function. A result of zero is rejected because the member would lose
    points and get nothing.

    Args:
        rate: The rate version to apply.
        source_points: Points to convert.

    Returns:
        Whole destination points (at least 1).

    Raises:
        AmountOutOfRange: If the amount is outside min/max.
        InvalidIncrement: If the amount is not a multiple of the increment.
        ConversionTooSmall: If rounding down leaves zero destination points.
    """
    validate_amount(rate, source_points)
    destination_points = round_down_points(Decimal(source_points) * rate.ratio)
    if destination_points < 1:
        raise ConversionTooSmall("conversion would give zero destination points")
    return destination_points


def quote(
    rates: Sequence[Rate],
    source_program: str,
    destination_program: str,
    source_points: int,
    at: datetime,
) -> Quote:
    """Select the effective rate and convert, returning both.

    Args:
        rates: Candidate rate versions.
        source_program: Program the points leave.
        destination_program: Program the points go to.
        source_points: Points to convert.
        at: The moment to price at.

    Returns:
        A Quote with the rate used and the destination points.

    Raises:
        NoEffectiveRate, AmountOutOfRange, InvalidIncrement, ConversionTooSmall: See
            `select_rate` and `convert`.
    """
    rate = select_rate(rates, source_program, destination_program, at)
    return Quote(
        rate=rate, source_points=source_points, destination_points=convert(rate, source_points)
    )
