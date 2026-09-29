"""Ports: the interfaces the services need from the outside world.

Where it fits: the domain layer. Services depend on these Protocols only. The `infra`
layer provides the real implementations (Postgres, Redis, HTTP partners) and `app.main`
plugs them in. This keeps dependencies pointing inward and lets tests swap parts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.models import (
    Account,
    AccountKind,
    IdempotencyRecord,
    JournalKind,
    LedgerLine,
    PartnerCreditRequest,
    PartnerCreditResult,
    PartnerCreditStatus,
    Program,
    Rate,
    Transfer,
    TransferStatus,
)

Clock = Callable[[], datetime]
"""Returns the current time as a timezone-aware UTC datetime. Injected so tests can fix it."""


class Repository(Protocol):
    """All database reads and writes, bound to one open transaction.

    Every method runs inside the transaction of the unit of work that created the
    repository. Methods named `lock_*` take row locks that are held until that
    transaction ends.
    """

    def get_program(self, code: str) -> Program | None:
        """Return the program with this code, or None."""
        ...

    def upsert_program(self, program: Program) -> None:
        """Insert a program, or update its name, kind, adapter and URL if it exists."""
        ...

    def get_account(self, account_id: UUID) -> Account | None:
        """Return the account with this id, or None. Takes no lock."""
        ...

    def get_system_account(self, program_code: str, kind: AccountKind) -> Account | None:
        """Return the program's CLEARING or ISSUANCE account, or None."""
        ...

    def insert_account(self, account: Account) -> None:
        """Insert an account with zero balance. Does nothing if the id already exists."""
        ...

    def lock_accounts(self, account_ids: Sequence[UUID]) -> dict[UUID, Account]:
        """Lock the given accounts in id order and return them keyed by id."""
        ...

    def has_journal(self, journal_id: UUID) -> bool:
        """Return True if a journal with this id was already written."""
        ...

    def post_journal(
        self,
        kind: JournalKind,
        transfer_id: UUID | None,
        lines: Sequence[LedgerLine],
        journal_id: UUID | None = None,
    ) -> UUID:
        """Write one balanced journal, its entries, and update account balances."""
        ...

    def list_rates(
        self, source_program: str | None = None, destination_program: str | None = None
    ) -> list[Rate]:
        """Return rate versions, optionally filtered by source and/or destination."""
        ...

    def lock_rate(self, rate_id: UUID) -> Rate | None:
        """Lock one rate row and return it, or None if it does not exist."""
        ...

    def insert_rate(self, rate: Rate) -> None:
        """Insert a rate version. Raises RateConflict if it overlaps another version."""
        ...

    def close_rate(self, rate_id: UUID, effective_to: datetime) -> None:
        """Set the end of a rate version's effective window."""
        ...

    def insert_transfer(self, transfer: Transfer) -> None:
        """Insert a transfer. Raises IdempotencyKeyTaken if its key is already used."""
        ...

    def get_transfer(self, transfer_id: UUID) -> Transfer | None:
        """Return the transfer with this id, or None. Takes no lock."""
        ...

    def lock_transfer(self, transfer_id: UUID) -> Transfer | None:
        """Lock one transfer row and return it, or None."""
        ...

    def update_transfer_status(
        self,
        transfer_id: UUID,
        status: TransferStatus,
        partner_reference: str | None,
        failure_reason: str | None,
        updated_at: datetime,
    ) -> None:
        """Save a new status and its outcome details for a transfer."""
        ...

    def list_transfers_for_account(
        self, account_id: UUID, limit: int, offset: int
    ) -> list[Transfer]:
        """Return transfers debited from this account, newest first."""
        ...

    def list_unknown_transfers(self, updated_before: datetime, limit: int) -> list[Transfer]:
        """Return UNKNOWN transfers last changed before `updated_before`, oldest first."""
        ...

    def insert_outbox(self, transfer_id: UUID, lease_seconds: int) -> None:
        """Queue the partner credit for a transfer, claimed by the caller for a lease."""
        ...

    def claim_due_outbox(self, lease_seconds: int, limit: int) -> list[UUID]:
        """Claim unprocessed messages whose lease ran out; return their transfer ids."""
        ...

    def mark_outbox_processed(self, transfer_id: UUID) -> None:
        """Mark a transfer's outbox message as done."""
        ...

    def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        """Return what we stored for an idempotency key, or None."""
        ...

    def insert_idempotency(self, key: str, request_hash: str, transfer_id: UUID) -> None:
        """Store a new idempotency key. Raises IdempotencyKeyTaken if it exists."""
        ...

    def save_idempotency_response(self, key: str, body: dict[str, object]) -> None:
        """Store the response body first returned for an idempotency key."""
        ...


UnitOfWork = Callable[[], AbstractContextManager[Repository]]
"""Opens one database transaction and yields a Repository bound to it.

The transaction commits when the `with` block ends normally and rolls back on any error.
"""


class PartnerAdapter(Protocol):
    """Talks to one partner loyalty program's API.

    Implementations must raise PartnerTimeout, PartnerUnavailable or PartnerRejected
    (from `app.domain.errors`) instead of HTTP-library errors.
    """

    def credit(self, request: PartnerCreditRequest) -> PartnerCreditResult:
        """Ask the partner to add points. Must be safe to call twice for one transfer."""
        ...

    def credit_status(self, transfer_id: UUID) -> PartnerCreditStatus:
        """Ask the partner whether the credit for this transfer was applied."""
        ...

    def close(self) -> None:
        """Release network resources."""
        ...


class PartnerDirectory(Protocol):
    """Finds the adapter for a program."""

    def adapter_for(self, program: Program) -> PartnerAdapter:
        """Return a ready adapter for `program`. Raises LookupError if none is registered."""
        ...


class InFlightLock(Protocol):
    """A short-lived lock that says "a request with this key is running right now"."""

    def acquire(self, key: str, ttl_seconds: int) -> str | None:
        """Try to take the lock. Return an owner token on success, None if already held."""
        ...

    def release(self, key: str, token: str) -> None:
        """Release the lock, but only if `token` still owns it."""
        ...


class HealthProbe(Protocol):
    """Checks that one dependency (database, Redis) is reachable."""

    @property
    def name(self) -> str:
        """Short name shown in the health response, for example "database"."""
        ...

    def check(self) -> None:
        """Return normally when healthy. Raise any exception when not."""
        ...
