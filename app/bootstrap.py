"""Composition root: builds real infrastructure and plugs it into the services.

Where it fits: the outermost layer, used by `app.main` (API), `app.worker` (background
jobs), `scripts.seed` and the tests. It is the only place that knows both the services
and the concrete infra classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import redis

from app.config import Settings
from app.infra.db import DbPool, PgUnitOfWork, create_pool
from app.infra.partners.registry import HttpPartnerDirectory
from app.infra.probes import PostgresProbe, RedisProbe
from app.infra.redis_lock import RedisInFlightLock
from app.services.container import ServiceContainer
from app.services.idempotency import IdempotentTransferService
from app.services.orchestrator import TransferOrchestrator
from app.services.queries import HealthService, TransferQueries
from app.services.rates import RateService
from app.services.recovery import OutboxRelay, Reconciler
from app.services.retry import RetryPolicy


def utc_now() -> datetime:
    """Return the current time in UTC (the production Clock)."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class Runtime:
    """Everything built at startup, plus the handles that must be closed at shutdown.

    Attributes:
        settings: The settings used to build this runtime.
        pool: Postgres connection pool.
        redis_client: Redis client.
        partners: Partner adapter directory.
        uow: Unit-of-work factory.
        orchestrator: Saga orchestrator.
        relay: Outbox relay job.
        reconciler: UNKNOWN reconciler job.
        services: What the HTTP routes use.
    """

    settings: Settings
    pool: DbPool
    redis_client: redis.Redis
    partners: HttpPartnerDirectory
    uow: PgUnitOfWork
    orchestrator: TransferOrchestrator
    relay: OutboxRelay
    reconciler: Reconciler
    services: ServiceContainer

    def close(self) -> None:
        """Close partner clients, the Redis client and the database pool."""
        self.partners.close()
        self.redis_client.close()
        self.pool.close()


def build_runtime(settings: Settings) -> Runtime:
    """Create all infrastructure and services from settings.

    Args:
        settings: Validated settings.

    Returns:
        A ready Runtime. The caller must call `close()` when done.

    Raises:
        psycopg_pool.PoolTimeout: If Postgres is unreachable.
    """
    pool = create_pool(settings.database_url, settings.db_pool_min_size, settings.db_pool_max_size)
    redis_client = redis.Redis.from_url(settings.redis_url)
    partners = HttpPartnerDirectory(settings.partner_timeout_seconds)
    uow = PgUnitOfWork(pool)
    orchestrator = TransferOrchestrator(
        uow=uow,
        partners=partners,
        retry_policy=RetryPolicy(
            max_attempts=settings.partner_max_attempts,
            base_delay_seconds=settings.partner_backoff_base_seconds,
            max_delay_seconds=settings.partner_backoff_max_seconds,
        ),
        clock=utc_now,
        outbox_lease_seconds=settings.outbox_lease_seconds,
    )
    services = ServiceContainer(
        rates=RateService(uow, utc_now),
        transfers=IdempotentTransferService(
            uow,
            RedisInFlightLock(redis_client),
            orchestrator,
            settings.idempotency_lock_ttl_seconds,
        ),
        transfer_queries=TransferQueries(uow),
        health=HealthService([PostgresProbe(pool), RedisProbe(redis_client)]),
    )
    return Runtime(
        settings=settings,
        pool=pool,
        redis_client=redis_client,
        partners=partners,
        uow=uow,
        orchestrator=orchestrator,
        relay=OutboxRelay(
            uow, orchestrator, settings.outbox_lease_seconds, settings.worker_batch_size
        ),
        reconciler=Reconciler(
            uow,
            orchestrator,
            partners,
            utc_now,
            settings.reconcile_min_age_seconds,
            settings.worker_batch_size,
        ),
        services=services,
    )
