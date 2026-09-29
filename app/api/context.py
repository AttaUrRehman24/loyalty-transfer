"""Request context middleware.

Where it fits: the API layer. For every HTTP request it puts the Idempotency-Key header
(if present) into the logging context, so all log lines written while serving the request
carry it, and it writes one summary log line when the request ends.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import idempotency_key_var, log_event, transfer_id_var

logger = logging.getLogger("app.api.access")


class RequestContextMiddleware:
    """Plain ASGI middleware (not BaseHTTPMiddleware) so context vars reach the routes."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the next ASGI app."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Set the log context, call the app, then log method, path, status and duration."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        raw_key = headers.get(b"idempotency-key")
        key_token = idempotency_key_var.set(raw_key.decode("latin-1") if raw_key else None)
        # Start every request with no transfer id; services set it when they know it.
        transfer_token = transfer_id_var.set(None)
        status_code = 500
        started = time.perf_counter()

        async def send_and_capture(message: Message) -> None:
            """Remember the status code as the response starts."""
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, send_and_capture)
        finally:
            log_event(
                logger,
                "http.request",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            transfer_id_var.reset(transfer_token)
            idempotency_key_var.reset(key_token)
