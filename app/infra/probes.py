"""Health probes for Postgres and Redis.

Where it fits: the infra layer, implementing `app.domain.ports.HealthProbe`, used by
`GET /health`.
"""

from __future__ import annotations

import redis

from app.infra.db import DbPool


class PostgresProbe:
    """Checks that a pooled connection can run a trivial query."""

    name = "database"

    def __init__(self, pool: DbPool) -> None:
        """Store the pool."""
        self._pool = pool

    def check(self) -> None:
        """Run SELECT 1. Raises if the database is unreachable."""
        with self._pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")


class RedisProbe:
    """Checks that Redis answers PING."""

    name = "redis"

    def __init__(self, client: redis.Redis) -> None:
        """Store the client."""
        self._client = client

    def check(self) -> None:
        """Send PING. Raises if Redis is unreachable."""
        self._client.ping()
