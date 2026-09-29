"""GET /accounts/{id}/transfers: an account's transfer history."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.api.dependencies import Services
from app.api.schemas import ErrorResponse, TransferResponse
from app.services.transfer_view import transfer_view

router = APIRouter(tags=["accounts"])


@router.get(
    "/accounts/{account_id}/transfers",
    response_model=list[TransferResponse],
    responses={404: {"model": ErrorResponse}},
)
def list_account_transfers(
    account_id: UUID,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JSONResponse:
    """Return transfers debited from this account, newest first, one page at a time.

    Items use the same `transfer_view` shape as POST /transfers, sent as-is.
    Errors: 404 if the account does not exist.
    """
    transfers = services.transfer_queries.list_for_account(account_id, limit, offset)
    return JSONResponse(content=[transfer_view(transfer) for transfer in transfers])
