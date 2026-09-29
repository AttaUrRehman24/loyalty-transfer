"""Background recovery jobs: the outbox relay and the reconciler.

Where it fits: the services layer, run in a loop by `app.worker`.

- OutboxRelay finishes transfers whose dispatcher died after the debit was committed
  (crash between debit and partner call). It picks outbox jobs whose lease ran out.
- Reconciler settles UNKNOWN transfers (partner timed out) by asking the partner what
  really happened. Only then is a refund allowed, so we never refund a credit that the
  partner actually applied.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from app.domain.errors import PartnerError
from app.domain.models import PartnerCreditState, TransferStatus
from app.domain.ports import Clock, PartnerDirectory, UnitOfWork
from app.observability import log_context, log_event
from app.services.orchestrator import OutcomeKind, PartnerOutcome, TransferOrchestrator

logger = logging.getLogger(__name__)


class OutboxRelay:
    """Dispatches outbox jobs that no live request is handling any more."""

    def __init__(
        self,
        uow: UnitOfWork,
        orchestrator: TransferOrchestrator,
        lease_seconds: int,
        batch_size: int,
    ) -> None:
        """Store dependencies.

        Args:
            uow: Opens database transactions.
            orchestrator: Runs saga step 2 for each job.
            lease_seconds: How long this relay owns a claimed job.
            batch_size: Maximum jobs claimed per run.
        """
        self._uow = uow
        self._orchestrator = orchestrator
        self._lease_seconds = lease_seconds
        self._batch_size = batch_size

    def run_once(self) -> int:
        """Claim due outbox jobs and dispatch each one.

        One failing job is logged and skipped so it cannot block the others; its lease
        will expire and it will be retried on a later run.

        Returns:
            Number of jobs claimed.
        """
        with self._uow() as repo:
            transfer_ids = repo.claim_due_outbox(self._lease_seconds, self._batch_size)
        for transfer_id in transfer_ids:
            self._dispatch_one(transfer_id)
        return len(transfer_ids)

    def _dispatch_one(self, transfer_id: UUID) -> None:
        """Dispatch one job, logging instead of raising on failure."""
        with log_context(transfer_id=transfer_id):
            try:
                transfer = self._orchestrator.dispatch(transfer_id)
                log_event(logger, "outbox.dispatched", status=transfer.status.value)
            except Exception:
                logger.exception("outbox.dispatch_failed")


class Reconciler:
    """Settles UNKNOWN transfers by asking the partner about each one."""

    def __init__(
        self,
        uow: UnitOfWork,
        orchestrator: TransferOrchestrator,
        partners: PartnerDirectory,
        clock: Clock,
        min_age_seconds: int,
        batch_size: int,
    ) -> None:
        """Store dependencies.

        Args:
            uow: Opens database transactions.
            orchestrator: Applies the outcome (and refund) under a lock.
            partners: Finds the adapter for each destination program.
            clock: Returns the current UTC time.
            min_age_seconds: Only look at transfers UNKNOWN for at least this long, so a
                slow partner has time to finish the original request first.
            batch_size: Maximum transfers checked per run.
        """
        self._uow = uow
        self._orchestrator = orchestrator
        self._partners = partners
        self._clock = clock
        self._min_age = timedelta(seconds=min_age_seconds)
        self._batch_size = batch_size

    def run_once(self) -> int:
        """Check a batch of old-enough UNKNOWN transfers.

        Returns:
            Number of transfers moved to a final state.
        """
        with self._uow() as repo:
            candidates = repo.list_unknown_transfers(
                self._clock() - self._min_age, self._batch_size
            )
        return sum(1 for transfer in candidates if self._reconcile_one(transfer.id))

    def _reconcile_one(self, transfer_id: UUID) -> bool:
        """Ask the partner about one transfer and apply the answer.

        Business rule: if the partner cannot be reached, the transfer stays UNKNOWN and
        the points stay debited. Refunding without an answer could pay the member twice.

        Returns:
            True if the transfer reached a final state.
        """
        with log_context(transfer_id=transfer_id):
            try:
                with self._uow() as repo:
                    transfer = repo.get_transfer(transfer_id)
                    program = repo.get_program(transfer.destination_program) if transfer else None
                if transfer is None or program is None:
                    return False
                status = self._partners.adapter_for(program).credit_status(transfer_id)
            except PartnerError as error:
                log_event(logger, "reconcile.partner_unreachable", reason=str(error))
                return False
            if status.state is PartnerCreditState.COMPLETED:
                outcome = PartnerOutcome(OutcomeKind.CREDITED, reference=status.reference)
            else:
                outcome = PartnerOutcome(
                    OutcomeKind.FAILED, reason="partner has no record of the credit"
                )
            result = self._orchestrator.resolve(transfer_id, outcome, close_outbox=False)
            log_event(logger, "reconcile.resolved", status=result.status.value)
            return result.status is not TransferStatus.UNKNOWN
