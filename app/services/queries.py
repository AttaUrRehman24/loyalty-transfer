"""Read-only services: transfer lookups and health.

Where it fits: the services layer, behind `GET /transfers/{id}`,
`GET /accounts/{id}/transfers` and `GET /health`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import UUID

from app.domain.errors import AccountNotFound, TransferNotFound
from app.domain.models import Transfer
from app.domain.ports import HealthProbe, UnitOfWork

logger = logging.getLogger(__name__)


class TransferQueries:
    """Looks up transfers."""

    def __init__(self, uow: UnitOfWork) -> None:
        """Store the unit-of-work factory."""
        self._uow = uow

    def get(self, transfer_id: UUID) -> Transfer:
        """Return one transfer.

        Raises:
            TransferNotFound: If it does not exist.
        """
        with self._uow() as repo:
            transfer = repo.get_transfer(transfer_id)
        if transfer is None:
            raise TransferNotFound(f"transfer {transfer_id} not found")
        return transfer

    def list_for_account(self, account_id: UUID, limit: int, offset: int) -> list[Transfer]:
        """Return transfers debited from one account, newest first.

        Args:
            account_id: Member account id.
            limit: Page size.
            offset: Number of rows to skip.

        Raises:
            AccountNotFound: If the account does not exist (instead of an empty list, so
                a typo in the id is visible).
        """
        with self._uow() as repo:
            if repo.get_account(account_id) is None:
                raise AccountNotFound(f"account {account_id} not found")
            return repo.list_transfers_for_account(account_id, limit, offset)


class HealthService:
    """Checks every dependency and reports which ones work."""

    def __init__(self, probes: Sequence[HealthProbe]) -> None:
        """Store the probes (database, Redis)."""
        self._probes = probes

    def check(self) -> dict[str, str]:
        """Run every probe.

        Returns:
            Map of dependency name to "ok" or "error".
        """
        results: dict[str, str] = {}
        for probe in self._probes:
            try:
                probe.check()
                results[probe.name] = "ok"
            except Exception:
                # Log the details but only expose "error": health output is public.
                logger.exception("health.probe_failed")
                results[probe.name] = "error"
        return results
