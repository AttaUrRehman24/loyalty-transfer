"""GET /rates, POST /rates and PUT /rates/{id}: manage versioned conversion rates."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.dependencies import Services
from app.api.schemas import (
    ErrorResponse,
    ProgramCode,
    RateCreateRequest,
    RateResponse,
    RateUpdateRequest,
)

router = APIRouter(tags=["rates"])


@router.get("/rates", response_model=list[RateResponse])
def list_rates(
    services: Services,
    source_program: Annotated[ProgramCode | None, Query()] = None,
    destination_program: Annotated[ProgramCode | None, Query()] = None,
) -> list[RateResponse]:
    """List every rate version, optionally filtered by direction."""
    rates = services.rates.list_rates(source_program, destination_program)
    return [RateResponse.from_domain(rate) for rate in rates]


@router.post(
    "/rates",
    status_code=201,
    response_model=RateResponse,
    responses={409: {"model": ErrorResponse}},
)
def create_rate(body: RateCreateRequest, services: Services) -> RateResponse:
    """Create a rate for one direction.

    Errors: 409 if it overlaps an existing version, programs are unknown, or max < min.
    """
    rate = services.rates.create_rate(
        body.source_program, body.destination_program, body.to_terms(), body.effective_from
    )
    return RateResponse.from_domain(rate)


@router.put(
    "/rates/{rate_id}",
    response_model=RateResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def replace_rate(rate_id: UUID, body: RateUpdateRequest, services: Services) -> RateResponse:
    """Close the given (current) version and return the new version that replaces it.

    Errors: 404 unknown rate; 409 if that version is already closed or the new start
    time is in the past or not after the current version's start.
    """
    rate = services.rates.replace_rate(rate_id, body.to_terms(), body.effective_from)
    return RateResponse.from_domain(rate)
