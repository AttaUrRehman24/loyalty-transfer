"""Transfer state machine.

Where it fits: the domain layer. The orchestrator must call `transition` before changing a
transfer's status. Any move not listed in `ALLOWED_TRANSITIONS` raises, so a bug can never
silently move a finished transfer back to an open state.

    DEBITED --partner ok-------------> COMPLETED
    DEBITED --partner failed---------> COMPENSATED
    DEBITED --partner timed out------> UNKNOWN
    UNKNOWN --partner has the credit-> COMPLETED
    UNKNOWN --partner has no credit--> COMPENSATED
"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.errors import IllegalTransition
from app.domain.models import TransferStatus

ALLOWED_TRANSITIONS: Mapping[TransferStatus, frozenset[TransferStatus]] = {
    TransferStatus.DEBITED: frozenset(
        {TransferStatus.COMPLETED, TransferStatus.COMPENSATED, TransferStatus.UNKNOWN}
    ),
    # UNKNOWN can only be closed by the reconciler after it asked the partner.
    TransferStatus.UNKNOWN: frozenset({TransferStatus.COMPLETED, TransferStatus.COMPENSATED}),
    # Final states: nothing may follow them.
    TransferStatus.COMPLETED: frozenset(),
    TransferStatus.COMPENSATED: frozenset(),
}


def can_transition(current: TransferStatus, target: TransferStatus) -> bool:
    """Return True if moving from `current` to `target` is allowed."""
    return target in ALLOWED_TRANSITIONS[current]


def transition(current: TransferStatus, target: TransferStatus) -> TransferStatus:
    """Validate a status change and return the new status.

    Args:
        current: The transfer's status right now.
        target: The status we want to move to.

    Returns:
        `target`, so callers can write `status = transition(old, new)`.

    Raises:
        IllegalTransition: If the move is not in `ALLOWED_TRANSITIONS`.
    """
    if not can_transition(current, target):
        raise IllegalTransition(f"transfer cannot move from {current} to {target}")
    return target
