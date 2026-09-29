"""Bounded retries with exponential backoff and jitter for partner calls.

Where it fits: the services layer. The orchestrator wraps each partner credit call with
`call_with_retry`. Only PartnerUnavailable (5xx or connection failure) is retried.
A timeout is never retried here: it means "outcome unknown", and the reconciler owns it.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.errors import PartnerUnavailable
from app.observability import log_event

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to try and how long to wait between tries.

    Attributes:
        max_attempts: Total tries including the first one. 1 means no retry.
        base_delay_seconds: Wait cap before the 2nd try; doubles for each later try.
        max_delay_seconds: Upper bound for any single wait.
    """

    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float


def backoff_delay(policy: RetryPolicy, failed_attempt: int, rng: random.Random) -> float:
    """Return how long to wait after a failed attempt ("full jitter" backoff).

    Business rule: the cap grows 2x per attempt (base, 2x base, 4x base, ...) up to
    `max_delay_seconds`, and the actual wait is a random value between 0 and that cap.
    The randomness stops many clients from retrying at the same instant.

    Args:
        policy: The retry policy.
        failed_attempt: Number of the attempt that just failed, starting at 1.
        rng: Random source, injected so tests are repeatable.

    Returns:
        Seconds to sleep, between 0 and the capped exponential value.
    """
    cap = min(policy.max_delay_seconds, policy.base_delay_seconds * (2 ** (failed_attempt - 1)))
    return rng.uniform(0.0, cap)


def call_with_retry[T](
    operation: Callable[[], T],
    policy: RetryPolicy,
    sleep: Callable[[float], None],
    rng: random.Random,
) -> T:
    """Run `operation`, retrying only on PartnerUnavailable, up to `policy.max_attempts`.

    Args:
        operation: The partner call to run. Takes no arguments.
        policy: Attempt limit and backoff settings.
        sleep: Function used to wait (time.sleep in production).
        rng: Random source for jitter.

    Returns:
        Whatever `operation` returns on its first successful try.

    Raises:
        PartnerUnavailable: If every attempt failed with it (the last error is raised).
        PartnerTimeout, PartnerRejected: Immediately, without retry.
    """
    attempt = 1
    while True:
        try:
            return operation()
        except PartnerUnavailable as error:
            if attempt >= policy.max_attempts:
                raise
            delay = backoff_delay(policy, attempt, rng)
            log_event(
                logger,
                "partner.retry",
                attempt=attempt,
                delay_seconds=round(delay, 3),
                reason=str(error),
            )
            sleep(delay)
            attempt += 1
