"""All HTTP routes, collected into one router that `app.main` mounts."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import accounts, health, quotes, rates, transfers

api_router = APIRouter()
api_router.include_router(quotes.router)
api_router.include_router(transfers.router)
api_router.include_router(accounts.router)
api_router.include_router(rates.router)
api_router.include_router(health.router)
