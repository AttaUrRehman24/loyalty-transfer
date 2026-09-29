"""Background worker entry point: outbox relay + reconciler loop.

Where it fits: the outermost layer, run as its own process (`python -m app.worker`).
Every poll interval it (1) dispatches outbox jobs left behind by crashed requests and
(2) settles UNKNOWN transfers by asking partners. It stops cleanly on SIGTERM or SIGINT.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from app.bootstrap import Runtime, build_runtime
from app.config import Settings
from app.observability import configure_logging, log_event

logger = logging.getLogger(__name__)


def run_cycle(runtime: Runtime) -> None:
    """Run one relay pass and one reconcile pass.

    Errors are logged and swallowed so one bad cycle never kills the worker; the next
    cycle simply tries again.
    """
    try:
        dispatched = runtime.relay.run_once()
        resolved = runtime.reconciler.run_once()
        if dispatched or resolved:
            log_event(logger, "worker.cycle", dispatched=dispatched, resolved=resolved)
    except Exception:
        logger.exception("worker.cycle_failed")


def main() -> None:
    """Start the worker loop and block until a stop signal arrives."""
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    runtime = build_runtime(settings)
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        """Signal handler: finish the current cycle, then exit."""
        log_event(logger, "worker.stopping", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    log_event(logger, "worker.started", poll_interval=settings.worker_poll_interval_seconds)
    try:
        while not stop.is_set():
            run_cycle(runtime)
            # Event.wait instead of time.sleep so a stop signal wakes us immediately.
            stop.wait(settings.worker_poll_interval_seconds)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
