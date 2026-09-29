"""Shared HTTP client for partner adapters.

Where it fits: the infra layer. Adapters send requests through `PartnerHttpClient`, which
turns HTTP-library problems into the domain's partner errors. That way the saga only
knows three failure kinds: timeout, unavailable (retryable) and rejected (final).
"""

from __future__ import annotations

from typing import Any, NoReturn

import httpx

from app.domain.errors import PartnerRejected, PartnerTimeout, PartnerUnavailable


class PartnerHttpClient:
    """Synchronous JSON-over-HTTP client with a hard timeout."""

    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        """Create the underlying connection-pooled client.

        Args:
            base_url: Partner API root, for example http://card-partner:9001.
            timeout_seconds: Limit for connecting, sending and reading each request.
        """
        self._client = httpx.Client(base_url=base_url, timeout=timeout_seconds)

    def request(self, method: str, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        """Send one request and classify failures.

        Business rules:
        - Any timeout -> PartnerTimeout. We cannot know if the partner acted on it.
        - Connection errors and 5xx -> PartnerUnavailable (safe to retry).
        - Other responses (2xx, 4xx) are returned for the adapter to interpret.

        Args:
            method: HTTP method.
            path: Path under the base URL.
            json: Optional JSON body.

        Returns:
            The response, when the status code is below 500.

        Raises:
            PartnerTimeout: The request timed out.
            PartnerUnavailable: Connection failed or the partner returned 5xx.
        """
        try:
            response = self._client.request(method, path, json=json)
        except httpx.TimeoutException as error:
            raise PartnerTimeout(f"{method} {path} timed out") from error
        except httpx.TransportError as error:
            raise PartnerUnavailable(f"{method} {path} failed: {error}") from error
        if response.status_code >= 500:
            raise PartnerUnavailable(f"{method} {path} returned HTTP {response.status_code}")
        return response

    def close(self) -> None:
        """Close pooled connections."""
        self._client.close()


def raise_rejected(response: httpx.Response) -> NoReturn:
    """Raise PartnerRejected for a response the adapter does not accept.

    Only the first 200 characters of the body are kept, to keep logs small.

    Raises:
        PartnerRejected: Always.
    """
    raise PartnerRejected(f"HTTP {response.status_code}: {response.text[:200]}")
