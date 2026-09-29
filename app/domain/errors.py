"""Business errors raised by the domain and services.

Where it fits: the domain layer. Each error has a stable `code` string. The API layer maps
error classes to HTTP status codes and renders them with one shared error schema, so the
rest of the code never thinks about HTTP.
"""

from __future__ import annotations

from typing import ClassVar


class DomainError(Exception):
    """Base class for every expected business error.

    Attributes:
        code: Stable machine-readable error code returned to API clients.
    """

    code: ClassVar[str] = "DOMAIN_ERROR"


class NotFoundError(DomainError):
    """Base class for "the thing you asked for does not exist"."""

    code = "NOT_FOUND"


class AccountNotFound(NotFoundError):
    """No ledger account has the given id."""

    code = "ACCOUNT_NOT_FOUND"


class TransferNotFound(NotFoundError):
    """No transfer has the given id."""

    code = "TRANSFER_NOT_FOUND"


class RateNotFound(NotFoundError):
    """No rate row has the given id."""

    code = "RATE_NOT_FOUND"


class NoEffectiveRate(DomainError):
    """No rate version applies to this program pair and direction right now."""

    code = "NO_EFFECTIVE_RATE"


class ConversionRejected(DomainError):
    """Base class for amounts the rate rules do not allow."""

    code = "CONVERSION_REJECTED"


class AmountOutOfRange(ConversionRejected):
    """Source points are below the rate's minimum or above its maximum."""

    code = "AMOUNT_OUT_OF_RANGE"


class InvalidIncrement(ConversionRejected):
    """Source points are not a whole multiple of the rate's increment."""

    code = "INVALID_INCREMENT"


class ConversionTooSmall(ConversionRejected):
    """After rounding down, the member would receive zero destination points."""

    code = "CONVERSION_TOO_SMALL"


class InvalidSourceAccount(DomainError):
    """The source account is a system account, not a member account."""

    code = "INVALID_SOURCE_ACCOUNT"


class InsufficientPoints(DomainError):
    """The member account does not hold enough points for this transfer."""

    code = "INSUFFICIENT_POINTS"


class RateConflict(DomainError):
    """A rate change would overlap another version or edit a closed version."""

    code = "RATE_CONFLICT"


class IdempotencyKeyReused(DomainError):
    """The same Idempotency-Key was sent again with a different request body."""

    code = "IDEMPOTENCY_KEY_REUSED"


class IdempotencyInProgress(DomainError):
    """Another request with the same Idempotency-Key is being processed right now."""

    code = "IDEMPOTENCY_IN_PROGRESS"


class IdempotencyKeyTaken(DomainError):
    """Internal signal: the database already holds this key (a race was lost).

    Never shown to clients. The idempotency service catches it and replays the stored
    response instead.
    """

    code = "IDEMPOTENCY_KEY_TAKEN"


class IllegalTransition(DomainError):
    """A transfer status change that the state machine does not allow."""

    code = "ILLEGAL_TRANSITION"


class PartnerError(Exception):
    """Base class for problems talking to a partner API.

    Not a DomainError: these are handled inside the saga and never reach the client
    directly.
    """


class PartnerTimeout(PartnerError):
    """The partner did not answer in time. The credit may or may not have happened."""


class PartnerUnavailable(PartnerError):
    """The partner answered with a 5xx or the connection failed. Safe to retry."""


class PartnerRejected(PartnerError):
    """The partner answered with a 4xx. Retrying the same request will not help."""
