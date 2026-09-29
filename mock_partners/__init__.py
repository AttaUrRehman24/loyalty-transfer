"""Mock partner services used by docker compose and the integration tests.

Two small FastAPI apps: a card rewards program and an airline program. Each can simulate
latency, timeouts, 5xx errors and duplicate responses through config flags (env vars in
docker compose, or the `MockBehavior` object in tests). They are test doubles, not part
of the transfer engine.
"""
