"""Adapter for the credit card rewards partner API.

Where it fits: the infra layer. Used for programs whose `adapter` column is
"card_program". The partner's contract:

    POST /v1/points/credits {request_id, member_id, points}
        201 {credit_id}                              -> credit applied
        409 {error: "DUPLICATE_REQUEST", credit_id}  -> already applied earlier
    GET  /v1/points/credits/{request_id}
        200 {credit_id}                              -> applied
        404                                          -> never applied
"""

from __future__ import annotations

from uuid import UUID

from app.domain.models import (
    PartnerCreditRequest,
    PartnerCreditResult,
    PartnerCreditState,
    PartnerCreditStatus,
)
from app.infra.partners.http import PartnerHttpClient, raise_rejected
from app.infra.partners.registry import register_adapter


@register_adapter("card_program")
class CardProgramAdapter:
    """Credits points into a card rewards account."""

    def __init__(self, client: PartnerHttpClient) -> None:
        """Store the HTTP client."""
        self._client = client

    def credit(self, request: PartnerCreditRequest) -> PartnerCreditResult:
        """Ask the card partner to add points.

        Our transfer id is sent as `request_id`, so the partner applies it only once.
        A "duplicate" answer means an earlier try already worked, so it counts as success.

        Raises:
            PartnerTimeout, PartnerUnavailable, PartnerRejected: See PartnerHttpClient.
        """
        response = self._client.request(
            "POST",
            "/v1/points/credits",
            json={
                "request_id": str(request.transfer_id),
                "member_id": request.member_id,
                "points": request.points,
            },
        )
        body = response.json() if response.content else {}
        if response.status_code == 201:
            return PartnerCreditResult(reference=str(body["credit_id"]))
        if response.status_code == 409 and body.get("error") == "DUPLICATE_REQUEST":
            return PartnerCreditResult(reference=str(body["credit_id"]))
        raise_rejected(response)

    def credit_status(self, transfer_id: UUID) -> PartnerCreditStatus:
        """Ask whether the credit for this transfer was applied.

        Raises:
            PartnerTimeout, PartnerUnavailable, PartnerRejected: See PartnerHttpClient.
        """
        response = self._client.request("GET", f"/v1/points/credits/{transfer_id}")
        if response.status_code == 200:
            reference = str(response.json()["credit_id"])
            return PartnerCreditStatus(PartnerCreditState.COMPLETED, reference)
        if response.status_code == 404:
            return PartnerCreditStatus(PartnerCreditState.NOT_FOUND, None)
        raise_rejected(response)

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()
