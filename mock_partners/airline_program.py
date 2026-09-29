"""Mock airline frequent-flyer partner (see app/infra/partners/airline_program.py).

Run: `uvicorn mock_partners.airline_program:create_app --factory --port 9002`.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from mock_partners.common import CreditOutcome, CreditStore, MockBehavior, process_credit


class AccrualBody(BaseModel):
    """Request body of POST /api/miles/accruals (camelCase, like many airline APIs)."""

    model_config = ConfigDict(populate_by_name=True)

    external_ref: str = Field(alias="externalRef", min_length=1)
    frequent_flyer_number: str = Field(alias="frequentFlyerNumber", min_length=1)
    miles: int = Field(gt=0)


def create_app(behavior: MockBehavior | None = None, store: CreditStore | None = None) -> FastAPI:
    """Build the mock airline partner.

    Args:
        behavior: Failure flags; read from MOCK_* env vars when None.
        store: Credit store; a new empty one when None.

    Returns:
        The FastAPI app. `app.state.behavior` and `app.state.store` expose both objects.
    """
    app = FastAPI(title="Mock airline partner")
    app.state.behavior = behavior or MockBehavior.from_env()
    app.state.store = store or CreditStore()

    @app.post("/api/miles/accruals")
    async def accrue(body: AccrualBody, request: Request) -> JSONResponse:
        """Add miles once per externalRef, with simulated failures."""
        outcome, record = await process_credit(
            request.app.state.behavior,
            request.app.state.store,
            body.external_ref,
            body.frequent_flyer_number,
            body.miles,
        )
        if outcome is CreditOutcome.UNAVAILABLE or record is None:
            return JSONResponse(status_code=503, content={"code": "UNAVAILABLE"})
        if outcome is CreditOutcome.DUPLICATE:
            return JSONResponse(
                status_code=409,
                content={"code": "ALREADY_PROCESSED", "accrualId": record.reference},
            )
        return JSONResponse(status_code=201, content={"accrualId": record.reference})

    @app.get("/api/miles/accruals/{external_ref}")
    async def accrual_status(external_ref: str, request: Request) -> JSONResponse:
        """Return 200 with the accrual if miles were added, else 404."""
        record = request.app.state.store.get(external_ref)
        if record is None:
            return JSONResponse(status_code=404, content={"code": "NOT_FOUND"})
        return JSONResponse(status_code=200, content={"accrualId": record.reference})

    return app
