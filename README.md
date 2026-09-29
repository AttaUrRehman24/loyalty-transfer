# Loyalty Points Transfer Engine

A microservice that converts points in both directions between credit card rewards
programs and airline loyalty programs. Stack: Python 3.12, FastAPI, PostgreSQL, Redis.

- Versioned conversion rates per direction (ratio, min, max, increment, effective dates).
- Each transfer is a saga: local debit → partner credit → commit, or an automatic refund.
- Idempotency keys: same key + same body replays the original response; a changed body gets 422.
- Double-entry ledger. The database blocks negative balances and unbalanced journals.
- A transactional outbox and a reconciler recover from crashes and partner timeouts.

Architecture, diagrams and the state machine: [`docs/architecture.md`](docs/architecture.md).
Choices made where the brief was silent: [`docs/decisions.md`](docs/decisions.md).
What is not built: [`docs/limitations.md`](docs/limitations.md).

## Setup (one command)

Requires Docker with the Compose plugin.

```bash
make up      # builds and starts postgres, redis, card-partner, airline-partner, api, worker
```

The API listens on `http://localhost:8000`. Migrations and seed data are applied on start.
OpenAPI docs: `http://localhost:8000/docs`.

```bash
make test    # unit + integration tests against the compose Postgres and Redis
make lint    # ruff check, ruff format --check, mypy --strict
make down    # stop and delete volumes
```

Every setting is an env var, documented in [`.env.example`](.env.example). Copy it to
`.env` to override values.

### Seeded demo data

| Item | Value |
|---|---|
| Card member account (BANKCARD) | `11111111-1111-4111-8111-111111111111`, 100,000 points |
| Airline member account (SKYMILES) | `22222222-2222-4222-8222-222222222222`, 100,000 miles |
| BANKCARD → SKYMILES rate | ratio 0.8, min 1,000, max 100,000, increment 500 |
| SKYMILES → BANKCARD rate | ratio 0.5, min 1,000, max 50,000, increment 1,000 |

## curl examples

```bash
# Health
curl -s localhost:8000/health

# Quote: 10,000 card points -> 8,000 miles
curl -s -X POST localhost:8000/quotes -H 'Content-Type: application/json' \
  -d '{"source_program":"BANKCARD","destination_program":"SKYMILES","source_points":10000}'

# Transfer card -> airline (Idempotency-Key is required)
curl -s -i -X POST localhost:8000/transfers -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-1001' \
  -d '{"source_account_id":"11111111-1111-4111-8111-111111111111",
       "destination_program":"SKYMILES","destination_member_id":"FF-778899","source_points":10000}'

# Same key + same body again -> identical response, header Idempotent-Replayed: true
# Same key + different body -> 422 IDEMPOTENCY_KEY_REUSED

# Transfer airline -> card
curl -s -X POST localhost:8000/transfers -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: order-1002' \
  -d '{"source_account_id":"22222222-2222-4222-8222-222222222222",
       "destination_program":"BANKCARD","destination_member_id":"CARD-445566","source_points":4000}'

# Read a transfer / an account's history
curl -s localhost:8000/transfers/<transfer-id>
curl -s 'localhost:8000/accounts/11111111-1111-4111-8111-111111111111/transfers?limit=20'

# Rates: list, create, new version
curl -s 'localhost:8000/rates?source_program=BANKCARD&destination_program=SKYMILES'
curl -s -X PUT localhost:8000/rates/<rate-id> -H 'Content-Type: application/json' \
  -d '{"ratio":"1.25","min_points":1000,"max_points":100000,"increment":500}'
curl -s -X POST localhost:8000/rates -H 'Content-Type: application/json' \
  -d '{"source_program":"BANKCARD","destination_program":"SKYMILES","ratio":"1.0",
       "min_points":1000,"max_points":5000,"increment":1000,
       "effective_from":"2030-01-01T00:00:00Z"}'
```

Every error has the same shape:

```json
{"error": {"code": "INSUFFICIENT_POINTS", "message": "account has 0 points, transfer needs 1000", "details": null}}
```

### Simulating partner failures

Set `MOCK_*` variables (see `.env.example`) and restart the mocks, for example:

```bash
MOCK_FAIL_5XX=true docker compose up -d card-partner airline-partner   # -> COMPENSATED
MOCK_TIMEOUT_MODE=drop docker compose up -d airline-partner             # -> UNKNOWN, then the worker settles it
```

## How to add a partner

1. Create one adapter file in `app/infra/partners/`, for example `aeroplus_program.py`:

   ```python
   @register_adapter("aeroplus_program")
   class AeroPlusProgramAdapter:
       def __init__(self, client: PartnerHttpClient) -> None: ...
       def credit(self, request: PartnerCreditRequest) -> PartnerCreditResult: ...
       def credit_status(self, transfer_id: UUID) -> PartnerCreditStatus: ...
       def close(self) -> None: ...
   ```

   Send `request.transfer_id` as the partner's de-duplication id. Treat the partner's
   "already processed" answer as success. `PartnerHttpClient` already maps timeouts,
   connection errors and 5xx to the right domain errors.

2. Add one config row (plus its CLEARING and ISSUANCE accounts), and rates for each direction:

   ```sql
   INSERT INTO programs (code, name, kind, adapter, base_url)
   VALUES ('AEROPLUS', 'AeroPlus Miles', 'AIRLINE', 'aeroplus_program', 'https://partner.example/api');
   ```

   Then `POST /rates` for each direction you want to support.

Modules in `app/infra/partners/` are discovered automatically; no other file changes.

## Traceability

| Scope line | Code | Tests |
|---|---|---|
| Microservice for bi-directional point conversions (cards ↔ travel programs) | `app/main.py`, `app/services/orchestrator.py`, `app/api/routes/transfers.py` | `test_happy_path_card_to_airline`, `test_happy_path_airline_to_card` |
| Flexible conversion rate engine | `app/services/rate_engine.py`, `app/services/rates.py`, `app/domain/rounding.py`, `rates` table in `migrations/versions/0001_initial_schema.py`, `app/api/routes/rates.py`, `app/api/routes/quotes.py` | `tests/unit/test_rate_engine.py` (rounding, min/max, increment, effective dates, both directions), `tests/integration/test_rates_api.py` |
| Atomic transactions with automatic rollback on failure | `TransferOrchestrator.accept` / `resolve` (`app/services/orchestrator.py`), `app/domain/ledger.py`, `app/domain/state_machine.py`, `app/infra/db.py`, `PgRepository.post_journal` | `test_partner_5xx_is_retried_then_compensated`, `test_timeout_is_unknown_then_reconciler_compensates`, `tests/unit/test_state_machine_and_ledger.py`, ledger-sum check after every integration test (`tests/integration/conftest.py`) |
| Idempotency keys | `app/services/idempotency.py`, `app/infra/redis_lock.py`, `idempotency_keys` table | `test_replay_same_key_same_body_returns_original_response`, `test_same_key_different_body_is_rejected_with_422`, `test_50_concurrent_requests_same_key_create_exactly_one_transfer`, `test_missing_idempotency_key_is_400` |
| Partner API integration | `app/infra/partners/` (http, registry, card_program, airline_program), `app/services/retry.py`, `app/services/recovery.py`, `mock_partners/` | `test_partner_5xx_is_retried_then_compensated`, `test_timeout_is_unknown_then_reconciler_confirms_success`, `test_partner_duplicate_response_counts_as_success`, `test_crash_after_debit_before_partner_call_is_recovered_by_outbox`, `tests/unit/test_retry.py` |
| Stack: Python, PostgreSQL, Redis, REST | `requirements.txt`, `docker-compose.yml`, `app/api/` | whole integration suite (real Postgres + Redis over HTTP), `test_health_reports_database_and_redis` |
| Deliverable: architecture diagram | `docs/architecture.md` | — |
| Deliverable: complete source code | this repository | `make test`, `make lint`, `tests/unit/test_layering.py` |

## Project layout

```
app/
  api/            routes, schemas, error mapping, request-context middleware
  services/       rate engine, rates, orchestrator (saga), idempotency, relay/reconciler, retry
  domain/         models, errors, rounding, ledger rules, state machine, ports
  infra/          db pool + unit of work, repository (all SQL), Redis lock, probes, partners/
  bootstrap.py    wires infra into services
  main.py         FastAPI app factory
  worker.py       outbox relay + reconciler loop
  config.py       env-var settings
  observability.py JSON logging with transfer_id and idempotency_key
migrations/       Alembic (0001_initial_schema.py)
mock_partners/    card and airline mock partner apps
scripts/seed.py   demo data
tests/unit/       rate engine, state machine, ledger, retry, layering
tests/integration/ transfers, idempotency, recovery, rates API
docs/             architecture, decisions, limitations
```
