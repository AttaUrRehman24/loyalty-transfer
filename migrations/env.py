"""Alembic environment: connects to the database and runs migrations.

Where it fits: migrations only. The URL comes from the `sqlalchemy.url` option when a
caller (the test suite) sets it, otherwise from the DATABASE_URL env var. SQLAlchemy is
used here only because Alembic requires it; the service itself uses psycopg directly.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def database_url() -> str:
    """Return a SQLAlchemy URL that uses the psycopg 3 driver.

    Raises:
        RuntimeError: If no URL is configured.
    """
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # "postgresql://" would make SQLAlchemy look for psycopg2; we ship psycopg 3.
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations() -> None:
    """Apply migrations in one transaction per run (online mode only)."""
    # NullPool: a migration run is short, so no connections are kept around afterwards.
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
