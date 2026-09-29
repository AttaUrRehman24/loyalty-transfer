"""One error schema and the mapping from errors to HTTP status codes.

Where it fits: the API layer. Every error, whether a business error, a validation error,
an unknown route or a crash, leaves the service as
`{"error": {"code": ..., "message": ..., "details": ...}}` with a fitting status code.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.schemas import ErrorDetail, ErrorResponse
from app.domain.errors import (
    ConversionRejected,
    DomainError,
    IdempotencyInProgress,
    IdempotencyKeyReused,
    InsufficientPoints,
    InvalidSourceAccount,
    NoEffectiveRate,
    NotFoundError,
    RateConflict,
)

logger = logging.getLogger(__name__)

# Most specific class wins because lookup walks each error's class hierarchy in order.
_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    NotFoundError: 404,
    # 422: the request is well-formed but the business rules reject it.
    NoEffectiveRate: 422,
    ConversionRejected: 422,
    InvalidSourceAccount: 422,
    IdempotencyKeyReused: 422,
    # 409: the request conflicts with the current state; it may work later.
    InsufficientPoints: 409,
    IdempotencyInProgress: 409,
    RateConflict: 409,
}


def error_response(
    status_code: int, code: str, message: str, details: object = None
) -> JSONResponse:
    """Build a JSON response in the shared error shape.

    Args:
        status_code: HTTP status.
        code: Stable error code.
        message: Human-readable text.
        details: Optional extra data (must be JSON-serialisable).

    Returns:
        The JSONResponse.
    """
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body))


def status_for(error: DomainError) -> int:
    """Return the HTTP status for a business error (500 if it is not mapped)."""
    for cls in type(error).__mro__:
        if cls in _STATUS_BY_ERROR:
            return _STATUS_BY_ERROR[cls]
    return 500


def register_error_handlers(app: FastAPI) -> None:
    """Attach handlers so every error uses the shared error shape."""

    @app.exception_handler(DomainError)
    async def handle_domain_error(_request: Request, error: DomainError) -> JSONResponse:
        """Business errors: mapped status, the error's own code and message."""
        return error_response(status_for(error), error.code, str(error))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        """Bad input: 422 with the list of field problems in `details`."""
        return error_response(
            422, "VALIDATION_ERROR", "request validation failed", jsonable_encoder(error.errors())
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        """Framework errors such as unknown route (404) or wrong method (405)."""
        return error_response(error.status_code, f"HTTP_{error.status_code}", str(error.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, error: Exception) -> JSONResponse:
        """Anything else: log the full error, but never leak internals to the client."""
        logger.error("request.unhandled_error", exc_info=error)
        return error_response(500, "INTERNAL_ERROR", "internal server error")
