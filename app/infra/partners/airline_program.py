"""Adapter for the airline frequent-flyer partner API.

Where it fits: the infra layer. Used for programs whose `adapter` column is
"airline_program". The partner's contract:

    POST /api/miles/accruals {externalRef, frequentFlyerNumber, miles}
        201 {accrualId}                            -> miles added
        409 {code: "ALREADY_PROCESSED", accrualId} -> already added earlier
    GET  /api/miles/accruals/{externalRef}
        200 {accrualId}                            -> added
        404                                        -> never added
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


@register_adapter("airline_program")
class AirlineProgramAdapter:
    """Credits miles into a frequent-flyer account."""

    def __init__(self, client: PartnerHttpClient) -> None:
        """Store the HTTP client."""
        self._client = client

    def credit(self, request: PartnerCreditRequest) -> PartnerCreditResult:
        """Ask the airline to add miles.

        Our transfer id is sent as `externalRef`, so the airline applies it only once.
        "ALREADY_PROCESSED" means an earlier try already worked, so it counts as success.

        Raises:
            PartnerTimeout, PartnerUnavailable, PartnerRejected: See PartnerHttpClient.
        """
        response = self._client.request(
            "POST",
            "/api/miles/accruals",
            json={
                "externalRef": str(request.transfer_id),
                "frequentFlyerNumber": request.member_id,
                "miles": request.points,
            },
        )
        body = response.json() if response.content else {}
        if response.status_code == 201:
            return PartnerCreditResult(reference=str(body["accrualId"]))
        if response.status_code == 409 and body.get("code") == "ALREADY_PROCESSED":
            return PartnerCreditResult(reference=str(body["accrualId"]))
        raise_rejected(response)

    def credit_status(self, transfer_id: UUID) -> PartnerCreditStatus:
        """Ask whether the miles for this transfer were added.

        Raises:
            PartnerTimeout, PartnerUnavailable, PartnerRejected: See PartnerHttpClient.
        """
        response = self._client.request("GET", f"/api/miles/accruals/{transfer_id}")
        if response.status_code == 200:
            reference = str(response.json()["accrualId"])
            return PartnerCreditStatus(PartnerCreditState.COMPLETED, reference)
        if response.status_code == 404:
            return PartnerCreditStatus(PartnerCreditState.NOT_FOUND, None)
        raise_rejected(response)

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()
