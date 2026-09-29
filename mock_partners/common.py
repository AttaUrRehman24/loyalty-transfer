"""Shared behaviour flags and in-memory credit store for both mock partners.

Where it fits: mock partners only. Both partner apps call `process_credit` so they fail,
stall and de-duplicate in exactly the same way; only their URLs and JSON field names differ.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4


class TimeoutMode(StrEnum):
    """How the mock behaves when asked to simulate a timeout.

    NONE: answer normally.
    APPLY: record the credit, then stall past the client's timeout (credit happened).
    DROP: stall past the client's timeout and do NOT record the credit (it never happened).
    """

    NONE = "none"
    APPLY = "apply"
    DROP = "drop"


@dataclass
class MockBehavior:
    """Failure-simulation flags. Tests change these on the live object between cases.

    Attributes:
        latency_ms: Extra delay added before every credit answer.
        fail_5xx: When True, every credit call returns HTTP 503.
        timeout_mode: See TimeoutMode.
        timeout_delay_ms: How long to stall in APPLY/DROP mode; set above client timeout.
        duplicate_responses: When True, a fresh credit is applied but answered with the
            partner's "duplicate" response, as some partners do on retried requests.
    """

    latency_ms: int = 0
    fail_5xx: bool = False
    timeout_mode: TimeoutMode = TimeoutMode.NONE
    timeout_delay_ms: int = 5_000
    duplicate_responses: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MockBehavior:
        """Read flags from MOCK_* env vars (documented in .env.example)."""
        env = os.environ if environ is None else environ
        return cls(
            latency_ms=int(env.get("MOCK_LATENCY_MS", "0")),
            fail_5xx=env.get("MOCK_FAIL_5XX", "false").lower() == "true",
            timeout_mode=TimeoutMode(env.get("MOCK_TIMEOUT_MODE", "none")),
            timeout_delay_ms=int(env.get("MOCK_TIMEOUT_DELAY_MS", "5000")),
            duplicate_responses=env.get("MOCK_DUPLICATE_RESPONSES", "false").lower() == "true",
        )

    def reset(self) -> None:
        """Go back to "everything works" (tests call this before each case)."""
        self.latency_ms = 0
        self.fail_5xx = False
        self.timeout_mode = TimeoutMode.NONE
        self.timeout_delay_ms = 5_000
        self.duplicate_responses = False


@dataclass(frozen=True)
class CreditRecord:
    """One applied credit."""

    reference: str
    member_id: str
    points: int


@dataclass
class CreditStore:
    """Applied credits keyed by the caller's request id, plus a call counter.

    A lock is used because tests read the store from another thread.
    """

    credits: dict[str, CreditRecord] = field(default_factory=dict)
    credit_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def count_call(self) -> None:
        """Count one credit request (used by tests to prove retries happened)."""
        with self._lock:
            self.credit_calls += 1

    def get(self, request_id: str) -> CreditRecord | None:
        """Return the credit for a request id, if applied."""
        with self._lock:
            return self.credits.get(request_id)

    def apply(self, request_id: str, member_id: str, points: int) -> CreditRecord:
        """Apply a credit once; a repeat returns the first record unchanged."""
        with self._lock:
            existing = self.credits.get(request_id)
            if existing is not None:
                return existing
            record = CreditRecord(uuid4().hex, member_id, points)
            self.credits[request_id] = record
            return record

    def reset(self) -> None:
        """Forget all credits and calls."""
        with self._lock:
            self.credits.clear()
            self.credit_calls = 0


class CreditOutcome(StrEnum):
    """What the partner app should answer."""

    CREATED = "CREATED"
    DUPLICATE = "DUPLICATE"
    UNAVAILABLE = "UNAVAILABLE"


async def process_credit(
    behavior: MockBehavior, store: CreditStore, request_id: str, member_id: str, points: int
) -> tuple[CreditOutcome, CreditRecord | None]:
    """Run the shared mock credit logic, including every simulated failure.

    Order matters: latency first, then 5xx, then de-duplication, then timeouts, so each
    flag behaves the same way in both partner apps.

    Returns:
        The outcome to answer with and the credit record (None when unavailable).
    """
    store.count_call()
    if behavior.latency_ms:
        await asyncio.sleep(behavior.latency_ms / 1000)
    if behavior.fail_5xx:
        return CreditOutcome.UNAVAILABLE, None
    existing = store.get(request_id)
    if existing is not None:
        # Real partners de-duplicate by request id: a retry never credits twice.
        return CreditOutcome.DUPLICATE, existing
    if behavior.timeout_mode is TimeoutMode.APPLY:
        record = store.apply(request_id, member_id, points)
        await asyncio.sleep(behavior.timeout_delay_ms / 1000)
        return CreditOutcome.CREATED, record
    if behavior.timeout_mode is TimeoutMode.DROP:
        await asyncio.sleep(behavior.timeout_delay_ms / 1000)
        return CreditOutcome.UNAVAILABLE, None
    record = store.apply(request_id, member_id, points)
    if behavior.duplicate_responses:
        return CreditOutcome.DUPLICATE, record
    return CreditOutcome.CREATED, record
