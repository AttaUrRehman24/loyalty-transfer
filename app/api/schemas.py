"""Request and response bodies for every endpoint (Pydantic v2 models).

Where it fits: the API layer. These models validate input before any service runs and
document the API in the generated OpenAPI spec. Unknown fields are rejected so typos in
client requests fail loudly instead of being ignored.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.models import Quote, Rate, RateTerms, TransferCommand

# Program codes: 2-32 upper-case letters, digits or underscores, e.g. "BANKCARD".
ProgramCode = Annotated[str, Field(pattern=r"^[A-Z0-9_]{2,32}$")]
# Points are whole numbers. The upper bound keeps values far inside Postgres BIGINT.
Points = Annotated[int, Field(gt=0, le=1_000_000_000_000)]


class StrictModel(BaseModel):
    """Base for request bodies: extra fields are an error."""

    model_config = ConfigDict(extra="forbid")


class ErrorDetail(BaseModel):
    """The inside of every error response."""

    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable explanation.")
    details: object | None = Field(default=None, description="Optional extra data.")


class ErrorResponse(BaseModel):
    """The one error shape used by every endpoint: {"error": {code, message, details}}."""

    error: ErrorDetail


class QuoteRequest(StrictModel):
    """Body of POST /quotes."""

    source_program: ProgramCode
    destination_program: ProgramCode
    source_points: Points


class QuoteResponse(BaseModel):
    """What a conversion would give right now."""

    source_program: str
    destination_program: str
    source_points: int
    destination_points: int
    rate_id: UUID
    rate_version: int
    ratio: Decimal

    @classmethod
    def from_domain(cls, quote: Quote) -> QuoteResponse:
        """Build the response from a domain Quote."""
        return cls(
            source_program=quote.rate.source_program,
            destination_program=quote.rate.destination_program,
            source_points=quote.source_points,
            destination_points=quote.destination_points,
            rate_id=quote.rate.id,
            rate_version=quote.rate.version,
            ratio=quote.rate.ratio,
        )


class TransferRequest(StrictModel):
    """Body of POST /transfers."""

    source_account_id: UUID
    destination_program: ProgramCode
    destination_member_id: str = Field(min_length=1, max_length=64)
    source_points: Points

    def to_command(self) -> TransferCommand:
        """Convert to the domain command."""
        return TransferCommand(
            source_account_id=self.source_account_id,
            destination_program=self.destination_program,
            destination_member_id=self.destination_member_id,
            source_points=self.source_points,
        )


class TransferResponse(BaseModel):
    """A transfer as returned by every transfer endpoint (see services.transfer_view)."""

    id: UUID
    status: str = Field(description="DEBITED, UNKNOWN, COMPLETED or COMPENSATED.")
    idempotency_key: str
    source_account_id: UUID
    source_program: str
    destination_program: str
    destination_member_id: str
    source_points: int
    destination_points: int
    rate_id: UUID
    partner_reference: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class RateTermsBody(StrictModel):
    """The editable numbers of a rate."""

    # Up to 12 digits before and 6 after the decimal point, matching NUMERIC(18,6).
    ratio: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    min_points: Points
    max_points: Points
    increment: Points
    effective_from: AwareDatetime | None = Field(
        default=None, description="When this version starts. Defaults to now."
    )

    def to_terms(self) -> RateTerms:
        """Convert to domain RateTerms."""
        return RateTerms(
            ratio=self.ratio,
            min_points=self.min_points,
            max_points=self.max_points,
            increment=self.increment,
        )


class RateCreateRequest(RateTermsBody):
    """Body of POST /rates."""

    source_program: ProgramCode
    destination_program: ProgramCode


class RateUpdateRequest(RateTermsBody):
    """Body of PUT /rates/{rate_id}: creates the next version of that rate."""


class RateResponse(BaseModel):
    """One rate version."""

    id: UUID
    source_program: str
    destination_program: str
    version: int
    ratio: Decimal
    min_points: int
    max_points: int
    increment: int
    effective_from: datetime
    effective_to: datetime | None

    @classmethod
    def from_domain(cls, rate: Rate) -> RateResponse:
        """Build the response from a domain Rate."""
        return cls(
            id=rate.id,
            source_program=rate.source_program,
            destination_program=rate.destination_program,
            version=rate.version,
            ratio=rate.ratio,
            min_points=rate.min_points,
            max_points=rate.max_points,
            increment=rate.increment,
            effective_from=rate.effective_from,
            effective_to=rate.effective_to,
        )


class HealthResponse(BaseModel):
    """Result of GET /health."""

    status: str = Field(description='"ok" when every dependency works, else "degraded".')
    checks: dict[str, str]
