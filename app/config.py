"""Application settings, read from environment variables only (12-factor).

Where it fits: the outermost layer. `app.main`, `app.worker` and `scripts.seed` read
settings once at startup and pass plain values into the services. No other module reads
the environment. Every variable is documented in `.env.example`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    """Configuration for the API and worker processes.

    Each field is read from the environment variable with the same name in upper case.
    """

    model_config = ConfigDict(frozen=True)

    database_url: str
    redis_url: str
    db_pool_min_size: int = Field(default=2, ge=1)
    db_pool_max_size: int = Field(default=20, ge=1)
    partner_timeout_seconds: float = Field(default=3.0, gt=0)
    partner_max_attempts: int = Field(default=3, ge=1, le=10)
    partner_backoff_base_seconds: float = Field(default=0.2, ge=0)
    partner_backoff_max_seconds: float = Field(default=2.0, ge=0)
    idempotency_lock_ttl_seconds: int = Field(default=30, ge=1)
    outbox_lease_seconds: int = Field(default=30, ge=1)
    reconcile_min_age_seconds: int = Field(default=60, ge=0)
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    worker_batch_size: int = Field(default=50, ge=1)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _leases_cover_partner_calls(self) -> Settings:
        """Refuse settings where a lock or lease could expire during a partner call.

        Business rule: if the idempotency lock or outbox lease ran out while the partner
        call was still going, a second worker could start the same transfer. Partners
        de-duplicate, so it would still be safe, but it would be wasted work, so we fail
        fast at startup instead.
        """
        worst_case = (
            self.partner_max_attempts * self.partner_timeout_seconds
            + (self.partner_max_attempts - 1) * self.partner_backoff_max_seconds
        )
        if self.idempotency_lock_ttl_seconds <= worst_case:
            raise ValueError(
                f"IDEMPOTENCY_LOCK_TTL_SECONDS must exceed the worst partner call ({worst_case}s)"
            )
        if self.outbox_lease_seconds <= worst_case:
            raise ValueError(
                f"OUTBOX_LEASE_SECONDS must exceed the worst partner call ({worst_case}s)"
            )
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build settings from environment variables.

        Args:
            environ: Mapping to read from; defaults to `os.environ`.

        Returns:
            Validated settings.

        Raises:
            pydantic.ValidationError: If a required variable is missing or a value is
                invalid.
        """
        return cls.model_validate(_pick(cls, os.environ if environ is None else environ))


class SeedSettings(BaseModel):
    """Extra settings used only by `scripts/seed.py`."""

    model_config = ConfigDict(frozen=True)

    card_partner_url: str
    airline_partner_url: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SeedSettings:
        """Build seed settings from environment variables (see `Settings.from_env`)."""
        return cls.model_validate(_pick(cls, os.environ if environ is None else environ))


def _pick(model: type[BaseModel], environ: Mapping[str, str]) -> dict[str, str]:
    """Collect the env vars that match the model's field names (upper-cased)."""
    return {name: environ[name.upper()] for name in model.model_fields if name.upper() in environ}
