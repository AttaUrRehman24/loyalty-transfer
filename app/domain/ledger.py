"""Double-entry posting rules.

Where it fits: the domain layer. The services call these helpers to build the ledger lines
for each business event. The repository writes the lines. Every group of lines must add
up to zero, so points are only ever moved, never created or lost.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from app.domain.models import LedgerLine


def ensure_balanced(lines: Sequence[LedgerLine]) -> None:
    """Check that a group of ledger lines is a valid double-entry posting.

    Business rule: in double-entry bookkeeping every debit has an equal credit, so the
    amounts in one journal must add up to exactly zero. The database checks this again
    with a trigger; checking here too gives a clear error before any SQL runs.

    Args:
        lines: The lines that will be written as one journal.

    Raises:
        ValueError: If there are fewer than two lines, any amount is zero, or the sum
            is not zero.
    """
    if len(lines) < 2:
        raise ValueError("a journal needs at least two lines")
    if any(line.amount == 0 for line in lines):
        raise ValueError("ledger lines must not have a zero amount")
    total = sum(line.amount for line in lines)
    if total != 0:
        raise ValueError(f"journal is not balanced, sum is {total}")


def transfer_debit_lines(member_id: UUID, clearing_id: UUID, points: int) -> list[LedgerLine]:
    """Lines that take points from a member and park them in the program's clearing account.

    Args:
        member_id: Member account losing the points.
        clearing_id: Clearing account of the same program, holding points sent to partners.
        points: Positive number of source points.

    Returns:
        Two balanced lines: member -points, clearing +points.
    """
    return [LedgerLine(member_id, -points), LedgerLine(clearing_id, points)]


def compensation_lines(member_id: UUID, clearing_id: UUID, points: int) -> list[LedgerLine]:
    """Lines that undo a transfer debit: points go back from clearing to the member.

    This is the saga's compensating action. It is a new journal, not a delete, so the
    history shows both the debit and the refund.

    Args:
        member_id: Member account getting the points back.
        clearing_id: Clearing account that held the points.
        points: Positive number of source points to return.

    Returns:
        Two balanced lines: clearing -points, member +points.
    """
    return [LedgerLine(clearing_id, -points), LedgerLine(member_id, points)]


def funding_lines(issuance_id: UUID, member_id: UUID, points: int) -> list[LedgerLine]:
    """Lines that give a member starting points, taken from the program's issuance account.

    Args:
        issuance_id: The program's issuance account (allowed to go negative).
        member_id: Member account receiving the points.
        points: Positive number of points.

    Returns:
        Two balanced lines: issuance -points, member +points.
    """
    return [LedgerLine(issuance_id, -points), LedgerLine(member_id, points)]
