"""Unit tests for the transfer state machine and double-entry posting rules."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.domain.errors import IllegalTransition
from app.domain.ledger import compensation_lines, ensure_balanced, transfer_debit_lines
from app.domain.models import LedgerLine, TransferStatus
from app.domain.state_machine import transition

S = TransferStatus


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.DEBITED, S.COMPLETED),
        (S.DEBITED, S.COMPENSATED),
        (S.DEBITED, S.UNKNOWN),
        (S.UNKNOWN, S.COMPLETED),
        (S.UNKNOWN, S.COMPENSATED),
    ],
)
def test_allowed_transitions(current: TransferStatus, target: TransferStatus) -> None:
    # Every documented saga move is accepted and returns the new status.
    assert transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.COMPLETED, S.COMPENSATED),
        (S.COMPENSATED, S.COMPLETED),
        (S.COMPLETED, S.DEBITED),
        (S.UNKNOWN, S.DEBITED),
        (S.UNKNOWN, S.UNKNOWN),
        (S.DEBITED, S.DEBITED),
    ],
)
def test_illegal_transitions_raise(current: TransferStatus, target: TransferStatus) -> None:
    # Final states never change and nothing goes backwards -> IllegalTransition.
    with pytest.raises(IllegalTransition):
        transition(current, target)


def test_debit_and_compensation_cancel_out() -> None:
    # A debit followed by its refund leaves every account where it started.
    member, clearing = uuid4(), uuid4()
    lines = transfer_debit_lines(member, clearing, 700) + compensation_lines(member, clearing, 700)

    net: dict[object, int] = {}
    for line in lines:
        net[line.account_id] = net.get(line.account_id, 0) + line.amount

    assert net == {member: 0, clearing: 0}


def test_unbalanced_journal_is_rejected() -> None:
    # Lines that do not sum to zero would create or destroy points -> ValueError.
    with pytest.raises(ValueError, match="not balanced"):
        ensure_balanced([LedgerLine(uuid4(), -100), LedgerLine(uuid4(), 99)])


def test_single_line_journal_is_rejected() -> None:
    # Double entry needs at least two sides.
    with pytest.raises(ValueError, match="at least two"):
        ensure_balanced([LedgerLine(uuid4(), 0)])
