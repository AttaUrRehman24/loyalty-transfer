"""Fixtures for integration tests.

Setup, once per test run:
- Drop and recreate the test database, then apply the Alembic migrations.
- Start both mock partners and the engine API on free local ports.

Setup, before every test:
- Empty all tables and the Redis test database, reset mock partner flags, re-seed.

Check, after every test:
- The whole ledger sums to zero and every cached balance equals its entries.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import psycopg
import pytest
import redis
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.rows import DictRow, dict_row

from app.bootstrap import Runtime, build_runtime
from app.config import Settings
from app.main import create_app
from mock_partners import airline_program, card_program
from mock_partners.common import CreditStore, MockBehavior
from scripts.seed import SeedResult, seed_demo_data
from tests.support.db import assert_ledger_invariants
from tests.support.servers import BackgroundServer

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://loyalty:loyalty@localhost:5432/loyalty_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

# Every table the tests empty between cases (order does not matter with CASCADE).
_TABLES = [
    "idempotency_keys",
    "outbox",
    "ledger_entries",
    "journals",
    "transfers",
    "rates",
    "accounts",
    "programs",
]


@dataclass(frozen=True)
class MockPartner:
    """Handle on one running mock partner: its URL, flags and recorded credits."""

    url: str
    behavior: MockBehavior
    store: CreditStore


@dataclass(frozen=True)
class Mocks:
    """Both mock partners."""

    card: MockPartner
    airline: MockPartner


@pytest.fixture(scope="session")
def database_url() -> str:
    """Create a fresh test database and migrate it with Alembic (the real migration path)."""
    base, _, db_name = TEST_DATABASE_URL.rpartition("/")
    # Connect to the maintenance database to drop/create the test database.
    with psycopg.connect(f"{base}/postgres", autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def mocks() -> Iterator[Mocks]:
    """Start both mock partners on real ports for the whole run."""
    partners: list[tuple[BackgroundServer, MockPartner]] = []
    for module in (card_program, airline_program):
        behavior, store = MockBehavior(), CreditStore()
        server = BackgroundServer(module.create_app(behavior, store)).start()
        partners.append((server, MockPartner(server.base_url, behavior, store)))
    yield Mocks(card=partners[0][1], airline=partners[1][1])
    for server, _ in partners:
        server.stop()


@pytest.fixture(scope="session")
def runtime(database_url: str) -> Iterator[Runtime]:
    """Build the real runtime with short timeouts so failure tests run fast."""
    settings = Settings(
        database_url=database_url,
        redis_url=TEST_REDIS_URL,
        db_pool_min_size=2,
        db_pool_max_size=30,
        # Short partner timeout so the timeout tests take well under a second.
        partner_timeout_seconds=0.3,
        partner_max_attempts=3,
        partner_backoff_base_seconds=0.01,
        partner_backoff_max_seconds=0.05,
        idempotency_lock_ttl_seconds=10,
        outbox_lease_seconds=10,
        # Let the reconciler look at UNKNOWN transfers immediately.
        reconcile_min_age_seconds=0,
    )
    active = build_runtime(settings)
    yield active
    active.close()


@pytest.fixture(scope="session")
def api_url(runtime: Runtime) -> Iterator[str]:
    """Serve the real FastAPI app (same runtime as the tests use) on a real port."""
    server = BackgroundServer(create_app(runtime)).start()
    yield server.base_url
    server.stop()


@pytest.fixture
def client(api_url: str) -> Iterator[httpx.Client]:
    """HTTP client for the engine API. Thread-safe, so concurrent tests can share it."""
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)
    with httpx.Client(base_url=api_url, timeout=10, limits=limits) as http:
        yield http


@pytest.fixture(scope="session")
def db(database_url: str) -> Iterator[psycopg.Connection[DictRow]]:
    """Separate autocommit connection used only for test setup and assertions."""
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture(autouse=True)
def seeded(
    runtime: Runtime,
    mocks: Mocks,
    db: psycopg.Connection[DictRow],
) -> Iterator[SeedResult]:
    """Give every test a clean, seeded world and check ledger invariants afterwards."""
    db.execute(
        sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
            sql.SQL(", ").join(sql.Identifier(t) for t in _TABLES)
        )
    )
    redis.Redis.from_url(TEST_REDIS_URL).flushdb()
    for partner in (mocks.card, mocks.airline):
        partner.behavior.reset()
        partner.store.reset()
    result = seed_demo_data(runtime.uow, mocks.card.url, mocks.airline.url)
    yield result
    assert_ledger_invariants(db)
