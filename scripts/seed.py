"""Seed data: two programs, their system accounts, two funded members and two rates.

Where it fits: run once after migrations (`python -m scripts.seed`, done automatically
by docker compose). The test suite calls `seed_demo_data` too, so tests and the demo use
the same data. Safe to run many times: fixed ids plus "insert if missing" logic.

Seeded data (used by the curl examples in README.md):
- BANKCARD (card program)  -> member account 11111111-1111-4111-8111-111111111111, 100,000 pts
- SKYMILES (airline)       -> member account 22222222-2222-4222-8222-222222222222, 100,000 miles
- Rate BANKCARD -> SKYMILES: ratio 0.8, min 1,000, max 100,000, increment 500
- Rate SKYMILES -> BANKCARD: ratio 0.5, min 1,000, max 50,000, increment 1,000
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid5

from app.config import SeedSettings, Settings
from app.domain.errors import RateConflict
from app.domain.ledger import funding_lines
from app.domain.models import (
    Account,
    AccountKind,
    JournalKind,
    Program,
    ProgramKind,
    Rate,
)
from app.domain.ports import Repository, UnitOfWork
from app.infra.db import PgUnitOfWork, create_pool
from app.observability import configure_logging, log_event

logger = logging.getLogger(__name__)

# Namespace for deterministic ids: the same name always gives the same UUID.
_SEED_NAMESPACE = UUID("6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f")

CARD_PROGRAM = "BANKCARD"
AIRLINE_PROGRAM = "SKYMILES"
CARD_MEMBER_ACCOUNT_ID = UUID("11111111-1111-4111-8111-111111111111")
AIRLINE_MEMBER_ACCOUNT_ID = UUID("22222222-2222-4222-8222-222222222222")
MEMBER_REF = "MEMBER-1001"
STARTING_BALANCE = 100_000
# All seeded rates start here, well in the past, so they are effective immediately.
RATES_EFFECTIVE_FROM = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SeedResult:
    """Ids of the seeded objects (tests use them)."""

    card_member_account_id: UUID
    airline_member_account_id: UUID
    card_to_airline_rate_id: UUID
    airline_to_card_rate_id: UUID


def seed_id(name: str) -> UUID:
    """Return a stable UUID for a seed object name."""
    return uuid5(_SEED_NAMESPACE, name)


def seed_demo_data(uow: UnitOfWork, card_partner_url: str, airline_partner_url: str) -> SeedResult:
    """Create or refresh all demo data in one transaction.

    Args:
        uow: Unit-of-work factory.
        card_partner_url: Base URL of the card partner API.
        airline_partner_url: Base URL of the airline partner API.

    Returns:
        Ids of the seeded member accounts and rates.
    """
    with uow() as repo:
        _seed_program(
            repo,
            Program(
                CARD_PROGRAM,
                "Bank Card Rewards",
                ProgramKind.CARD,
                "card_program",
                card_partner_url,
            ),
        )
        _seed_program(
            repo,
            Program(
                AIRLINE_PROGRAM,
                "SkyMiles Frequent Flyer",
                ProgramKind.AIRLINE,
                "airline_program",
                airline_partner_url,
            ),
        )
        _seed_member(repo, CARD_PROGRAM, CARD_MEMBER_ACCOUNT_ID)
        _seed_member(repo, AIRLINE_PROGRAM, AIRLINE_MEMBER_ACCOUNT_ID)
        card_to_airline = _seed_rate(
            repo, CARD_PROGRAM, AIRLINE_PROGRAM, Decimal("0.8"), 1_000, 100_000, 500
        )
        airline_to_card = _seed_rate(
            repo, AIRLINE_PROGRAM, CARD_PROGRAM, Decimal("0.5"), 1_000, 50_000, 1_000
        )
    return SeedResult(
        card_member_account_id=CARD_MEMBER_ACCOUNT_ID,
        airline_member_account_id=AIRLINE_MEMBER_ACCOUNT_ID,
        card_to_airline_rate_id=card_to_airline,
        airline_to_card_rate_id=airline_to_card,
    )


def _seed_program(repo: Repository, program: Program) -> None:
    """Upsert a program and make sure its CLEARING and ISSUANCE accounts exist."""
    repo.upsert_program(program)
    for kind in (AccountKind.CLEARING, AccountKind.ISSUANCE):
        repo.insert_account(
            Account(seed_id(f"{program.code}:{kind}"), program.code, kind, kind.value.lower(), 0)
        )


def _seed_member(repo: Repository, program_code: str, account_id: UUID) -> None:
    """Create a member account and fund it once from the program's issuance account."""
    repo.insert_account(Account(account_id, program_code, AccountKind.MEMBER, MEMBER_REF, 0))
    journal_id = seed_id(f"fund:{account_id}")
    # The fixed journal id makes funding happen only on the first run.
    if not repo.has_journal(journal_id):
        issuance_id = seed_id(f"{program_code}:{AccountKind.ISSUANCE}")
        repo.post_journal(
            JournalKind.SEED,
            None,
            funding_lines(issuance_id, account_id, STARTING_BALANCE),
            journal_id=journal_id,
        )


def _seed_rate(
    repo: Repository,
    source: str,
    destination: str,
    ratio: Decimal,
    min_points: int,
    max_points: int,
    increment: int,
) -> UUID:
    """Insert version 1 of a rate if the direction has no rates yet; return its id."""
    existing = repo.list_rates(source, destination)
    if existing:
        return existing[0].id
    rate_id = seed_id(f"rate:{source}:{destination}:1")
    try:
        repo.insert_rate(
            Rate(
                rate_id,
                source,
                destination,
                1,
                ratio,
                min_points,
                max_points,
                increment,
                RATES_EFFECTIVE_FROM,
                None,
            )
        )
    except RateConflict:
        logger.exception("seed.rate_conflict")
        raise
    return rate_id


def main() -> None:
    """Seed the database named by DATABASE_URL."""
    settings = Settings.from_env()
    seed_settings = SeedSettings.from_env()
    configure_logging(settings.log_level)
    pool = create_pool(settings.database_url, 1, 2)
    try:
        result = seed_demo_data(
            PgUnitOfWork(pool), seed_settings.card_partner_url, seed_settings.airline_partner_url
        )
        log_event(logger, "seed.done", **{k: str(v) for k, v in vars(result).items()})
    finally:
        pool.close()


if __name__ == "__main__":
    main()
