"""Run an ASGI app on a real local port in a background thread.

Integration tests talk to the engine and the mock partners over real HTTP, so timeouts,
concurrency and connection handling behave as they do in production.
"""

from __future__ import annotations

import socket
import threading
import time

import uvicorn
from fastapi import FastAPI


class BackgroundServer:
    """A uvicorn server bound to a free port on 127.0.0.1."""

    def __init__(self, app: FastAPI) -> None:
        """Reserve a free port now so the URL is known before the server starts."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        port = self._socket.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            # Do not wait for stalled mock requests (timeout simulation) at shutdown.
            timeout_graceful_shutdown=1,
            # Enough concurrent connections for the 50-request idempotency test.
            limit_concurrency=500,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True
        )

    def start(self) -> BackgroundServer:
        """Start serving and wait (up to 10 s) until the server accepts requests.

        Raises:
            RuntimeError: If the server did not start in time.
        """
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        """Ask the server to exit and wait for its thread."""
        self._server.should_exit = True
        self._thread.join(timeout=10)
