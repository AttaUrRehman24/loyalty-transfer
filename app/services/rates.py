"""Rate management and quoting.

Where it fits: the services layer, behind `GET/POST/PUT /rates` and `POST /quotes`.
Rates are versioned: a change never edits a rate in place. It closes the current version
and opens a new one, so every transfer keeps pointing at the exact numbers it used.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import RateConflict, RateNotFound
from app.domain.models import Quote, Rate, RateTerms
from app.domain.ports import Clock, UnitOfWork
from app.observability import log_event
from app.services import rate_engine

logger = logging.getLogger(__name__)


class RateService:
    """Creates, lists and versions conversion rates, and prices quotes."""

    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        """Store dependencies.

        Args:
            uow: Opens database transactions.
            clock: Returns the current UTC time.
        """
        self._uow = uow
        self._clock = clock

    def list_rates(
        self, source_program: str | None = None, destination_program: str | None = None
    ) -> list[Rate]:
        """Return all rate versions, optionally filtered by direction.

        Args:
            source_program: Only rates out of this program, if given.
            destination_program: Only rates into this program, if given.

        Returns:
            Rate versions ordered by pair and version.
        """
        with self._uow() as repo:
            return repo.list_rates(source_program, destination_program)

    def create_rate(
        self,
        source_program: str,
        destination_program: str,
        terms: RateTerms,
        effective_from: datetime | None,
    ) -> Rate:
        """Create a new rate version for one direction (version 1 if it is the first).

        Args:
            source_program: Program the points leave.
            destination_program: Program the points go to.
            terms: Ratio, min, max and increment.
            effective_from: Start of the window. None means "now".

        Returns:
            The stored rate.

        Raises:
            RateConflict: If the window overlaps an existing version of this direction,
                or a program code does not exist.
        """
        with self._uow() as repo:
            existing = repo.list_rates(source_program, destination_program)
            # Business rule: versions count up per direction, so a new rate after closed
            # ones continues the numbering instead of restarting at 1.
            version = max((rate.version for rate in existing), default=0) + 1
            rate = _build_rate(
                source_program,
                destination_program,
                version,
                terms,
                effective_from or self._clock(),
            )
            repo.insert_rate(rate)
        log_event(logger, "rate.created", rate_id=str(rate.id), version=rate.version)
        return rate

    def replace_rate(
        self, rate_id: UUID, terms: RateTerms, effective_from: datetime | None
    ) -> Rate:
        """Supersede the current version of a rate with a new version.

        Business rule: only the open (not yet closed) version can be superseded, and the
        new version must start after the old one started and not in the past. The old
        version is closed at exactly the new start, so there is no gap and no overlap.

        Args:
            rate_id: Id of the current open version.
            terms: New ratio, min, max and increment.
            effective_from: When the new version starts. None means "now".

        Returns:
            The new rate version.

        Raises:
            RateNotFound: If `rate_id` does not exist.
            RateConflict: If the version is already closed or the start time is invalid.
        """
        now = self._clock()
        starts_at = effective_from or now
        with self._uow() as repo:
            # Lock the row so two concurrent updates cannot both create "version N+1".
            current = repo.lock_rate(rate_id)
            if current is None:
                raise RateNotFound(f"rate {rate_id} not found")
            if current.effective_to is not None:
                raise RateConflict("only the current open version of a rate can be replaced")
            if starts_at < now or starts_at <= current.effective_from:
                raise RateConflict(
                    "effective_from must not be in the past and must be after the current "
                    "version's effective_from"
                )
            repo.close_rate(current.id, starts_at)
            new_rate = _build_rate(
                current.source_program,
                current.destination_program,
                current.version + 1,
                terms,
                starts_at,
            )
            repo.insert_rate(new_rate)
        log_event(
            logger,
            "rate.versioned",
            previous_rate_id=str(current.id),
            rate_id=str(new_rate.id),
            version=new_rate.version,
        )
        return new_rate

    def quote(self, source_program: str, destination_program: str, source_points: int) -> Quote:
        """Price a conversion right now without moving any points.

        Args:
            source_program: Program the points leave.
            destination_program: Program the points go to.
            source_points: Points to convert.

        Returns:
            The rate used and the destination points.

        Raises:
            NoEffectiveRate, AmountOutOfRange, InvalidIncrement, ConversionTooSmall: See
                `rate_engine.quote`.
        """
        with self._uow() as repo:
            rates = repo.list_rates(source_program, destination_program)
        return rate_engine.quote(
            rates, source_program, destination_program, source_points, self._clock()
        )


def _build_rate(
    source_program: str,
    destination_program: str,
    version: int,
    terms: RateTerms,
    effective_from: datetime,
) -> Rate:
    """Assemble a new open-ended Rate from its parts.

    Raises:
        RateConflict: If max is below min, or source and destination are the same.
    """
    if terms.max_points < terms.min_points:
        raise RateConflict("max_points must be greater than or equal to min_points")
    if source_program == destination_program:
        raise RateConflict("source and destination programs must differ")
    return Rate(
        id=uuid4(),
        source_program=source_program,
        destination_program=destination_program,
        version=version,
        ratio=terms.ratio,
        min_points=terms.min_points,
        max_points=terms.max_points,
        increment=terms.increment,
        effective_from=effective_from,
        effective_to=None,
    )
