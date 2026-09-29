"""Structured JSON logging with per-request context.

Where it fits: a small cross-cutting helper used by every layer. It is not part of the
layer stack, so domain rules stay free of it.

Every log line is one JSON object and always has the keys `transfer_id` and
`idempotency_key` (null when not known yet). The values come from context variables,
so code does not have to pass them to every log call.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import UUID

transfer_id_var: ContextVar[str | None] = ContextVar("transfer_id", default=None)
idempotency_key_var: ContextVar[str | None] = ContextVar("idempotency_key", default=None)


class JsonFormatter(logging.Formatter):
    """Formats a log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        """Build the JSON line for one log record.

        Args:
            record: The standard library log record.

        Returns:
            A JSON string with timestamp, level, logger, message, the two context ids,
            any extra `fields` passed through `log_event`, and the exception if present.
        """
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "transfer_id": transfer_id_var.get(),
            "idempotency_key": idempotency_key_var.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str keeps logging from crashing on values like UUID or Decimal.
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    """Send all logs, including uvicorn's, to stdout as JSON lines.

    Args:
        level: Standard level name such as "INFO" or "DEBUG".
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Uvicorn adds its own handlers; remove them so its lines also go through our formatter.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    # Silence uvicorn's plain-text access log: our middleware writes one JSON line per
    # request instead, with the idempotency key attached.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False


def log_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Write an INFO log line with an event name and extra structured fields.

    Args:
        logger: The module's logger.
        event: Short dotted name, for example "transfer.completed".
        **fields: Extra key/value pairs added to the JSON line.
    """
    logger.info(event, extra={"fields": fields})


@contextmanager
def log_context(
    *, transfer_id: UUID | str | None = None, idempotency_key: str | None = None
) -> Iterator[None]:
    """Attach a transfer id and/or idempotency key to every log line inside the block.

    Only the values that are passed are changed; the others keep their current value.
    Both are restored when the block ends, even on error.

    Args:
        transfer_id: Transfer being worked on, if known.
        idempotency_key: Client idempotency key, if known.

    Yields:
        Nothing. Use as `with log_context(transfer_id=...):`.
    """
    tokens = []
    if transfer_id is not None:
        tokens.append((transfer_id_var, transfer_id_var.set(str(transfer_id))))
    if idempotency_key is not None:
        tokens.append((idempotency_key_var, idempotency_key_var.set(idempotency_key)))
    try:
        yield
    finally:
        # Reset in reverse order so nested blocks restore exactly what they found.
        for var, token in reversed(tokens):
            var.reset(token)
