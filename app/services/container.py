"""The set of services the HTTP layer can call.

Where it fits: the services layer. `app.main` builds one ServiceContainer at startup and
stores it on the FastAPI app. Routes only see this object, never infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.idempotency import IdempotentTransferService
from app.services.queries import HealthService, TransferQueries
from app.services.rates import RateService


@dataclass(frozen=True)
class ServiceContainer:
    """Everything the API routes need.

    Attributes:
        rates: Rate management and quotes.
        transfers: Idempotent transfer execution.
        transfer_queries: Transfer lookups.
        health: Dependency health checks.
    """

    rates: RateService
    transfers: IdempotentTransferService
    transfer_queries: TransferQueries
    health: HealthService
