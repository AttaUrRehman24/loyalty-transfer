"""Integration tests for the transfer saga, idempotency and recovery paths.

Every test runs against real Postgres and Redis, calls the engine over real HTTP, and the
engine calls real (mock) partner servers. The autouse `seeded` fixture checks after each
test that the ledger still sums to zero.
"""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import psycopg
from psycopg.rows import DictRow

from app.bootstrap import Runtime
from app.domain.models import TransferCommand
from app.services.idempotency import hash_request
from mock_partners.common import TimeoutMode
from scripts.seed import (
    AIRLINE_MEMBER_ACCOUNT_ID,
    AIRLINE_PROGRAM,
    CARD_MEMBER_ACCOUNT_ID,
    CARD_PROGRAM,
    STARTING_BALANCE,
)
from tests.integration.conftest import Mocks
from tests.support.db import balance, clearing_balance, scalar

# 10,000 card points at ratio 0.8 -> 8,000 airline miles.
CARD_TO_AIRLINE: dict[str, Any] = {
    "source_account_id": str(CARD_MEMBER_ACCOUNT_ID),
    "destination_program": AIRLINE_PROGRAM,
    "destination_member_id": "FF-778899",
    "source_points": 10_000,
}
# 4,000 airline miles at ratio 0.5 -> 2,000 card points.
AIRLINE_TO_CARD: dict[str, Any] = {
    "source_account_id": str(AIRLINE_MEMBER_ACCOUNT_ID),
    "destination_program": CARD_PROGRAM,
    "destination_member_id": "CARD-445566",
    "source_points": 4_000,
}


def post_transfer(client: httpx.Client, key: str, body: dict[str, Any]) -> httpx.Response:
    """POST /transfers with an Idempotency-Key."""
    return client.post("/transfers", json=body, headers={"Idempotency-Key": key})


def count_journals(db: psycopg.Connection[DictRow], kind: str) -> int:
    """Count journals of one kind."""
    return int(scalar(db, "SELECT COUNT(*) AS v FROM journals WHERE kind = %s", (kind,)))


def test_happy_path_card_to_airline(
    client: httpx.Client, db: psycopg.Connection[DictRow], mocks: Mocks
) -> None:
    # Card -> airline, partner healthy -> COMPLETED, card debited, airline credited once.
    response = post_transfer(client, "happy-card-to-airline", CARD_TO_AIRLINE)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["destination_points"] == 8_000
    assert body["partner_reference"]
    assert balance(db, CARD_MEMBER_ACCOUNT_ID) == STARTING_BALANCE - 10_000
    assert clearing_balance(db, CARD_PROGRAM) == 10_000
    credit = mocks.airline.store.get(body["id"])
    assert credit is not None and credit.points == 8_000 and credit.member_id == "FF-778899"
    fetched = client.get(f"/transfers/{body['id']}").json()
    assert fetched == body
    history = client.get(f"/accounts/{CARD_MEMBER_ACCOUNT_ID}/transfers").json()
    assert [item["id"] for item in history] == [body["id"]]


def test_happy_path_airline_to_card(
    client: httpx.Client, db: psycopg.Connection[DictRow], mocks: Mocks
) -> None:
    # Reverse direction uses its own rate (0.5) and the card partner's adapter.
    response = post_transfer(client, "happy-airline-to-card", AIRLINE_TO_CARD)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["destination_points"] == 2_000
    assert balance(db, AIRLINE_MEMBER_ACCOUNT_ID) == STARTING_BALANCE - 4_000
    credit = mocks.card.store.get(body["id"])
    assert credit is not None and credit.points == 2_000


def test_partner_5xx_is_retried_then_compensated(
    client: httpx.Client, db: psycopg.Connection[DictRow], mocks: Mocks
) -> None:
    # Partner always 503 -> 3 bounded tries -> COMPENSATED, member balance fully restored.
    mocks.airline.behavior.fail_5xx = True

    response = post_transfer(client, "partner-5xx", CARD_TO_AIRLINE)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "COMPENSATED"
    assert "partner failure" in body["failure_reason"]
    assert mocks.airline.store.credit_calls == 3
    assert balance(db, CARD_MEMBER_ACCOUNT_ID) == STARTING_BALANCE
    assert clearing_balance(db, CARD_PROGRAM) == 0
    assert count_journals(db, "TRANSFER_DEBIT") == 1
    assert count_journals(db, "COMPENSATION") == 1


def test_timeout_is_unknown_then_reconciler_confirms_success(
    client: httpx.Client, db: psycopg.Connection[DictRow], mocks: Mocks, runtime: Runtime
) -> None:
    # Partner applies the credit but answers too late -> UNKNOWN (not failed);
    # the reconciler finds the credit -> COMPLETED, and no refund happens.
    mocks.airline.behavior.timeout_mode = TimeoutMode.APPLY
    mocks.airline.behavior.timeout_delay_ms = 1_000

    response = post_transfer(client, "timeout-success", CARD_TO_AIRLINE)

    assert response.status_code == 202
    first_body = response.json()
    assert first_body["status"] == "UNKNOWN"
    assert balance(db, CARD_MEMBER_ACCOUNT_ID) == STARTING_BALANCE - 10_000

    assert runtime.reconciler.run_once() == 1

    settled = client.get(f"/transfers/{first_body['id']}").json()
    assert settled["status"] == "COMPLETED"
    assert settled["partner_reference"] == mocks.airline.store.get(first_body["id"]).reference  # type: ignore[union-attr]
    assert balance(db, CARD_MEMBER_ACCOUNT_ID) == STARTING_BALANCE - 10_000
    assert count_journals(db, "COMPENSATION") == 0
    # The idempotent replay still returns the ORIGINAL response, not the new state.
    replay = post_transfer(client, "timeout-success", CARD_TO_AIRLINE)
    assert replay.status_code == 202
    assert replay.json() == first_body


def test_timeout_is_unknown_then_reconciler_compensates(
    client: httpx.Client, db: psycopg.Connection[DictRow], mocks: Mocks, runtime: Runtime
) -> None:
    # Partner stalls and never applies the credit -> UNKNOWN; reconciler finds nothing
    # at the partner -> COMPENSATED and the member gets the points back.
    mocks.airline.behavior.timeout_mode = TimeoutMode.DROP
    mocks.airline.behavior.timeout_delay_ms = 1_000

    response = post_transfer(client, "timeout-failure", CARD_TO_AIRLINE)

    assert response.status_code == 202
    transfer_id = response.json()["id"]
    assert response.json()["status"] == "UNKNOWN"

    assert runtime.reconciler.run_once() == 1

    settled = client.get(f"/transfers/{transfer_id}").json()
    assert settled["status"] == "COMPENSATED"
    assert balance(db, CARD_MEMBER_ACCOUNT_ID) == STARTING_BALANCE
    assert clearing_balance(db, CARD_PROGRAM) == 0
    assert mocks.airline.store.get(transfer_id) is None


def test_crash_after_debit_before_partner_call_is_recovered_by_outbox(
    client: httpx.Client, db: psycopg.Connection[DictRow], mocks: Mocks, runtime: Runtime
) -> None:
    # Saga step 1 commits, then the process "dies" before calling the partner.
    # Once the outbox lease expires, the relay finishes the transfer exactly once.
    command = TransferCommand(CARD_MEMBER_ACCOUNT_ID, AIRLINE_PROGRAM, "FF-778899", 10_000)
    transfer = runtime.orchestrator.accept("crash-key", hash_request(command), command)

    assert transfer.status.value == "DEBITED"
    assert balance(db, CARD_MEMBER_ACCOUNT_ID) == STARTING_BALANCE - 10_000
    assert mocks.airline.store.credit_calls == 0
    # The dead request still "owns" the job until its lease runs out.
    assert runtime.relay.run_once() == 0

    # Pretend the lease ran out, as it would after the crashed process never came back.
    db.execute(
        "UPDATE outbox SET claimed_until = now() - interval '1 second' WHERE transfer_id = %s",
        (transfer.id,),
    )
    assert runtime.relay.run_once() == 1

    recovered = client.get(f"/transfers/{transfer.id}").json()
    assert recovered["status"] == "COMPLETED"
    assert mocks.airline.store.credit_calls == 1
    assert runtime.relay.run_once() == 0
    # A client retry after the crash gets the recovered result, marked as a replay.
    replay = post_transfer(client, "crash-key", CARD_TO_AIRLINE)
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["id"] == str(transfer.id)


def test_replay_same_key_same_body_returns_original_response(
    client: httpx.Client, db: psycopg.Connection[DictRow], mocks: Mocks
) -> None:
    # Same key + same body twice -> identical response, one transfer, one debit, one credit.
    first = post_transfer(client, "replay-key", CARD_TO_AIRLINE)
    second = post_transfer(client, "replay-key", CARD_TO_AIRLINE)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"
    assert scalar(db, "SELECT COUNT(*) AS v FROM transfers") == 1
    assert count_journals(db, "TRANSFER_DEBIT") == 1
    assert mocks.airline.store.credit_calls == 1
    assert balance(db, CARD_MEMBER_ACCOUNT_ID) == STARTING_BALANCE - 10_000


def test_same_key_different_body_is_rejected_with_422(
    client: httpx.Client, db: psycopg.Connection[DictRow]
) -> None:
    # Reusing a key with a changed amount must fail and must not move any points.
    assert post_transfer(client, "reused-key", CARD_TO_AIRLINE).status_code == 201

    changed = {**CARD_TO_AIRLINE, "source_points": 20_000}
    response = post_transfer(client, "reused-key", changed)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert balance(db, CARD_MEMBER_ACCOUNT_ID) == STARTING_BALANCE - 10_000


def test_50_concurrent_requests_same_key_create_exactly_one_transfer(
    client: httpx.Client, db: psycopg.Connection[DictRow], mocks: Mocks
) -> None:
    # 50 threads fire the same request at once -> one transfer, one debit, one credit.
    # Losers get 409 IDEMPOTENCY_IN_PROGRESS or, if they arrive late, the replayed 201.
    # Keep the winner in flight while the others arrive, but stay under the 0.3 s timeout.
    mocks.airline.behavior.latency_ms = 150
    barrier = threading.Barrier(50)

    def fire(_: int) -> httpx.Response:
        """Wait until all 50 threads are ready, then send the request at the same moment."""
        barrier.wait()
        return post_transfer(client, "concurrent-key", CARD_TO_AIRLINE)

    with ThreadPoolExecutor(max_workers=50) as pool:
        responses = list(pool.map(fire, range(50)))

    codes = Counter(response.status_code for response in responses)
    assert set(codes) <= {201, 409}, codes
    created = [response.json()["id"] for response in responses if response.status_code == 201]
    assert created and len(set(created)) == 1
    assert all(
        response.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
        for response in responses
        if response.status_code == 409
    )
    assert scalar(db, "SELECT COUNT(*) AS v FROM transfers") == 1
    assert count_journals(db, "TRANSFER_DEBIT") == 1
    assert mocks.airline.store.credit_calls == 1
    assert balance(db, CARD_MEMBER_ACCOUNT_ID) == STARTING_BALANCE - 10_000


def test_partner_duplicate_response_counts_as_success(client: httpx.Client, mocks: Mocks) -> None:
    # Partner applies the credit but answers "already processed" -> still COMPLETED.
    mocks.card.behavior.duplicate_responses = True

    response = post_transfer(client, "duplicate-response", AIRLINE_TO_CARD)

    assert response.status_code == 201
    assert response.json()["status"] == "COMPLETED"
    assert len(mocks.card.store.credits) == 1


def test_insufficient_points_is_409_and_creates_nothing(
    client: httpx.Client, db: psycopg.Connection[DictRow]
) -> None:
    # Airline member has 100,000 miles; the rate max is 50,000, so drain first, then
    # ask for more than is left.
    for index in range(2):
        body = {**AIRLINE_TO_CARD, "source_points": 50_000}
        assert post_transfer(client, f"drain-{index}", body).status_code == 201

    response = post_transfer(client, "too-much", {**AIRLINE_TO_CARD, "source_points": 1_000})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INSUFFICIENT_POINTS"
    assert balance(db, AIRLINE_MEMBER_ACCOUNT_ID) == 0
    assert scalar(db, "SELECT COUNT(*) AS v FROM transfers") == 2


def test_missing_idempotency_key_is_400(client: httpx.Client) -> None:
    # No Idempotency-Key header -> 400 in the shared error shape, nothing executed.
    response = client.post("/transfers", json=CARD_TO_AIRLINE)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_unknown_transfer_and_account_return_404(client: httpx.Client) -> None:
    # Lookups of ids that do not exist -> 404 with a specific error code.
    missing = "00000000-0000-4000-8000-000000000000"

    transfer = client.get(f"/transfers/{missing}")
    account = client.get(f"/accounts/{missing}/transfers")

    assert transfer.status_code == 404
    assert transfer.json()["error"]["code"] == "TRANSFER_NOT_FOUND"
    assert account.status_code == 404
    assert account.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_health_reports_database_and_redis(client: httpx.Client) -> None:
    # Both dependencies are up in the test stack -> 200 and "ok" for each.
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
