"""FastAPI application factory (the API process entry point).

Where it fits: the outermost layer. Run with
`uvicorn app.main:create_app --factory`. It builds the runtime from env vars, attaches
the services to the app, and registers middleware, error handlers and routes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.context import RequestContextMiddleware
from app.api.errors import register_error_handlers
from app.api.routes import api_router
from app.bootstrap import Runtime, build_runtime
from app.config import Settings
from app.observability import configure_logging


def create_app(runtime: Runtime | None = None) -> FastAPI:
    """Build the FastAPI app.

    Args:
        runtime: A pre-built runtime (tests pass one and close it themselves). If None,
            the app builds its own from env vars at startup and closes it at shutdown.

    Returns:
        The configured FastAPI application.
    """

    # Read settings and switch to JSON logs now (not at startup) so even uvicorn's very
    # first "server started" lines are JSON.
    settings = Settings.from_env() if runtime is None else runtime.settings
    if runtime is None:
        configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Create the runtime on startup and close it on shutdown if we own it."""
        owned = runtime is None
        active = runtime or build_runtime(settings)
        app.state.services = active.services
        try:
            yield
        finally:
            if owned:
                active.close()

    app = FastAPI(title="Loyalty Points Transfer Engine", version="1.0.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(api_router)
    return app
