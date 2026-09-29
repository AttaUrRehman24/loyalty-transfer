"""Idempotent transfer execution (the service behind `POST /transfers`).

Where it fits: the services layer, wrapping the orchestrator.

Rules:
- Redis holds only a short in-flight lock per key, so two identical requests do not run
  at the same time. It is never the source of truth.
- Postgres holds the key, a hash of the request body, and the first response body.
- Same key + same body -> the stored original response.
- Same key + different body -> IdempotencyKeyReused (HTTP 422).
- Even if Redis fails or a lock expires, the unique key in Postgres still guarantees at
  most one transfer per key.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from app.domain.errors import (
    IdempotencyInProgress,
    IdempotencyKeyReused,
    IdempotencyKeyTaken,
    TransferNotFound,
)
from app.domain.models import IdempotencyRecord, TransferCommand
from app.domain.ports import InFlightLock, UnitOfWork
from app.observability import log_context, log_event
from app.services.orchestrator import TransferOrchestrator
from app.services.transfer_view import transfer_view

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransferResponse:
    """What `POST /transfers` returns.

    Attributes:
        body: The transfer view (see `transfer_view`).
        replayed: True when this is a stored response for a repeated key.
    """

    body: dict[str, object]
    replayed: bool


def hash_request(command: TransferCommand) -> str:
    """Fingerprint a transfer request so a reused key with a changed body can be detected.

    The fields are written as JSON with sorted keys, so the same request always gives the
    same hash no matter how the client ordered its JSON.

    Args:
        command: The validated request.

    Returns:
        Hex SHA-256 of the canonical JSON.
    """
    canonical = json.dumps(
        {
            "source_account_id": str(command.source_account_id),
            "destination_program": command.destination_program,
            "destination_member_id": command.destination_member_id,
            "source_points": command.source_points,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotentTransferService:
    """Runs a transfer at most once per Idempotency-Key and replays its response."""

    def __init__(
        self,
        uow: UnitOfWork,
        lock: InFlightLock,
        orchestrator: TransferOrchestrator,
        lock_ttl_seconds: int,
    ) -> None:
        """Store dependencies.

        Args:
            uow: Opens database transactions.
            lock: Redis in-flight lock.
            orchestrator: Runs the saga.
            lock_ttl_seconds: Lock lifetime; must be longer than one full request.
        """
        self._uow = uow
        self._lock = lock
        self._orchestrator = orchestrator
        self._lock_ttl_seconds = lock_ttl_seconds

    def execute(self, idempotency_key: str, command: TransferCommand) -> TransferResponse:
        """Create and run a transfer, or replay the response already stored for this key.

        Args:
            idempotency_key: Client-chosen key from the Idempotency-Key header.
            command: The validated request.

        Returns:
            The response body and whether it is a replay.

        Raises:
            IdempotencyInProgress: Another request with this key is running now.
            IdempotencyKeyReused: The key was used before with a different body.
            Any error from `TransferOrchestrator.accept` (nothing is stored in that case).
        """
        request_hash = hash_request(command)
        with log_context(idempotency_key=idempotency_key):
            token = self._lock.acquire(idempotency_key, self._lock_ttl_seconds)
            if token is None:
                raise IdempotencyInProgress("a request with this Idempotency-Key is in progress")
            try:
                return self._execute_locked(idempotency_key, request_hash, command)
            finally:
                self._lock.release(idempotency_key, token)

    def _execute_locked(
        self, idempotency_key: str, request_hash: str, command: TransferCommand
    ) -> TransferResponse:
        """The body of `execute`, run while this process holds the in-flight lock."""
        existing = self._find(idempotency_key)
        if existing is not None:
            return self._replay(existing, request_hash)
        try:
            transfer = self._orchestrator.accept(idempotency_key, request_hash, command)
        except IdempotencyKeyTaken:
            # Lost a race the Redis lock should have prevented (for example the lock
            # expired). The database unique key saved us; replay what the winner stored.
            record = self._find(idempotency_key)
            if record is None:
                raise
            return self._replay(record, request_hash)
        final = self._orchestrator.dispatch(transfer.id)
        body = transfer_view(final)
        self._save_response(idempotency_key, body)
        return TransferResponse(body=body, replayed=False)

    def _replay(self, record: IdempotencyRecord, request_hash: str) -> TransferResponse:
        """Return the stored response for a repeated key.

        Business rule: the body must match the original. If the first request never
        stored a response (the process crashed after the debit), the transfer's current
        state becomes the stored response, so every later replay is identical to it.

        Raises:
            IdempotencyKeyReused: If the body hash differs from the original.
        """
        if record.request_hash != request_hash:
            raise IdempotencyKeyReused(
                "Idempotency-Key was already used with a different request body"
            )
        with log_context(transfer_id=record.transfer_id):
            if record.response_body is not None:
                log_event(logger, "idempotency.replayed")
                return TransferResponse(body=record.response_body, replayed=True)
            body = self._current_view(record)
            self._save_response(record.key, body)
            log_event(logger, "idempotency.replayed_from_current_state")
            return TransferResponse(body=body, replayed=True)

    def _find(self, idempotency_key: str) -> IdempotencyRecord | None:
        """Read the stored record for a key, if any."""
        with self._uow() as repo:
            return repo.get_idempotency(idempotency_key)

    def _current_view(self, record: IdempotencyRecord) -> dict[str, object]:
        """Build the response body from the transfer's current state."""
        with self._uow() as repo:
            transfer = repo.get_transfer(record.transfer_id)
        if transfer is None:
            raise TransferNotFound(f"transfer {record.transfer_id} not found")
        return transfer_view(transfer)

    def _save_response(self, idempotency_key: str, body: dict[str, object]) -> None:
        """Store the first response body for a key."""
        with self._uow() as repo:
            repo.save_idempotency_response(idempotency_key, body)
