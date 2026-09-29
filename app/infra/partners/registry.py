"""Adapter registry and the PartnerDirectory implementation.

Where it fits: the infra layer. Adapter modules register themselves by name with
`@register_adapter`. `HttpPartnerDirectory` looks up the adapter named in a program's
`adapter` column and builds it with that program's `base_url`.
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from collections.abc import Callable
from typing import TypeVar

from app.domain.models import Program
from app.domain.ports import PartnerAdapter
from app.infra.partners.http import PartnerHttpClient

AdapterFactory = Callable[[PartnerHttpClient], PartnerAdapter]
F = TypeVar("F", bound=AdapterFactory)

_REGISTRY: dict[str, AdapterFactory] = {}


def register_adapter(name: str) -> Callable[[F], F]:
    """Class decorator that makes an adapter available under `name`.

    Args:
        name: Value to put in `programs.adapter` to use this adapter.

    Returns:
        A decorator that registers the class and returns it unchanged.

    Raises:
        ValueError: If two adapters use the same name.
    """

    def decorator(factory: F) -> F:
        """Store `factory` under `name` and hand it back unchanged."""
        if name in _REGISTRY and _REGISTRY[name] is not factory:
            raise ValueError(f"adapter name {name!r} is already registered")
        _REGISTRY[name] = factory
        return factory

    return decorator


def load_adapter_modules() -> None:
    """Import every module in `app.infra.partners` so their decorators run."""
    package = importlib.import_module("app.infra.partners")
    for module in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{module.name}")


class HttpPartnerDirectory:
    """Builds and caches one adapter per program."""

    def __init__(self, timeout_seconds: float) -> None:
        """Load all adapter modules and prepare the cache.

        Args:
            timeout_seconds: Timeout given to every partner HTTP client.
        """
        load_adapter_modules()
        self._timeout_seconds = timeout_seconds
        self._adapters: dict[tuple[str, str], PartnerAdapter] = {}
        # Requests run on many threads; the lock stops two of them building the same
        # adapter at once.
        self._lock = threading.Lock()

    def adapter_for(self, program: Program) -> PartnerAdapter:
        """Return the cached adapter for a program, creating it on first use.

        The cache key includes the base URL, so changing a program's URL in the database
        takes effect without a restart.

        Raises:
            LookupError: If no adapter is registered under `program.adapter`.
        """
        cache_key = (program.code, program.base_url)
        with self._lock:
            adapter = self._adapters.get(cache_key)
            if adapter is None:
                factory = _REGISTRY.get(program.adapter)
                if factory is None:
                    raise LookupError(f"no partner adapter named {program.adapter!r}")
                adapter = factory(PartnerHttpClient(program.base_url, self._timeout_seconds))
                self._adapters[cache_key] = adapter
            return adapter

    def close(self) -> None:
        """Close every adapter's HTTP connections."""
        with self._lock:
            for adapter in self._adapters.values():
                adapter.close()
            self._adapters.clear()
