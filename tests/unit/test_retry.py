"""Unit tests for bounded retries with exponential backoff and jitter."""

from __future__ import annotations

import random

import pytest

from app.domain.errors import PartnerRejected, PartnerTimeout, PartnerUnavailable
from app.services.retry import RetryPolicy, backoff_delay, call_with_retry

POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0.1, max_delay_seconds=0.25)


class FlakyCall:
    """Fails with the given errors in order, then returns "ok"."""

    def __init__(self, *errors: Exception) -> None:
        """Remember which errors to raise, in order, before succeeding."""
        self.errors = list(errors)
        self.calls = 0

    def __call__(self) -> str:
        """Count the call, raise the next queued error, or return "ok" when none remain."""
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def test_backoff_cap_doubles_and_is_bounded() -> None:
    # Jitter stays within [0, cap]; cap = 0.1, 0.2, then limited to 0.25.
    rng = random.Random(7)
    for attempt, cap in [(1, 0.1), (2, 0.2), (3, 0.25), (6, 0.25)]:
        for _ in range(200):
            assert 0.0 <= backoff_delay(POLICY, attempt, rng) <= cap


def test_retries_unavailable_then_succeeds() -> None:
    # Two 5xx then success -> three calls, two sleeps, result returned.
    call = FlakyCall(PartnerUnavailable("503"), PartnerUnavailable("503"))
    sleeps: list[float] = []

    assert call_with_retry(call, POLICY, sleeps.append, random.Random(1)) == "ok"
    assert call.calls == 3
    assert len(sleeps) == 2


def test_gives_up_after_max_attempts() -> None:
    # Always 5xx -> exactly max_attempts calls, then the last error is raised.
    call = FlakyCall(*(PartnerUnavailable("503") for _ in range(5)))

    with pytest.raises(PartnerUnavailable):
        call_with_retry(call, POLICY, lambda _: None, random.Random(1))
    assert call.calls == 3


@pytest.mark.parametrize("error", [PartnerTimeout("slow"), PartnerRejected("400")])
def test_timeouts_and_rejections_are_not_retried(error: Exception) -> None:
    # Timeout = unknown outcome (reconciler's job); 4xx = will never succeed. One call only.
    call = FlakyCall(error)

    with pytest.raises(type(error)):
        call_with_retry(call, POLICY, lambda _: None, random.Random(1))
    assert call.calls == 1
