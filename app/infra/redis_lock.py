"""Redis in-flight lock for idempotency keys.

Where it fits: the infra layer, implementing `app.domain.ports.InFlightLock`. Redis is used
only for this short lock. The durable record of every key lives in Postgres.
"""

from __future__ import annotations

from uuid import uuid4

import redis

# Delete the lock only if it still holds our token. Done as one Lua script so the check
# and the delete cannot be split by another client taking the lock in between.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class RedisInFlightLock:
    """SET NX based lock with an expiry and an owner token."""

    def __init__(self, client: redis.Redis, prefix: str = "idempotency-lock:") -> None:
        """Store the client and register the release script.

        Args:
            client: Connected Redis client.
            prefix: Namespace for lock keys so they cannot clash with other Redis data.
        """
        self._client = client
        self._prefix = prefix
        self._release = client.register_script(_RELEASE_SCRIPT)

    def acquire(self, key: str, ttl_seconds: int) -> str | None:
        """Try to take the lock for `key`.

        The lock expires after `ttl_seconds` on its own, so a crashed process can never
        block a key forever.

        Args:
            key: The idempotency key.
            ttl_seconds: Lock lifetime.

        Returns:
            A random owner token if we got the lock, otherwise None.
        """
        token = uuid4().hex
        acquired = self._client.set(self._prefix + key, token, nx=True, ex=ttl_seconds)
        return token if acquired else None

    def release(self, key: str, token: str) -> None:
        """Release the lock if `token` still owns it.

        If our lock already expired and someone else took it, their lock is left alone.
        """
        self._release(keys=[self._prefix + key], args=[token])
