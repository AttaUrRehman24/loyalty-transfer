"""Core data types of the transfer engine.

Where it fits: the domain layer. Every other layer passes these plain, immutable objects
around. They carry data only; business rules live in `rounding`, `ledger`,
`state_machine` and the services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class ProgramKind(StrEnum):
    """The two families of loyalty programs the engine converts between."""

    CARD = "CARD"
    AIRLINE = "AIRLINE"


class AccountKind(StrEnum):
    """Role of a ledger account.

    MEMBER: points owned by one customer inside one program. Can never go below zero.
    CLEARING: points that left a program on their way to a partner. Can never go below zero.
    ISSUANCE: where points are created from when a member is funded. The only account
        allowed to be negative, because it is the "other side" of every funding entry.
    """

    MEMBER = "MEMBER"
    CLEARING = "CLEARING"
    ISSUANCE = "ISSUANCE"


class JournalKind(StrEnum):
    """Why a group of ledger entries was written."""

    SEED = "SEED"
    TRANSFER_DEBIT = "TRANSFER_DEBIT"
    COMPENSATION = "COMPENSATION"


class TransferStatus(StrEnum):
    """Lifecycle of a transfer. Allowed moves are defined in `state_machine`.

    DEBITED: points taken from the member, partner credit not confirmed yet.
    UNKNOWN: the partner call timed out, so we do not know if the credit happened.
    COMPLETED: the partner confirmed the credit. Final.
    COMPENSATED: the partner credit failed and the points were given back. Final.
    """

    DEBITED = "DEBITED"
    UNKNOWN = "UNKNOWN"
    COMPLETED = "COMPLETED"
    COMPENSATED = "COMPENSATED"

    @property
    def is_terminal(self) -> bool:
        """Return True when no further status change is possible."""
        return self in (TransferStatus.COMPLETED, TransferStatus.COMPENSATED)


class PartnerCreditState(StrEnum):
    """What a partner says when we ask about an earlier credit request."""

    COMPLETED = "COMPLETED"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class Program:
    """A loyalty program we can move points into or out of.

    Attributes:
        code: Short stable id, for example "BANKCARD".
        name: Display name.
        kind: Card or airline.
        adapter: Name of the partner adapter that talks to this program's API.
        base_url: Root URL of the partner's API.
    """

    code: str
    name: str
    kind: ProgramKind
    adapter: str
    base_url: str


@dataclass(frozen=True)
class Account:
    """One ledger account and its current balance in whole points."""

    id: UUID
    program_code: str
    kind: AccountKind
    owner_ref: str
    balance: int


@dataclass(frozen=True)
class Rate:
    """One version of a conversion rate for one direction between two programs.

    Attributes:
        ratio: Destination points given per one source point, for example 0.8.
        min_points: Smallest number of source points allowed in one transfer.
        max_points: Largest number of source points allowed in one transfer.
        increment: Source points must be a whole multiple of this step.
        effective_from: First moment this version applies (inclusive).
        effective_to: Moment this version stops applying (exclusive). None = open ended.
    """

    id: UUID
    source_program: str
    destination_program: str
    version: int
    ratio: Decimal
    min_points: int
    max_points: int
    increment: int
    effective_from: datetime
    effective_to: datetime | None

    def is_effective_at(self, moment: datetime) -> bool:
        """Return True if this version applies at `moment`.

        The window is half open: `effective_from` is included and `effective_to` is not.
        This way two back-to-back versions never both apply at the same instant.
        """
        if moment < self.effective_from:
            return False
        return self.effective_to is None or moment < self.effective_to


@dataclass(frozen=True)
class RateTerms:
    """The editable numbers of a rate, used when creating a rate or a new version."""

    ratio: Decimal
    min_points: int
    max_points: int
    increment: int


@dataclass(frozen=True)
class Quote:
    """Result of pricing a conversion: which rate was used and what the member gets."""

    rate: Rate
    source_points: int
    destination_points: int


@dataclass(frozen=True)
class TransferCommand:
    """A member's request to move points out of one of their accounts to a partner.

    Attributes:
        source_account_id: Member ledger account that will be debited.
        destination_program: Program code that receives the converted points.
        destination_member_id: The member's id inside the destination program.
        source_points: How many source points to convert.
    """

    source_account_id: UUID
    destination_program: str
    destination_member_id: str
    source_points: int


@dataclass(frozen=True)
class Transfer:
    """A stored transfer and its current status."""

    id: UUID
    idempotency_key: str
    source_account_id: UUID
    source_program: str
    destination_program: str
    destination_member_id: str
    source_points: int
    destination_points: int
    rate_id: UUID
    status: TransferStatus
    partner_reference: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class IdempotencyRecord:
    """What we remember about an idempotency key.

    Attributes:
        key: The client's Idempotency-Key header value.
        request_hash: Fingerprint of the request body first sent with this key.
        transfer_id: The transfer that key created.
        response_body: The exact body first returned to the client, or None if the first
            request never got that far (for example the process crashed).
    """

    key: str
    request_hash: str
    transfer_id: UUID
    response_body: dict[str, object] | None


@dataclass(frozen=True)
class LedgerLine:
    """One side of a double-entry posting. Negative amount = debit, positive = credit."""

    account_id: UUID
    amount: int


@dataclass(frozen=True)
class PartnerCreditRequest:
    """What we ask a partner to do: add `points` to `member_id`.

    `transfer_id` is sent as the partner-side request id so the partner can spot retries
    of the same credit and apply it only once.
    """

    transfer_id: UUID
    member_id: str
    points: int


@dataclass(frozen=True)
class PartnerCreditResult:
    """A partner's confirmation that a credit was applied, with its own reference id."""

    reference: str


@dataclass(frozen=True)
class PartnerCreditStatus:
    """A partner's answer to "did you apply this credit?"."""

    state: PartnerCreditState
    reference: str | None
