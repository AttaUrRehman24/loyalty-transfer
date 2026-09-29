"""Postgres connection pool and unit of work.

Where it fits: the infra layer. `PgUnitOfWork` is the only way code opens a transaction.
Connections run in autocommit mode and every piece of work is wrapped in an explicit
`conn.transaction()`, so nothing is ever written outside a clearly bounded transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from app.domain.ports import Repository
from app.infra.repository import PgRepository

DbPool = ConnectionPool[Connection[DictRow]]
"""A pool whose connections return rows as dicts."""


def create_pool(database_url: str, min_size: int, max_size: int) -> DbPool:
    """Open a pool of Postgres connections.

    Args:
        database_url: libpq URL, for example postgresql://user:pass@host:5432/db.
        min_size: Connections kept open at all times.
        max_size: Hard cap on open connections.

    Returns:
        An open pool. Call `close()` on shutdown.

    Raises:
        psycopg_pool.PoolTimeout: If the database cannot be reached at startup.
    """
    pool = ConnectionPool(
        database_url,
        # Tells the type checker that rows come back as dicts (matches row_factory below).
        connection_class=Connection[DictRow],
        min_size=min_size,
        max_size=max_size,
        # autocommit=True means "no hidden transaction"; we always open one explicitly.
        # timezone=UTC so every timestamp read back is in UTC, whatever the server's zone.
        kwargs={"autocommit": True, "row_factory": dict_row, "options": "-c timezone=UTC"},
        open=False,
    )
    pool.open(wait=True)
    return pool


class PgUnitOfWork:
    """Callable that opens one transaction and yields a repository bound to it."""

    def __init__(self, pool: DbPool) -> None:
        """Store the pool."""
        self._pool = pool

    @contextmanager
    def __call__(self) -> Iterator[Repository]:
        """Borrow a connection, start a transaction, yield a repository.

        The transaction commits if the block finishes normally and rolls back if it
        raises. Row locks taken inside are released at that point.

        Yields:
            A PgRepository using this transaction.
        """
        with self._pool.connection() as conn, conn.transaction():
            yield PgRepository(conn)
