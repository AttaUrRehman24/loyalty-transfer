# Decisions

Choices made where the brief was silent. Each is the simplest option that meets the scope.

- **Ledger model:** the engine keeps a local points ledger per member per program; the source side is debited locally, the destination side is the partner's API.
- **Account kinds:** MEMBER, CLEARING (points sent to partners) and ISSUANCE (source of seeded points, the only account allowed below zero).
- **Rate direction:** each direction is its own row; A→B never implies B→A.
- **Rate ratio:** `ratio` = destination points per one source point, `NUMERIC(18,6)`, applied as `floor(source × ratio)` by `round_down_points`.
- **Min, max, increment:** apply to source points; min and max are inclusive; source must be a whole multiple of `increment`; a result of 0 destination points is rejected.
- **Rate windows:** half-open `[effective_from, effective_to)`; `PUT /rates/{id}` closes the open version at the new start and inserts version N+1; the new start must not be in the past.
- **`PUT /rates/{id}` target:** only the currently open version can be replaced (409 otherwise).
- **Error status codes:** 404 not found, 409 insufficient points / key in flight / rate conflict, 422 rule violations and key reused with a different body, 400 missing or malformed Idempotency-Key.
- **Transfer status codes:** 201 when final (COMPLETED or COMPENSATED), 202 when open (UNKNOWN or DEBITED); the code is derived from the body so a replay gets the same code.
- **Replay marker:** replays add the header `Idempotent-Replayed: true`; the body is identical.
- **Concurrent same key:** requests that find the Redis lock taken get 409 `IDEMPOTENCY_IN_PROGRESS` immediately (no waiting).
- **Rejected requests are not stored:** if validation fails before a transfer is created (for example insufficient points), nothing is stored for the key and the client may retry it.
- **Missing first response:** if the first request crashed before storing its response, the first replay stores the transfer's current state and all later replays return that.
- **Request hash:** SHA-256 of the canonical JSON of the four body fields, keys sorted.
- **Partner 4xx:** treated as a final failure without retries, then compensated.
- **Partner "duplicate" answer:** counted as success; partners are called with the transfer id as their request id.
- **Outbox lease:** the job is inserted already leased to the creating request; the relay only takes jobs whose lease expired.
- **Reconciler age gate:** only UNKNOWN transfers older than `RECONCILE_MIN_AGE_SECONDS` are checked, so a slow partner can finish first.
- **Lock/lease sizing:** startup fails if the lock TTL or outbox lease does not exceed the worst-case partner call time.
- **Deadlock avoidance:** account rows are always locked in ascending id order.
- **Layer wiring:** domain defines Protocols (ports); infra implements them; `app/bootstrap.py` wires them. `app/observability.py` is a shared logging helper, not a layer.
- **Sync stack:** sync FastAPI routes (thread pool), sync psycopg and httpx; partner calls are synchronous with a timeout, as specified.
- **Timestamps:** DB sessions run in UTC; transfer timestamps come from the application clock.
- **Alembic needs SQLAlchemy:** SQLAlchemy is installed only because Alembic requires it; the service uses psycopg directly.
- **Settings loader:** a small Pydantic model reads env vars, instead of adding `pydantic-settings`.
- **Single image:** one Docker image (with dev tools) runs the API, worker, mocks, tests and lint.
- **Mock partner control:** flags come from `MOCK_*` env vars in compose; tests start the mocks in-process and change the same flag object directly.
- **Access log:** uvicorn's plain-text access log is disabled; the middleware writes one JSON line per request instead.
- **Comment style:** every file, class and function carries a plain-English docstring (Google style), per the user's later instruction, which replaces the brief's "short docstrings only" line.
