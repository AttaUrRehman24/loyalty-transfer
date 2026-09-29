"""GET /health: is the service able to reach Postgres and Redis?"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.dependencies import Services
from app.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, responses={503: {"model": HealthResponse}})
def health(services: Services) -> JSONResponse:
    """Return 200 when every dependency answers, 503 otherwise (so load balancers react)."""
    checks = services.health.check()
    healthy = all(value == "ok" for value in checks.values())
    body = HealthResponse(status="ok" if healthy else "degraded", checks=checks)
    return JSONResponse(status_code=200 if healthy else 503, content=body.model_dump())
