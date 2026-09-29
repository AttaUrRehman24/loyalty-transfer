"""Mock credit card rewards partner (see app/infra/partners/card_program.py for the contract).

Run: `uvicorn mock_partners.card_program:create_app --factory --port 9001`.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from mock_partners.common import CreditOutcome, CreditStore, MockBehavior, process_credit


class CreditBody(BaseModel):
    """Request body of POST /v1/points/credits."""

    request_id: str = Field(min_length=1)
    member_id: str = Field(min_length=1)
    points: int = Field(gt=0)


def create_app(behavior: MockBehavior | None = None, store: CreditStore | None = None) -> FastAPI:
    """Build the mock card partner.

    Args:
        behavior: Failure flags; read from MOCK_* env vars when None.
        store: Credit store; a new empty one when None.

    Returns:
        The FastAPI app. `app.state.behavior` and `app.state.store` expose both objects.
    """
    app = FastAPI(title="Mock card rewards partner")
    app.state.behavior = behavior or MockBehavior.from_env()
    app.state.store = store or CreditStore()

    @app.post("/v1/points/credits")
    async def credit(body: CreditBody, request: Request) -> JSONResponse:
        """Apply a credit once per request_id, with simulated failures."""
        outcome, record = await process_credit(
            request.app.state.behavior,
            request.app.state.store,
            body.request_id,
            body.member_id,
            body.points,
        )
        if outcome is CreditOutcome.UNAVAILABLE or record is None:
            return JSONResponse(status_code=503, content={"error": "SERVICE_UNAVAILABLE"})
        if outcome is CreditOutcome.DUPLICATE:
            return JSONResponse(
                status_code=409,
                content={"error": "DUPLICATE_REQUEST", "credit_id": record.reference},
            )
        return JSONResponse(status_code=201, content={"credit_id": record.reference})

    @app.get("/v1/points/credits/{request_id}")
    async def credit_status(request_id: str, request: Request) -> JSONResponse:
        """Return 200 with the credit if it was applied, else 404."""
        record = request.app.state.store.get(request_id)
        if record is None:
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND"})
        return JSONResponse(status_code=200, content={"credit_id": record.reference})

    return app
