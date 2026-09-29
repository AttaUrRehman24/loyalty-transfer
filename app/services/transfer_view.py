"""The public JSON shape of a transfer.

Where it fits: the services layer. The same shape is returned by `POST /transfers`,
`GET /transfers/{id}` and `GET /accounts/{id}/transfers`, and it is what gets stored for
idempotent replay. Keeping it in one function means a replay is byte-for-byte the same
body the client saw the first time.
"""

from __future__ import annotations

from app.domain.models import Transfer


def transfer_view(transfer: Transfer) -> dict[str, object]:
    """Convert a transfer into a JSON-ready dict with only strings, ints and nulls.

    Args:
        transfer: The transfer to show.

    Returns:
        A plain dict safe to store as JSONB and to send as an HTTP body.
    """
    return {
        "id": str(transfer.id),
        "status": transfer.status.value,
        "idempotency_key": transfer.idempotency_key,
        "source_account_id": str(transfer.source_account_id),
        "source_program": transfer.source_program,
        "destination_program": transfer.destination_program,
        "destination_member_id": transfer.destination_member_id,
        "source_points": transfer.source_points,
        "destination_points": transfer.destination_points,
        "rate_id": str(transfer.rate_id),
        "partner_reference": transfer.partner_reference,
        "failure_reason": transfer.failure_reason,
        "created_at": transfer.created_at.isoformat(),
        "updated_at": transfer.updated_at.isoformat(),
    }
