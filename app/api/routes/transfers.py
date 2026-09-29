"""POST /transfers and GET /transfers/{id}."""

from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app.api.dependencies import Services
from app.api.errors import error_response
from app.api.schemas import ErrorResponse, TransferRequest, TransferResponse
from app.services.transfer_view import transfer_view

router = APIRouter(tags=["transfers"])

# 1-255 visible ASCII characters: safe to log, store and use as a Redis key.
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{1,255}$")

# Business rule: a settled transfer (COMPLETED or COMPENSATED) is "created" -> 201.
# A transfer whose outcome is still open (DEBITED or UNKNOWN) is "accepted" -> 202.
_STATUS_CODE_BY_TRANSFER_STATUS = {
    "COMPLETED": 201,
    "COMPENSATED": 201,
    "DEBITED": 202,
    "UNKNOWN": 202,
}


@router.post(
    "/transfers",
    status_code=201,
    response_model=TransferResponse,
    responses={
        202: {"model": TransferResponse, "description": "Accepted, outcome not final yet."},
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
)
def create_transfer(
    body: TransferRequest,
    services: Services,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JSONResponse:
    """Execute a transfer exactly once per Idempotency-Key.

    The response status code is derived from the transfer status in the body, so a
    replayed response has the same status code as the original. Replays also carry the
    header `Idempotent-Replayed: true`.

    Errors: 400 missing/invalid key, 404 unknown account, 409 insufficient points or
    same key in flight, 422 rate rules or same key with a different body.
    """
    if idempotency_key is None or not _IDEMPOTENCY_KEY_PATTERN.match(idempotency_key):
        return error_response(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key header is required: 1-255 visible ASCII characters",
        )
    result = services.transfers.execute(idempotency_key, body.to_command())
    return JSONResponse(
        status_code=_STATUS_CODE_BY_TRANSFER_STATUS[str(result.body["status"])],
        content=result.body,
        headers={"Idempotent-Replayed": "true"} if result.replayed else None,
    )


@router.get(
    "/transfers/{transfer_id}",
    response_model=TransferResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_transfer(transfer_id: UUID, services: Services) -> JSONResponse:
    """Return the current state of one transfer. 404 if it does not exist.

    The shared `transfer_view` dict is sent as-is (not re-serialised through the
    response model), so this body is byte-for-byte the shape POST /transfers returns.
    """
    return JSONResponse(content=transfer_view(services.transfer_queries.get(transfer_id)))
