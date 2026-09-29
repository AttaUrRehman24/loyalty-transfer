# Architecture

A single Python service moves loyalty points in both directions between credit card
programs and airline programs. Every transfer is a saga: a local debit, a partner
credit, then a commit or a refund. Postgres is the source of truth. Redis only holds
short in-flight locks.

## Component diagram

```mermaid
flowchart LR
    client([API client])

    subgraph engine[Transfer engine]
        direction TB
        api["api/<br/>routes, schemas, error mapping"]
        services["services/<br/>rate engine, orchestrator,<br/>idempotency, relay, reconciler"]
        domain["domain/<br/>models, state machine,<br/>ledger rules, ports"]
        infra["infra/<br/>repository, Redis lock,<br/>partner adapters"]
        api --> services --> domain
        infra -. implements ports .-> domain
    end

    worker["worker process<br/>(outbox relay + reconciler)"]
    pg[(PostgreSQL<br/>ledger, transfers, rates,<br/>outbox, idempotency keys)]
    redis[(Redis<br/>in-flight locks only)]
    card["Card partner API<br/>(mock_partners/card_program)"]
    air["Airline partner API<br/>(mock_partners/airline_program)"]

    client -->|REST| api
    infra --> pg
    infra --> redis
    infra -->|HTTP + timeout| card
    infra -->|HTTP + timeout| air
    worker --> services
```

Dependencies point inward: `api → services → domain ← infra`. `app/main.py`,
`app/worker.py` and `app/bootstrap.py` wire real infrastructure into the services.
`tests/unit/test_layering.py` fails the build if a layer imports an outer one.

## Sequence: happy path

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as API
    participant R as Redis
    participant O as Orchestrator
    participant DB as Postgres
    participant P as Partner

    C->>A: POST /transfers (Idempotency-Key: K)
    A->>R: SET lock:K NX EX ttl
    R-->>A: OK
    A->>DB: SELECT idempotency_keys WHERE key = K
    DB-->>A: none
    A->>O: accept()
    O->>DB: BEGIN; lock accounts (id order); check balance
    O->>DB: INSERT transfer (DEBITED), journal (member −N, clearing +N)
    O->>DB: INSERT outbox (lease), INSERT idempotency key; COMMIT
    A->>O: dispatch()
    O->>P: credit(transfer_id, member, points) [timeout, retry on 5xx]
    P-->>O: 201 credit_id
    O->>DB: BEGIN; lock transfer; DEBITED→COMPLETED; outbox done; COMMIT
    A->>DB: save response body for K
    A->>R: release lock:K (only if token matches)
    A-->>C: 201 {status: COMPLETED}
```

## Sequence: partner failure and rollback

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant O as Orchestrator
    participant DB as Postgres
    participant P as Partner

    C->>O: POST /transfers
    O->>DB: COMMIT debit + outbox (status DEBITED)
    loop up to PARTNER_MAX_ATTEMPTS, exponential backoff + jitter
        O->>P: credit()
        P-->>O: 503
    end
    Note over O: all attempts failed (5xx) or partner said 4xx
    O->>DB: BEGIN; lock transfer; DEBITED→COMPENSATED
    O->>DB: lock accounts; journal (clearing −N, member +N); outbox done; COMMIT
    O-->>C: 201 {status: COMPENSATED, failure_reason}
```

## Sequence: timeout and reconcile

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant O as Orchestrator
    participant DB as Postgres
    participant P as Partner
    participant W as Reconciler (worker)

    C->>O: POST /transfers
    O->>DB: COMMIT debit + outbox (DEBITED)
    O->>P: credit()
    Note over O,P: no answer within PARTNER_TIMEOUT_SECONDS
    O->>DB: DEBITED→UNKNOWN (points stay debited); outbox done
    O-->>C: 202 {status: UNKNOWN}

    W->>DB: find UNKNOWN older than RECONCILE_MIN_AGE_SECONDS
    W->>P: GET credit status (transfer_id)
    alt partner has the credit
        P-->>W: 200 credit_id
        W->>DB: lock transfer; UNKNOWN→COMPLETED
    else partner has no record
        P-->>W: 404
        W->>DB: lock transfer; UNKNOWN→COMPENSATED + refund journal
    else partner unreachable
        P-->>W: timeout / 5xx
        Note over W: stay UNKNOWN, try next cycle (never refund blind)
    end
```

## Sequence: idempotent replay

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as API
    participant R as Redis
    participant DB as Postgres

    C->>A: POST /transfers (K, body B) — second time
    A->>R: SET lock:K NX
    alt another request with K is running
        R-->>A: nil
        A-->>C: 409 IDEMPOTENCY_IN_PROGRESS
    else lock taken
        R-->>A: OK
        A->>DB: SELECT key, request_hash, response_body WHERE key = K
        alt hash(B) differs from stored hash
            A-->>C: 422 IDEMPOTENCY_KEY_REUSED
        else same body
            A-->>C: stored body, same status code, Idempotent-Replayed: true
        end
        A->>R: release lock:K
    end
    Note over A,DB: If Redis ever fails, the UNIQUE key in Postgres still<br/>allows only one transfer per key.
```

## Crash recovery (transactional outbox)

The debit, the transfer row and the outbox job commit in **one** transaction. The job
starts leased to the request that created it. If that process dies before calling the
partner, the lease expires, and the worker's outbox relay claims the job
(`FOR UPDATE SKIP LOCKED`) and runs the same `dispatch` step. Partners de-duplicate by
transfer id, so a dispatch that runs twice still credits once.

## Transfer state machine

```mermaid
stateDiagram-v2
    [*] --> DEBITED: accept (debit + outbox in one commit)
    DEBITED --> COMPLETED: partner confirmed
    DEBITED --> COMPENSATED: 5xx after retries, or 4xx (refund posted)
    DEBITED --> UNKNOWN: partner timeout
    UNKNOWN --> COMPLETED: reconciler — partner has credit
    UNKNOWN --> COMPENSATED: reconciler — partner has no credit (refund posted)
    COMPLETED --> [*]
    COMPENSATED --> [*]
```

Any other move raises `IllegalTransition` (`app/domain/state_machine.py`).

## Entity relationship diagram

```mermaid
erDiagram
    PROGRAMS ||--o{ ACCOUNTS : "holds points of"
    PROGRAMS ||--o{ RATES : "source / destination"
    ACCOUNTS ||--o{ LEDGER_ENTRIES : "changed by"
    JOURNALS ||--|{ LEDGER_ENTRIES : "groups (sum = 0)"
    TRANSFERS ||--o{ JOURNALS : "debit, refund"
    ACCOUNTS ||--o{ TRANSFERS : "debited from"
    RATES ||--o{ TRANSFERS : "priced with"
    TRANSFERS ||--|| OUTBOX : "partner credit job"
    TRANSFERS ||--|| IDEMPOTENCY_KEYS : "created by"

    PROGRAMS {
        text code PK
        text name
        text kind "CARD | AIRLINE"
        text adapter
        text base_url
    }
    ACCOUNTS {
        uuid id PK
        text program_code FK
        text kind "MEMBER | CLEARING | ISSUANCE"
        text owner_ref
        bigint balance "CHECK >= 0 unless ISSUANCE"
    }
    JOURNALS {
        uuid id PK
        text kind "SEED | TRANSFER_DEBIT | COMPENSATION"
        uuid transfer_id FK "unique per kind"
    }
    LEDGER_ENTRIES {
        bigint id PK
        uuid journal_id FK
        uuid account_id FK
        bigint amount "non-zero, signed"
    }
    RATES {
        uuid id PK
        text source_program FK
        text destination_program FK
        int version
        numeric ratio
        bigint min_points
        bigint max_points
        bigint increment
        timestamptz effective_from
        timestamptz effective_to "EXCLUDE overlap"
    }
    TRANSFERS {
        uuid id PK
        text idempotency_key UK
        uuid source_account_id FK
        text destination_program FK
        bigint source_points
        bigint destination_points
        uuid rate_id FK
        text status
        text partner_reference
    }
    OUTBOX {
        bigint id PK
        uuid transfer_id FK
        timestamptz claimed_until
        timestamptz processed_at
    }
    IDEMPOTENCY_KEYS {
        text key PK
        text request_hash
        uuid transfer_id FK
        jsonb response_body
    }
```

## Money-safety guarantees and where they live

| Guarantee | Enforced by |
|---|---|
| Member/clearing balances never negative | `accounts_no_negative_balance` CHECK + balance check under row lock |
| Every journal sums to zero | `ensure_balanced()` + deferred trigger `ledger_entries_balanced` |
| Whole ledger sums to zero | follows from the rule above; asserted after every integration test |
| One transfer per idempotency key | Redis lock (fast path) + `idempotency_keys` PK + `transfers.idempotency_key` UNIQUE |
| At most one debit and one refund per transfer | `journals_one_kind_per_transfer` partial UNIQUE index |
| No overlapping rate versions | `rates_no_overlap` EXCLUDE constraint |
| No refund while the partner outcome is unknown | state machine + reconciler asks partner first |
