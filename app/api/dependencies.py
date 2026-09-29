"""FastAPI dependency that hands routes the service container.

Where it fits: the API layer. `app.main` stores the container on `app.state.services` at
startup; routes receive it through `Services`.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from app.services.container import ServiceContainer


def get_services(request: Request) -> ServiceContainer:
    """Return the ServiceContainer stored on the app at startup."""
    return cast(ServiceContainer, request.app.state.services)


Services = Annotated[ServiceContainer, Depends(get_services)]
