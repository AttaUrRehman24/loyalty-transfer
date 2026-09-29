"""Initial schema: programs, accounts, double-entry ledger, rates, transfers, outbox, keys.

Revision ID: 0001
Revises: none

Money-safety rules enforced by the database itself (not only by the code):
- MEMBER and CLEARING balances can never go negative (CHECK constraint).
- Every journal's entries add up to zero (deferred constraint trigger).
- Two versions of the same rate direction can never overlap in time (EXCLUDE constraint).
- One idempotency key can create at most one transfer (UNIQUE / PRIMARY KEY).
- A transfer can have at most one debit and one refund journal (partial UNIQUE index).
"""

from __future__ import annotations

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

UPGRADE_STATEMENTS: list[str] = [
    # btree_gist lets the rates EXCLUDE constraint combine "same program" (=) with
    # "overlapping time range" (&&) in one index.
    "CREATE EXTENSION IF NOT EXISTS btree_gist",
    """
    -- Loyalty programs we move points between. One row per partner.
    CREATE TABLE programs (
        -- Short stable code used everywhere else, for example BANKCARD or SKYMILES.
        code        TEXT PRIMARY KEY CHECK (code ~ '^[A-Z0-9_]{2,32}$'),
        -- Display name only.
        name        TEXT NOT NULL,
        -- CARD for credit card rewards, AIRLINE for travel loyalty.
        kind        TEXT NOT NULL CHECK (kind IN ('CARD', 'AIRLINE')),
        -- Name of the partner adapter module that talks to this partner's API.
        adapter     TEXT NOT NULL,
        -- Root URL of the partner's API. Stored here so a new partner is one row.
        base_url    TEXT NOT NULL,
        -- When the row was created, for audit.
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    -- Ledger accounts. Balances only change through ledger entries (see post_journal).
    CREATE TABLE accounts (
        -- Random UUID chosen by the application.
        id            UUID PRIMARY KEY,
        -- Program whose points this account holds. All entries share this unit.
        program_code  TEXT NOT NULL REFERENCES programs (code),
        -- MEMBER = a customer's points, CLEARING = points sent to partners,
        -- ISSUANCE = source of seeded points.
        kind          TEXT NOT NULL CHECK (kind IN ('MEMBER', 'CLEARING', 'ISSUANCE')),
        -- Customer id inside the program for MEMBER accounts, a fixed label otherwise.
        owner_ref     TEXT NOT NULL,
        -- Running total of this account's entries, kept in the same transaction.
        balance       BIGINT NOT NULL DEFAULT 0,
        -- When the account was opened.
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        -- A customer has at most one account per program.
        CONSTRAINT accounts_owner_unique UNIQUE (program_code, kind, owner_ref),
        -- Only ISSUANCE may go negative; it is the counter-side of every funding entry.
        CONSTRAINT accounts_no_negative_balance CHECK (kind = 'ISSUANCE' OR balance >= 0)
    )
    """,
    # Each program has exactly one CLEARING and one ISSUANCE account.
    """
    CREATE UNIQUE INDEX accounts_one_system_account_per_program
        ON accounts (program_code, kind) WHERE kind <> 'MEMBER'
    """,
    """
    -- Versioned conversion rates, one row per version per direction.
    CREATE TABLE rates (
        -- Random UUID; transfers keep this id so they always know the exact rate used.
        id                   UUID PRIMARY KEY,
        -- Program the points leave.
        source_program       TEXT NOT NULL REFERENCES programs (code),
        -- Program the points go to. A reverse direction is a separate row.
        destination_program  TEXT NOT NULL REFERENCES programs (code),
        -- 1, 2, 3 ... per direction.
        version              INT NOT NULL CHECK (version >= 1),
        -- Destination points per one source point. Exact decimal, never float.
        ratio                NUMERIC(18, 6) NOT NULL CHECK (ratio > 0),
        -- Smallest source amount allowed in one transfer.
        min_points           BIGINT NOT NULL CHECK (min_points > 0),
        -- Largest source amount allowed in one transfer.
        max_points           BIGINT NOT NULL,
        -- Source amount must be a whole multiple of this step.
        increment            BIGINT NOT NULL CHECK (increment > 0),
        -- First moment this version applies (inclusive).
        effective_from       TIMESTAMPTZ NOT NULL,
        -- Moment this version stops applying (exclusive). NULL = still open.
        effective_to         TIMESTAMPTZ NULL,
        -- When the version was written.
        created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT rates_distinct_programs CHECK (source_program <> destination_program),
        CONSTRAINT rates_max_not_below_min CHECK (max_points >= min_points),
        CONSTRAINT rates_window_valid CHECK (effective_to IS NULL OR effective_to > effective_from),
        CONSTRAINT rates_version_unique UNIQUE (source_program, destination_program, version),
        -- No two versions of one direction may be effective at the same instant.
        CONSTRAINT rates_no_overlap EXCLUDE USING gist (
            source_program WITH =,
            destination_program WITH =,
            tstzrange(effective_from, effective_to, '[)') WITH &&
        )
    )
    """,
    """
    -- One row per transfer request (the saga's state).
    CREATE TABLE transfers (
        -- Random UUID; also sent to the partner as its de-duplication id.
        id                     UUID PRIMARY KEY,
        -- Client key that created it. UNIQUE is a second guard against duplicates.
        idempotency_key        TEXT NOT NULL UNIQUE,
        -- Member account that was debited.
        source_account_id      UUID NOT NULL REFERENCES accounts (id),
        -- Program of the debited points.
        source_program         TEXT NOT NULL REFERENCES programs (code),
        -- Program receiving the converted points.
        destination_program    TEXT NOT NULL REFERENCES programs (code),
        -- Customer id inside the destination program.
        destination_member_id  TEXT NOT NULL,
        -- Points taken from the member.
        source_points          BIGINT NOT NULL CHECK (source_points > 0),
        -- Points the partner is asked to add, after rounding down.
        destination_points     BIGINT NOT NULL CHECK (destination_points > 0),
        -- Exact rate version used for the conversion.
        rate_id                UUID NOT NULL REFERENCES rates (id),
        -- Saga state; allowed moves are enforced by the domain state machine.
        status                 TEXT NOT NULL
                               CHECK (status IN ('DEBITED', 'UNKNOWN', 'COMPLETED', 'COMPENSATED')),
        -- Partner's own id for the credit, once confirmed.
        partner_reference      TEXT NULL,
        -- Why the transfer failed or is unknown, for support staff.
        failure_reason         TEXT NULL,
        -- When the request was accepted.
        created_at             TIMESTAMPTZ NOT NULL,
        -- When the status last changed (the reconciler waits on this).
        updated_at             TIMESTAMPTZ NOT NULL
    )
    """,
    # Serves GET /accounts/{id}/transfers (newest first).
    "CREATE INDEX transfers_account_created_idx ON transfers (source_account_id, created_at DESC)",
    # Small index the reconciler scans: only UNKNOWN rows are in it.
    "CREATE INDEX transfers_unknown_idx ON transfers (updated_at) WHERE status = 'UNKNOWN'",
    """
    -- A journal groups the entries of one business event; they must sum to zero.
    CREATE TABLE journals (
        -- Random UUID (fixed UUIDs for seed data, so seeding can run twice).
        id           UUID PRIMARY KEY,
        -- SEED = funding, TRANSFER_DEBIT = saga step 1, COMPENSATION = saga refund.
        kind         TEXT NOT NULL CHECK (kind IN ('SEED', 'TRANSFER_DEBIT', 'COMPENSATION')),
        -- Transfer this journal belongs to (NULL for seed funding).
        transfer_id  UUID NULL REFERENCES transfers (id),
        -- When it was posted.
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # A transfer can be debited once and refunded once, never twice.
    """
    CREATE UNIQUE INDEX journals_one_kind_per_transfer
        ON journals (transfer_id, kind) WHERE transfer_id IS NOT NULL
    """,
    """
    -- Double-entry ledger lines. Append-only; corrections are new journals.
    CREATE TABLE ledger_entries (
        -- Insert order, handy when reading history.
        id          BIGSERIAL PRIMARY KEY,
        -- Journal this line belongs to.
        journal_id  UUID NOT NULL REFERENCES journals (id),
        -- Account whose balance this line changes.
        account_id  UUID NOT NULL REFERENCES accounts (id),
        -- Signed whole points; negative = debit, positive = credit. Zero is meaningless.
        amount      BIGINT NOT NULL CHECK (amount <> 0),
        -- When it was written.
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ledger_entries_account_idx ON ledger_entries (account_id)",
    "CREATE INDEX ledger_entries_journal_idx ON ledger_entries (journal_id)",
    """
    -- Raises if a journal's lines do not add up to zero. Runs at COMMIT (deferred), after
    -- all lines of the journal were inserted, so partial journals mid-transaction are fine.
    CREATE FUNCTION ledger_check_journal_balanced() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        total NUMERIC;
    BEGIN
        SELECT COALESCE(SUM(amount), 0) INTO total
          FROM ledger_entries WHERE journal_id = NEW.journal_id;
        IF total <> 0 THEN
            RAISE EXCEPTION USING
                MESSAGE = 'journal ' || NEW.journal_id || ' is not balanced, sum is ' || total;
        END IF;
        RETURN NULL;
    END;
    $$
    """,
    """
    CREATE CONSTRAINT TRIGGER ledger_entries_balanced
        AFTER INSERT ON ledger_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION ledger_check_journal_balanced()
    """,
    """
    -- Transactional outbox. Written in the same transaction as the debit, so a crash
    -- between the debit and the partner call can always be recovered.
    CREATE TABLE outbox (
        -- Insert order; the relay processes oldest first.
        id             BIGSERIAL PRIMARY KEY,
        -- One job per transfer.
        transfer_id    UUID NOT NULL UNIQUE REFERENCES transfers (id),
        -- Only one job type exists today.
        kind           TEXT NOT NULL CHECK (kind IN ('PARTNER_CREDIT')),
        -- How many times the relay took over this job (0 = handled by the request).
        attempts       INT NOT NULL DEFAULT 0,
        -- Lease end. Until then the current owner is working on the job.
        claimed_until  TIMESTAMPTZ NOT NULL,
        -- Set when the job reached an outcome. NULL = still to do.
        processed_at   TIMESTAMPTZ NULL,
        -- When the job was queued.
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Small index of unfinished jobs, ordered by lease end, for the relay's scan.
    "CREATE INDEX outbox_pending_idx ON outbox (claimed_until) WHERE processed_at IS NULL",
    """
    -- Durable record of idempotency keys (Redis holds only the short in-flight lock).
    CREATE TABLE idempotency_keys (
        -- The client's Idempotency-Key header value.
        key            TEXT PRIMARY KEY CHECK (char_length(key) BETWEEN 1 AND 255),
        -- SHA-256 of the canonical request body; detects a key reused with a new body.
        request_hash   TEXT NOT NULL,
        -- Transfer this key created.
        transfer_id    UUID NOT NULL UNIQUE REFERENCES transfers (id),
        -- First response body sent to the client; replays return exactly this.
        response_body  JSONB NULL,
        -- When the key was first seen.
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]

# Drop in reverse dependency order.
DOWNGRADE_STATEMENTS: list[str] = [
    "DROP TABLE IF EXISTS idempotency_keys",
    "DROP TABLE IF EXISTS outbox",
    "DROP TRIGGER IF EXISTS ledger_entries_balanced ON ledger_entries",
    "DROP FUNCTION IF EXISTS ledger_check_journal_balanced()",
    "DROP TABLE IF EXISTS ledger_entries",
    "DROP TABLE IF EXISTS journals",
    "DROP TABLE IF EXISTS transfers",
    "DROP TABLE IF EXISTS rates",
    "DROP TABLE IF EXISTS accounts",
    "DROP TABLE IF EXISTS programs",
]


def upgrade() -> None:
    """Create the full schema, one statement at a time."""
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Remove everything created by `upgrade` (btree_gist is left installed)."""
    for statement in DOWNGRADE_STATEMENTS:
        op.execute(statement)
