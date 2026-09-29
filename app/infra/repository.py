"""The one repository: every SQL statement the service runs lives in this file.

Where it fits: the infra layer, implementing `app.domain.ports.Repository`. A repository
is always bound to one open transaction (see `PgUnitOfWork`). Methods that change
balances lock the affected account rows first, always in id order, so concurrent
transfers queue up instead of overspending or deadlocking.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from app.domain.errors import IdempotencyKeyTaken, RateConflict
from app.domain.ledger import ensure_balanced
from app.domain.models import (
    Account,
    AccountKind,
    IdempotencyRecord,
    JournalKind,
    LedgerLine,
    Program,
    ProgramKind,
    Rate,
    Transfer,
    TransferStatus,
)

_TRANSFER_COLUMNS = """
    id, idempotency_key, source_account_id, source_program, destination_program,
    destination_member_id, source_points, destination_points, rate_id, status,
    partner_reference, failure_reason, created_at, updated_at
"""

_RATE_COLUMNS = """
    id, source_program, destination_program, version, ratio, min_points, max_points,
    increment, effective_from, effective_to
"""


class PgRepository:
    """Postgres implementation of the Repository port."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        """Bind to a connection that already has an open transaction."""
        self._conn = conn

    # ---------------------------------------------------------------- programs

    def get_program(self, code: str) -> Program | None:
        """Return the program with this code, or None."""
        row = self._conn.execute(
            "SELECT code, name, kind, adapter, base_url FROM programs WHERE code = %s", (code,)
        ).fetchone()
        return _program(row) if row else None

    def upsert_program(self, program: Program) -> None:
        """Insert a program or refresh its settings (used by the seed script)."""
        self._conn.execute(
            """
            INSERT INTO programs (code, name, kind, adapter, base_url)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE
               SET name = EXCLUDED.name, kind = EXCLUDED.kind,
                   adapter = EXCLUDED.adapter, base_url = EXCLUDED.base_url
            """,
            (program.code, program.name, program.kind.value, program.adapter, program.base_url),
        )

    # ---------------------------------------------------------------- accounts

    def get_account(self, account_id: UUID) -> Account | None:
        """Return the account, or None. No lock: use lock_accounts before changing money."""
        row = self._conn.execute(
            "SELECT id, program_code, kind, owner_ref, balance FROM accounts WHERE id = %s",
            (account_id,),
        ).fetchone()
        return _account(row) if row else None

    def get_system_account(self, program_code: str, kind: AccountKind) -> Account | None:
        """Return the program's CLEARING or ISSUANCE account, or None."""
        row = self._conn.execute(
            """
            SELECT id, program_code, kind, owner_ref, balance
              FROM accounts WHERE program_code = %s AND kind = %s
            """,
            (program_code, kind.value),
        ).fetchone()
        return _account(row) if row else None

    def insert_account(self, account: Account) -> None:
        """Create an account with a zero balance; ignore if the id exists.

        The balance always starts at zero: points only arrive through ledger journals,
        so the balance can never disagree with the entries.
        """
        self._conn.execute(
            """
            INSERT INTO accounts (id, program_code, kind, owner_ref, balance)
            VALUES (%s, %s, %s, %s, 0)
            ON CONFLICT (id) DO NOTHING
            """,
            (account.id, account.program_code, account.kind.value, account.owner_ref),
        )

    def lock_accounts(self, account_ids: Sequence[UUID]) -> dict[UUID, Account]:
        """Lock accounts with SELECT ... FOR UPDATE and return them by id.

        Rows are locked in ascending id order. Every caller uses this same order, so two
        transactions can never each hold one lock and wait for the other (a deadlock).
        """
        rows = self._conn.execute(
            """
            SELECT id, program_code, kind, owner_ref, balance
              FROM accounts WHERE id = ANY(%s) ORDER BY id FOR UPDATE
            """,
            (sorted(set(account_ids)),),
        ).fetchall()
        return {row["id"]: _account(row) for row in rows}

    # ------------------------------------------------------------------ ledger

    def has_journal(self, journal_id: UUID) -> bool:
        """Return True if the journal exists (lets the seed script run twice safely)."""
        row = self._conn.execute("SELECT 1 FROM journals WHERE id = %s", (journal_id,)).fetchone()
        return row is not None

    def post_journal(
        self,
        kind: JournalKind,
        transfer_id: UUID | None,
        lines: Sequence[LedgerLine],
        journal_id: UUID | None = None,
    ) -> UUID:
        """Write one balanced journal, its entries, and move the account balances.

        Business rules:
        - The lines must add up to zero (checked here and again by a database trigger).
        - Accounts are locked in id order before their balances change.
        - A balance update that would make a MEMBER or CLEARING account negative is
          rejected by a CHECK constraint, rolling back the whole transaction.

        Args:
            kind: Why the points move.
            transfer_id: The transfer this journal belongs to, if any.
            lines: Balanced ledger lines.
            journal_id: Fixed id to use (seed data); a new random id if None.

        Returns:
            The journal id.

        Raises:
            ValueError: If the lines are not balanced.
            psycopg.errors.CheckViolation: If a balance would go negative.
        """
        ensure_balanced(lines)
        new_id = journal_id or uuid4()
        self.lock_accounts([line.account_id for line in lines])
        self._conn.execute(
            "INSERT INTO journals (id, kind, transfer_id) VALUES (%s, %s, %s)",
            (new_id, kind.value, transfer_id),
        )
        with self._conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO ledger_entries (journal_id, account_id, amount) VALUES (%s, %s, %s)",
                [(new_id, line.account_id, line.amount) for line in lines],
            )
            # The cached balance changes in the same transaction as the entries, so
            # balance always equals the sum of the account's entries.
            cursor.executemany(
                "UPDATE accounts SET balance = balance + %s WHERE id = %s",
                [(line.amount, line.account_id) for line in lines],
            )
        return new_id

    # ------------------------------------------------------------------- rates

    def list_rates(
        self, source_program: str | None = None, destination_program: str | None = None
    ) -> list[Rate]:
        """Return rate versions; a None filter means "any"."""
        rows = self._conn.execute(
            f"""
            SELECT {_RATE_COLUMNS} FROM rates
             WHERE (%(src)s::text IS NULL OR source_program = %(src)s)
               AND (%(dst)s::text IS NULL OR destination_program = %(dst)s)
             ORDER BY source_program, destination_program, version
            """,  # noqa: S608 - the f-string only inserts a constant column list
            {"src": source_program, "dst": destination_program},
        ).fetchall()
        return [_rate(row) for row in rows]

    def lock_rate(self, rate_id: UUID) -> Rate | None:
        """Lock one rate row so only one new version can be created from it at a time."""
        row = self._conn.execute(
            f"SELECT {_RATE_COLUMNS} FROM rates WHERE id = %s FOR UPDATE",  # noqa: S608
            (rate_id,),
        ).fetchone()
        return _rate(row) if row else None

    def insert_rate(self, rate: Rate) -> None:
        """Insert one rate version.

        Raises:
            RateConflict: If the window overlaps another version of the same direction
                (exclusion constraint), the version number is taken, or a program is
                unknown.
        """
        try:
            with self._conn.transaction():
                self._conn.execute(
                    """
                    INSERT INTO rates (id, source_program, destination_program, version, ratio,
                                       min_points, max_points, increment, effective_from,
                                       effective_to)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        rate.id,
                        rate.source_program,
                        rate.destination_program,
                        rate.version,
                        rate.ratio,
                        rate.min_points,
                        rate.max_points,
                        rate.increment,
                        rate.effective_from,
                        rate.effective_to,
                    ),
                )
        except (pg_errors.ExclusionViolation, pg_errors.UniqueViolation) as error:
            raise RateConflict("rate overlaps an existing version of this direction") from error
        except pg_errors.ForeignKeyViolation as error:
            raise RateConflict("source or destination program does not exist") from error

    def close_rate(self, rate_id: UUID, effective_to: datetime) -> None:
        """End a rate version's window at `effective_to` (exclusive)."""
        self._conn.execute(
            "UPDATE rates SET effective_to = %s WHERE id = %s", (effective_to, rate_id)
        )

    # --------------------------------------------------------------- transfers

    def insert_transfer(self, transfer: Transfer) -> None:
        """Insert a new transfer row.

        Raises:
            IdempotencyKeyTaken: If another transfer already uses this idempotency key.
        """
        try:
            # A savepoint so a duplicate key error is reported cleanly as our own error.
            with self._conn.transaction():
                self._conn.execute(
                    f"INSERT INTO transfers ({_TRANSFER_COLUMNS}) "  # noqa: S608
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        transfer.id,
                        transfer.idempotency_key,
                        transfer.source_account_id,
                        transfer.source_program,
                        transfer.destination_program,
                        transfer.destination_member_id,
                        transfer.source_points,
                        transfer.destination_points,
                        transfer.rate_id,
                        transfer.status.value,
                        transfer.partner_reference,
                        transfer.failure_reason,
                        transfer.created_at,
                        transfer.updated_at,
                    ),
                )
        except pg_errors.UniqueViolation as error:
            raise IdempotencyKeyTaken("idempotency key already used") from error

    def get_transfer(self, transfer_id: UUID) -> Transfer | None:
        """Return one transfer without locking it."""
        row = self._conn.execute(
            f"SELECT {_TRANSFER_COLUMNS} FROM transfers WHERE id = %s",  # noqa: S608
            (transfer_id,),
        ).fetchone()
        return _transfer(row) if row else None

    def lock_transfer(self, transfer_id: UUID) -> Transfer | None:
        """Lock one transfer row until the transaction ends and return it."""
        row = self._conn.execute(
            f"SELECT {_TRANSFER_COLUMNS} FROM transfers WHERE id = %s FOR UPDATE",  # noqa: S608
            (transfer_id,),
        ).fetchone()
        return _transfer(row) if row else None

    def update_transfer_status(
        self,
        transfer_id: UUID,
        status: TransferStatus,
        partner_reference: str | None,
        failure_reason: str | None,
        updated_at: datetime,
    ) -> None:
        """Save a transfer's new status and outcome details."""
        self._conn.execute(
            """
            UPDATE transfers
               SET status = %s, partner_reference = %s, failure_reason = %s, updated_at = %s
             WHERE id = %s
            """,
            (status.value, partner_reference, failure_reason, updated_at, transfer_id),
        )

    def list_transfers_for_account(
        self, account_id: UUID, limit: int, offset: int
    ) -> list[Transfer]:
        """Return a page of an account's transfers, newest first."""
        rows = self._conn.execute(
            f"""
            SELECT {_TRANSFER_COLUMNS} FROM transfers
             WHERE source_account_id = %s
             ORDER BY created_at DESC, id
             LIMIT %s OFFSET %s
            """,  # noqa: S608
            (account_id, limit, offset),
        ).fetchall()
        return [_transfer(row) for row in rows]

    def list_unknown_transfers(self, updated_before: datetime, limit: int) -> list[Transfer]:
        """Return UNKNOWN transfers old enough for the reconciler, oldest first."""
        rows = self._conn.execute(
            f"""
            SELECT {_TRANSFER_COLUMNS} FROM transfers
             WHERE status = 'UNKNOWN' AND updated_at <= %s
             ORDER BY updated_at
             LIMIT %s
            """,  # noqa: S608
            (updated_before, limit),
        ).fetchall()
        return [_transfer(row) for row in rows]

    # ------------------------------------------------------------------ outbox

    def insert_outbox(self, transfer_id: UUID, lease_seconds: int) -> None:
        """Queue the partner-credit job for a transfer.

        The job starts already claimed for `lease_seconds` by the request that created
        it, so the relay leaves it alone unless that request dies.
        """
        self._conn.execute(
            """
            INSERT INTO outbox (transfer_id, kind, claimed_until)
            VALUES (%s, 'PARTNER_CREDIT', now() + make_interval(secs => %s))
            """,
            (transfer_id, lease_seconds),
        )

    def claim_due_outbox(self, lease_seconds: int, limit: int) -> list[UUID]:
        """Claim unfinished jobs whose lease expired and return their transfer ids.

        FOR UPDATE SKIP LOCKED lets several relays run at once: each one skips rows
        another relay is claiming, so no job is handed out twice at the same moment.
        """
        rows = self._conn.execute(
            """
            UPDATE outbox
               SET claimed_until = now() + make_interval(secs => %s),
                   attempts = attempts + 1
             WHERE id IN (
                   SELECT id FROM outbox
                    WHERE processed_at IS NULL AND claimed_until < now()
                    ORDER BY id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED)
            RETURNING transfer_id
            """,
            (lease_seconds, limit),
        ).fetchall()
        return [row["transfer_id"] for row in rows]

    def mark_outbox_processed(self, transfer_id: UUID) -> None:
        """Mark a transfer's job as done so it is never dispatched again."""
        self._conn.execute(
            """
            UPDATE outbox SET processed_at = now()
             WHERE transfer_id = %s AND processed_at IS NULL
            """,
            (transfer_id,),
        )

    # ------------------------------------------------------------- idempotency

    def get_idempotency(self, key: str) -> IdempotencyRecord | None:
        """Return the stored record for an idempotency key, or None."""
        row = self._conn.execute(
            """
            SELECT key, request_hash, transfer_id, response_body
              FROM idempotency_keys WHERE key = %s
            """,
            (key,),
        ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            key=row["key"],
            request_hash=row["request_hash"],
            transfer_id=row["transfer_id"],
            response_body=row["response_body"],
        )

    def insert_idempotency(self, key: str, request_hash: str, transfer_id: UUID) -> None:
        """Store a new key with its request hash.

        Raises:
            IdempotencyKeyTaken: If the key already exists.
        """
        try:
            with self._conn.transaction():
                self._conn.execute(
                    """
                    INSERT INTO idempotency_keys (key, request_hash, transfer_id)
                    VALUES (%s, %s, %s)
                    """,
                    (key, request_hash, transfer_id),
                )
        except pg_errors.UniqueViolation as error:
            raise IdempotencyKeyTaken("idempotency key already used") from error

    def save_idempotency_response(self, key: str, body: dict[str, object]) -> None:
        """Store the first response body for a key.

        Business rule: only the FIRST body is kept (`response_body IS NULL` guard), so a
        replay always returns exactly what the client saw the first time.
        """
        self._conn.execute(
            """
            UPDATE idempotency_keys SET response_body = %s
             WHERE key = %s AND response_body IS NULL
            """,
            (Jsonb(body), key),
        )


# -------------------------------------------------------------- row mappers


def _program(row: dict[str, Any]) -> Program:
    """Map a programs row to a Program."""
    return Program(
        code=row["code"],
        name=row["name"],
        kind=ProgramKind(row["kind"]),
        adapter=row["adapter"],
        base_url=row["base_url"],
    )


def _account(row: dict[str, Any]) -> Account:
    """Map an accounts row to an Account."""
    return Account(
        id=row["id"],
        program_code=row["program_code"],
        kind=AccountKind(row["kind"]),
        owner_ref=row["owner_ref"],
        balance=row["balance"],
    )


def _rate(row: dict[str, Any]) -> Rate:
    """Map a rates row to a Rate."""
    return Rate(
        id=row["id"],
        source_program=row["source_program"],
        destination_program=row["destination_program"],
        version=row["version"],
        ratio=row["ratio"],
        min_points=row["min_points"],
        max_points=row["max_points"],
        increment=row["increment"],
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
    )


def _transfer(row: dict[str, Any]) -> Transfer:
    """Map a transfers row to a Transfer."""
    return Transfer(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        source_account_id=row["source_account_id"],
        source_program=row["source_program"],
        destination_program=row["destination_program"],
        destination_member_id=row["destination_member_id"],
        source_points=row["source_points"],
        destination_points=row["destination_points"],
        rate_id=row["rate_id"],
        status=TransferStatus(row["status"]),
        partner_reference=row["partner_reference"],
        failure_reason=row["failure_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
