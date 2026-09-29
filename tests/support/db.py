"""Small SQL helpers for integration-test assertions.

These read the database directly (not through the service) so tests check the real
stored state, independent of the code under test.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import DictRow


def assert_ledger_invariants(db: psycopg.Connection[DictRow]) -> None:
    """Fail the test if the ledger is out of balance anywhere.

    Checks: (1) all entries ever written sum to zero; (2) each account's cached balance
    equals the sum of its entries; (3) no member or clearing account is negative.
    """
    total = scalar(db, "SELECT COALESCE(SUM(amount), 0) AS v FROM ledger_entries")
    assert total == 0, f"ledger sum is {total}, expected 0"
    mismatched = db.execute(
        """
        SELECT a.id FROM accounts a
          LEFT JOIN ledger_entries e ON e.account_id = a.id
         GROUP BY a.id, a.balance
        HAVING a.balance <> COALESCE(SUM(e.amount), 0)
        """
    ).fetchall()
    assert mismatched == [], f"cached balances differ from entries: {mismatched}"
    negative = scalar(
        db, "SELECT COUNT(*) AS v FROM accounts WHERE kind <> 'ISSUANCE' AND balance < 0"
    )
    assert negative == 0


def scalar(db: psycopg.Connection[DictRow], query: str, params: tuple[Any, ...] = ()) -> int:
    """Run a query returning one row with one integer column named `v`; return that int."""
    row = db.execute(query.encode(), params).fetchone()
    assert row is not None
    return int(row["v"])


def balance(db: psycopg.Connection[DictRow], account_id: UUID) -> int:
    """Return an account's current balance."""
    return scalar(db, "SELECT balance AS v FROM accounts WHERE id = %s", (account_id,))


def clearing_balance(db: psycopg.Connection[DictRow], program: str) -> int:
    """Return a program's clearing account balance."""
    return int(
        scalar(
            db,
            "SELECT balance AS v FROM accounts WHERE program_code = %s AND kind = 'CLEARING'",
            (program,),
        )
    )
