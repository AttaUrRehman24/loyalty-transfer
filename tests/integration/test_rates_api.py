"""Integration tests for rate management (GET/POST/PUT /rates) and POST /quotes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from scripts.seed import AIRLINE_PROGRAM, CARD_PROGRAM, SeedResult


def test_quote_uses_the_effective_rate(client: httpx.Client, seeded: SeedResult) -> None:
    # 1,500 card points at 0.8 -> 1,200 miles, priced with the seeded rate version 1.
    response = client.post(
        "/quotes",
        json={
            "source_program": CARD_PROGRAM,
            "destination_program": AIRLINE_PROGRAM,
            "source_points": 1_500,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["destination_points"] == 1_200
    assert body["rate_id"] == str(seeded.card_to_airline_rate_id)
    assert body["rate_version"] == 1


def test_quote_rejects_amount_off_increment(client: httpx.Client) -> None:
    # 1,250 is not a multiple of the 500 increment -> 422 in the shared error shape.
    response = client.post(
        "/quotes",
        json={
            "source_program": CARD_PROGRAM,
            "destination_program": AIRLINE_PROGRAM,
            "source_points": 1_250,
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_INCREMENT"


def test_put_creates_new_version_and_closes_the_old_one(
    client: httpx.Client, seeded: SeedResult
) -> None:
    # PUT on the open version -> version 2 starts now, version 1 ends at that instant,
    # and quotes switch to the new ratio.
    response = client.put(
        f"/rates/{seeded.card_to_airline_rate_id}",
        json={"ratio": "1.25", "min_points": 1000, "max_points": 100000, "increment": 500},
    )

    assert response.status_code == 200
    new_version = response.json()
    assert new_version["version"] == 2
    assert new_version["effective_to"] is None

    versions = client.get(
        "/rates",
        params={"source_program": CARD_PROGRAM, "destination_program": AIRLINE_PROGRAM},
    ).json()
    assert [rate["version"] for rate in versions] == [1, 2]
    assert versions[0]["effective_to"] == new_version["effective_from"]

    quote = client.post(
        "/quotes",
        json={
            "source_program": CARD_PROGRAM,
            "destination_program": AIRLINE_PROGRAM,
            "source_points": 1_000,
        },
    ).json()
    assert quote["destination_points"] == 1_250
    assert quote["rate_version"] == 2


def test_put_on_closed_version_is_409(client: httpx.Client, seeded: SeedResult) -> None:
    # After version 2 exists, version 1 is closed and can no longer be replaced.
    terms = {"ratio": "0.9", "min_points": 1000, "max_points": 100000, "increment": 500}
    assert client.put(f"/rates/{seeded.card_to_airline_rate_id}", json=terms).status_code == 200

    response = client.put(f"/rates/{seeded.card_to_airline_rate_id}", json=terms)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RATE_CONFLICT"


def test_post_overlapping_rate_is_409(client: httpx.Client) -> None:
    # The seeded rate is open-ended, so a second rate for the same direction overlaps.
    response = client.post(
        "/rates",
        json={
            "source_program": CARD_PROGRAM,
            "destination_program": AIRLINE_PROGRAM,
            "ratio": "1.0",
            "min_points": 1000,
            "max_points": 5000,
            "increment": 1000,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RATE_CONFLICT"


def test_put_with_future_start_keeps_current_version_until_then(
    client: httpx.Client, seeded: SeedResult
) -> None:
    # A version scheduled for the future is stored, but quotes keep using today's version
    # until it starts.
    starts = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    terms = {"ratio": "0.7", "min_points": 1000, "max_points": 100000, "increment": 500}
    response = client.put(
        f"/rates/{seeded.card_to_airline_rate_id}", json={**terms, "effective_from": starts}
    )
    assert response.status_code == 200

    quote = client.post(
        "/quotes",
        json={
            "source_program": CARD_PROGRAM,
            "destination_program": AIRLINE_PROGRAM,
            "source_points": 1_000,
        },
    ).json()

    assert quote["rate_version"] == 1
    assert quote["destination_points"] == 800
