"""POST /quotes: preview a conversion without moving any points."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import Services
from app.api.schemas import ErrorResponse, QuoteRequest, QuoteResponse

router = APIRouter(tags=["quotes"])


@router.post(
    "/quotes",
    response_model=QuoteResponse,
    responses={422: {"model": ErrorResponse}},
)
def create_quote(body: QuoteRequest, services: Services) -> QuoteResponse:
    """Return how many destination points `source_points` would buy right now.

    Errors: 422 if no rate is effective or the amount breaks min/max/increment rules.
    """
    quote = services.rates.quote(body.source_program, body.destination_program, body.source_points)
    return QuoteResponse.from_domain(quote)
