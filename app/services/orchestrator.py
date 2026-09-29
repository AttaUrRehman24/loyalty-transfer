"""Transfer saga orchestrator.

Where it fits: the services layer, the heart of the engine. A transfer runs as a saga:

1. `accept`: in ONE local database transaction, debit the member, write the transfer row,
   queue an outbox message and store the idempotency key. After this commit the debit and
   the "credit the partner" job can never be separated, even if the process crashes.
2. `dispatch`: call the partner (outside any database transaction), then in a second
   transaction record the outcome: COMPLETED, COMPENSATED (points refunded) or UNKNOWN.
3. `resolve`: shared by dispatch and the reconciler. Applies an outcome under a row lock
   and through the state machine, and posts the refund journal when needed.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4

from app.domain import state_machine
from app.domain.errors import (
    AccountNotFound,
    InsufficientPoints,
    InvalidSourceAccount,
    PartnerRejected,
    PartnerTimeout,
    PartnerUnavailable,
    TransferNotFound,
)
from app.domain.ledger import compensation_lines, transfer_debit_lines
from app.domain.models import (
    Account,
    AccountKind,
    JournalKind,
    PartnerCreditRequest,
    Program,
    Transfer,
    TransferCommand,
    TransferStatus,
)
from app.domain.ports import Clock, PartnerAdapter, PartnerDirectory, Repository, UnitOfWork
from app.observability import log_context, log_event
from app.services import rate_engine
from app.services.retry import RetryPolicy, call_with_retry

logger = logging.getLogger(__name__)


class OutcomeKind(StrEnum):
    """What we learned from the partner about one credit."""

    CREDITED = "CREDITED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PartnerOutcome:
    """Result of asking a partner to credit (or of asking whether it did).

    Attributes:
        kind: Credited, failed, or unknown (timed out).
        reference: Partner's id for the credit, when credited.
        reason: Human-readable failure reason, when failed or unknown.
    """

    kind: OutcomeKind
    reference: str | None = None
    reason: str | None = None


_TARGET_STATUS = {
    OutcomeKind.CREDITED: TransferStatus.COMPLETED,
    OutcomeKind.FAILED: TransferStatus.COMPENSATED,
    OutcomeKind.UNKNOWN: TransferStatus.UNKNOWN,
}


class TransferOrchestrator:
    """Runs the debit -> partner credit -> commit-or-compensate saga."""

    def __init__(
        self,
        uow: UnitOfWork,
        partners: PartnerDirectory,
        retry_policy: RetryPolicy,
        clock: Clock,
        outbox_lease_seconds: int,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        """Store dependencies.

        Args:
            uow: Opens database transactions.
            partners: Finds the adapter for a destination program.
            retry_policy: Retry limits for partner credit calls.
            clock: Returns the current UTC time.
            outbox_lease_seconds: How long a dispatcher owns an outbox message before the
                relay may take it over. Must exceed the longest possible partner call.
            sleep: Wait function between retries.
            rng: Random source for retry jitter.
        """
        self._uow = uow
        self._partners = partners
        self._retry_policy = retry_policy
        self._clock = clock
        self._outbox_lease_seconds = outbox_lease_seconds
        self._sleep = sleep
        self._rng = rng or random.Random()

    def accept(self, idempotency_key: str, request_hash: str, command: TransferCommand) -> Transfer:
        """Validate, price and debit a transfer in one local transaction (saga step 1).

        Args:
            idempotency_key: The client's key, stored with the transfer.
            request_hash: Fingerprint of the request body, stored with the key.
            command: What the member asked for.

        Returns:
            The new transfer in status DEBITED.

        Raises:
            AccountNotFound: Source account does not exist.
            InvalidSourceAccount: Source account is not a member account.
            NoEffectiveRate, AmountOutOfRange, InvalidIncrement, ConversionTooSmall: The
                rate rules reject the amount.
            InsufficientPoints: Member balance is too low.
            IdempotencyKeyTaken: Another request stored this key first.
        """
        with self._uow() as repo:
            member = repo.get_account(command.source_account_id)
            if member is None:
                raise AccountNotFound(f"account {command.source_account_id} not found")
            if member.kind is not AccountKind.MEMBER:
                raise InvalidSourceAccount("transfers can only debit member accounts")
            now = self._clock()
            rates = repo.list_rates(member.program_code, command.destination_program)
            rate = rate_engine.select_rate(
                rates, member.program_code, command.destination_program, now
            )
            destination_points = rate_engine.convert(rate, command.source_points)
            clearing = _require_system_account(repo, member.program_code, AccountKind.CLEARING)
            # Lock both accounts (always in id order, inside lock_accounts) before reading the
            # balance, so two transfers cannot both spend the same points.
            locked = repo.lock_accounts([member.id, clearing.id])
            if locked[member.id].balance < command.source_points:
                raise InsufficientPoints(
                    f"account has {locked[member.id].balance} points, "
                    f"transfer needs {command.source_points}"
                )
            transfer = Transfer(
                id=uuid4(),
                idempotency_key=idempotency_key,
                source_account_id=member.id,
                source_program=member.program_code,
                destination_program=command.destination_program,
                destination_member_id=command.destination_member_id,
                source_points=command.source_points,
                destination_points=destination_points,
                rate_id=rate.id,
                status=TransferStatus.DEBITED,
                partner_reference=None,
                failure_reason=None,
                created_at=now,
                updated_at=now,
            )
            repo.insert_transfer(transfer)
            repo.post_journal(
                JournalKind.TRANSFER_DEBIT,
                transfer.id,
                transfer_debit_lines(member.id, clearing.id, command.source_points),
            )
            # Transactional outbox: the "credit the partner" job is saved in the same commit
            # as the debit. The lease says "this request will dispatch it now"; if the
            # process dies, the lease expires and the outbox relay picks the job up.
            repo.insert_outbox(transfer.id, self._outbox_lease_seconds)
            repo.insert_idempotency(idempotency_key, request_hash, transfer.id)
        with log_context(transfer_id=transfer.id):
            log_event(
                logger,
                "transfer.debited",
                source_points=transfer.source_points,
                destination_points=transfer.destination_points,
                rate_id=str(rate.id),
            )
        return transfer

    def dispatch(self, transfer_id: UUID) -> Transfer:
        """Call the partner for a DEBITED transfer and record the outcome (saga step 2).

        Safe to run more than once for the same transfer: partners de-duplicate by
        transfer id, and `resolve` ignores outcomes for transfers already settled.

        Args:
            transfer_id: The transfer to dispatch.

        Returns:
            The transfer after the outcome was recorded.

        Raises:
            TransferNotFound: If the transfer does not exist.
        """
        with log_context(transfer_id=transfer_id):
            transfer, program = self._load_with_program(transfer_id)
            if transfer.status is not TransferStatus.DEBITED:
                # Someone else already dispatched it; just make sure the outbox is closed.
                return self._close_outbox(transfer_id)
            adapter = self._partners.adapter_for(program)
            outcome = self._credit_partner(adapter, transfer)
            return self.resolve(transfer_id, outcome, close_outbox=True)

    def resolve(self, transfer_id: UUID, outcome: PartnerOutcome, close_outbox: bool) -> Transfer:
        """Apply a partner outcome to a transfer under a row lock.

        Business rules:
        - A transfer that is already COMPLETED or COMPENSATED is never changed again.
        - FAILED means "give the points back": a compensation journal is posted in the
          same transaction as the status change, so status and balances always agree.
        - UNKNOWN only records the timeout; points stay debited until the reconciler
          learns the real outcome from the partner.

        Args:
            transfer_id: The transfer to update.
            outcome: What the partner said.
            close_outbox: True when called from dispatch, to mark the outbox job done.

        Returns:
            The transfer after the update.

        Raises:
            TransferNotFound: If the transfer does not exist.
            IllegalTransition: If the outcome would break the state machine.
        """
        target = _TARGET_STATUS[outcome.kind]
        with log_context(transfer_id=transfer_id), self._uow() as repo:
            # Lock the transfer row so dispatch, relay and reconciler cannot apply two
            # outcomes to the same transfer at the same time.
            current = repo.lock_transfer(transfer_id)
            if current is None:
                raise TransferNotFound(f"transfer {transfer_id} not found")
            if not current.status.is_terminal and current.status is not target:
                state_machine.transition(current.status, target)
                if target is TransferStatus.COMPENSATED:
                    self._post_compensation(repo, current)
                repo.update_transfer_status(
                    transfer_id, target, outcome.reference, outcome.reason, self._clock()
                )
                log_event(
                    logger,
                    "transfer.status_changed",
                    from_status=current.status.value,
                    to_status=target.value,
                    partner_reference=outcome.reference,
                    reason=outcome.reason,
                )
            if close_outbox:
                repo.mark_outbox_processed(transfer_id)
            return _require_transfer(repo, transfer_id)

    def _credit_partner(self, adapter: PartnerAdapter, transfer: Transfer) -> PartnerOutcome:
        """Ask the partner to credit, with retries, and turn the result into an outcome.

        Business rules:
        - Timeout -> UNKNOWN, never failure: the partner may have applied the credit.
        - 5xx or connection error after all retries -> FAILED (compensate).
        - 4xx -> FAILED straight away; the same request will never succeed.
        """
        request = PartnerCreditRequest(
            transfer_id=transfer.id,
            member_id=transfer.destination_member_id,
            points=transfer.destination_points,
        )
        try:
            result = call_with_retry(
                lambda: adapter.credit(request), self._retry_policy, self._sleep, self._rng
            )
        except PartnerTimeout as error:
            return PartnerOutcome(OutcomeKind.UNKNOWN, reason=f"partner timeout: {error}")
        except (PartnerUnavailable, PartnerRejected) as error:
            return PartnerOutcome(OutcomeKind.FAILED, reason=f"partner failure: {error}")
        return PartnerOutcome(OutcomeKind.CREDITED, reference=result.reference)

    def _post_compensation(self, repo: Repository, transfer: Transfer) -> None:
        """Refund the member by moving the points back out of the clearing account."""
        clearing = _require_system_account(repo, transfer.source_program, AccountKind.CLEARING)
        # Lock in id order (same rule as accept) so refunds and new debits cannot deadlock.
        repo.lock_accounts([transfer.source_account_id, clearing.id])
        repo.post_journal(
            JournalKind.COMPENSATION,
            transfer.id,
            compensation_lines(transfer.source_account_id, clearing.id, transfer.source_points),
        )

    def _load_with_program(self, transfer_id: UUID) -> tuple[Transfer, Program]:
        """Read a transfer and its destination program in a short transaction."""
        with self._uow() as repo:
            transfer = _require_transfer(repo, transfer_id)
            program = repo.get_program(transfer.destination_program)
            if program is None:
                raise LookupError(f"program {transfer.destination_program} is not configured")
            return transfer, program

    def _close_outbox(self, transfer_id: UUID) -> Transfer:
        """Mark the outbox job done and return the transfer unchanged."""
        with self._uow() as repo:
            repo.mark_outbox_processed(transfer_id)
            return _require_transfer(repo, transfer_id)


def _require_transfer(repo: Repository, transfer_id: UUID) -> Transfer:
    """Return the transfer or raise TransferNotFound."""
    transfer = repo.get_transfer(transfer_id)
    if transfer is None:
        raise TransferNotFound(f"transfer {transfer_id} not found")
    return transfer


def _require_system_account(repo: Repository, program_code: str, kind: AccountKind) -> Account:
    """Return a program's system account or fail loudly: it means the seed data is missing."""
    account = repo.get_system_account(program_code, kind)
    if account is None:
        raise LookupError(f"program {program_code} has no {kind} account")
    return account
