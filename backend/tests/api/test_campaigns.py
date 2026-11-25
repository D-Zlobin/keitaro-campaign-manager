"""Campaign endpoint contracts: headers, validation, envelopes, status codes."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Campaign, SyncState
from app.keitaro.client import KeitaroResponseError, KeitaroTimeoutError
from tests.fakes import DEFAULT_OFFER_ID, FakeKeitaroAdapter

CREATE_BODY = {"name": "Example DE", "country_code": "DE", "offer_id": DEFAULT_OFFER_ID}


def _create(client: TestClient, *, key: str = "key-1", body: dict | None = None):
    return client.post(
        "/api/v1/campaigns",
        json=body if body is not None else CREATE_BODY,
        headers={"Idempotency-Key": key},
    )


def _seed_remote_campaign(fake: FakeKeitaroAdapter, *, name: str = "Remote") -> int:
    campaign = fake.seed_campaign(name=name)
    fake.seed_stream(campaign_id=campaign.id, name="A", position=0, weight=50)
    fake.seed_stream(
        campaign_id=campaign.id,
        name="B",
        position=1,
        weight=50,
        offers=[{"offer_id": DEFAULT_OFFER_ID, "state": "active", "share": 100}],
    )
    return campaign.id


# Creation --------------------------------------------------------------------


def test_create_returns_201_with_campaign_summary(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    response = _create(api_client)

    assert response.status_code == 201
    body = response.json()
    campaign = body["campaign"]
    assert campaign["keitaro_id"] == 500
    assert campaign["name"] == "Example DE"
    assert campaign["country_code"] == "DE"
    assert campaign["sync_state"] == "clean"
    assert campaign["dirty"] is False
    assert campaign["revision"] == 1
    assert body["notices"] == []
    uuid.UUID(campaign["id"])  # local ID is a valid UUID


def test_create_normalizes_lowercase_country(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    response = _create(api_client, body={**CREATE_BODY, "country_code": "de"})

    assert response.status_code == 201
    assert response.json()["campaign"]["country_code"] == "DE"
    (google, _offer) = fake_keitaro.stream_payloads
    assert google["filters"][0]["payload"] == ["DE"]


def test_create_requires_idempotency_key(api_client: TestClient) -> None:
    response = api_client.post("/api/v1/campaigns", json=CREATE_BODY)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


@pytest.mark.parametrize("key", ["", "   "])
def test_create_rejects_empty_idempotency_key(api_client: TestClient, key: str) -> None:
    response = api_client.post(
        "/api/v1/campaigns", json=CREATE_BODY, headers={"Idempotency-Key": key}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_create_rejects_overlong_idempotency_key(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/v1/campaigns", json=CREATE_BODY, headers={"Idempotency-Key": "k" * 256}
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "country_code": "DE", "offer_id": DEFAULT_OFFER_ID},
        {"name": "   ", "country_code": "DE", "offer_id": DEFAULT_OFFER_ID},
        {"name": "x" * 256, "country_code": "DE", "offer_id": DEFAULT_OFFER_ID},
        {"name": "Example", "country_code": "D", "offer_id": DEFAULT_OFFER_ID},
        {"name": "Example", "country_code": "DEU", "offer_id": DEFAULT_OFFER_ID},
        {"name": "Example", "country_code": "D1", "offer_id": DEFAULT_OFFER_ID},
        {"name": "Example", "country_code": "DE", "offer_id": 0},
        {"name": "Example", "country_code": "DE", "offer_id": -3},
        {"country_code": "DE", "offer_id": DEFAULT_OFFER_ID},
        {"name": "Example", "offer_id": DEFAULT_OFFER_ID},
        {"name": "Example", "country_code": "DE"},
    ],
)
def test_create_rejects_malformed_payloads(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter, body: dict
) -> None:
    response = _create(api_client, body=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert fake_keitaro.calls == []  # no remote work for malformed input


def test_create_rejects_unassigned_country_code(api_client: TestClient) -> None:
    response = _create(api_client, body={**CREATE_BODY, "country_code": "ZZ"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_country_code"


def test_create_replay_returns_original_result(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    first = _create(api_client)
    second = _create(api_client)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert fake_keitaro.calls_of("create_campaign") == 1


def test_create_same_key_different_payload_is_409(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    _create(api_client)
    conflict = _create(api_client, body={**CREATE_BODY, "name": "Other"})

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_payload_conflict"
    assert fake_keitaro.calls_of("create_campaign") == 1


def test_create_failure_response_carries_cleanup_details(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("create_stream", KeitaroResponseError("create_stream", 500, "x"))

    response = _create(api_client)

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "upstream_error"
    assert error["details"]["keitaro_campaign_id"] == 500
    assert error["details"]["cleanup_status"] == "archived"

    # A retry with the same key replays the recorded failure.
    replay = _create(api_client)
    assert replay.status_code == 502
    assert replay.json() == response.json()
    assert fake_keitaro.calls_of("create_campaign") == 1


def test_create_unavailable_offer_is_422(api_client: TestClient) -> None:
    response = _create(api_client, body={**CREATE_BODY, "offer_id": 999999})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "offer_unavailable"
    assert error["details"]["cleanup_status"] == "not_required"


def test_create_timeout_is_504(api_client: TestClient, fake_keitaro: FakeKeitaroAdapter) -> None:
    fake_keitaro.queue_failure("create_campaign", KeitaroTimeoutError("timed out"))

    response = _create(api_client)

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"


# Listing ---------------------------------------------------------------------


def test_list_campaigns_empty(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/campaigns")

    assert response.status_code == 200
    assert response.json() == {"campaigns": []}


def test_list_campaigns_shows_created_and_imported(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    _create(api_client)
    keitaro_id = _seed_remote_campaign(fake_keitaro, name="Imported")
    api_client.post("/api/v1/campaigns/import", json={"keitaro_campaign_id": keitaro_id})

    response = api_client.get("/api/v1/campaigns")

    assert response.status_code == 200
    campaigns = response.json()["campaigns"]
    assert len(campaigns) == 2
    by_keitaro_id = {c["keitaro_id"]: c for c in campaigns}
    assert by_keitaro_id[500]["name"] == "Example DE"
    assert by_keitaro_id[500]["sync_state"] == "clean"
    assert by_keitaro_id[500]["last_fetch_at"] is not None
    assert by_keitaro_id[keitaro_id]["name"] == "Imported"


# Import ----------------------------------------------------------------------


def test_import_returns_201(api_client: TestClient, fake_keitaro: FakeKeitaroAdapter) -> None:
    keitaro_id = _seed_remote_campaign(fake_keitaro)

    response = api_client.post("/api/v1/campaigns/import", json={"keitaro_campaign_id": keitaro_id})

    assert response.status_code == 201
    campaign = response.json()["campaign"]
    assert campaign["keitaro_id"] == keitaro_id
    assert campaign["sync_state"] == "clean"
    assert campaign["revision"] == 1


def test_import_duplicate_is_409_without_replacing_state(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    keitaro_id = _seed_remote_campaign(fake_keitaro)
    first = api_client.post("/api/v1/campaigns/import", json={"keitaro_campaign_id": keitaro_id})

    duplicate = api_client.post(
        "/api/v1/campaigns/import", json={"keitaro_campaign_id": keitaro_id}
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "campaign_exists"
    listing = api_client.get("/api/v1/campaigns").json()["campaigns"]
    assert len(listing) == 1
    assert listing[0]["revision"] == first.json()["campaign"]["revision"]


def test_import_unknown_campaign_is_404(api_client: TestClient) -> None:
    response = api_client.post("/api/v1/campaigns/import", json={"keitaro_campaign_id": 424242})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("keitaro_id", [0, -1])
def test_import_rejects_non_positive_ids(api_client: TestClient, keitaro_id: int) -> None:
    response = api_client.post("/api/v1/campaigns/import", json={"keitaro_campaign_id": keitaro_id})

    assert response.status_code == 400


# Fetch -----------------------------------------------------------------------


def test_fetch_returns_updated_state(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    created = _create(api_client).json()["campaign"]
    fake_keitaro.touch_stream(900, weight=70)
    fake_keitaro.touch_stream(901, weight=30)

    response = api_client.post(f"/api/v1/campaigns/{created['id']}/fetch")

    assert response.status_code == 200
    campaign = response.json()["campaign"]
    assert campaign["revision"] == 2
    assert campaign["sync_state"] == "clean"
    assert response.json()["notices"] == []


def test_fetch_dirty_without_confirmation_is_409(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    created = _create(api_client).json()["campaign"]
    # Make the local draft dirty: change a stream weight behind the API.
    app = api_client.app
    with app.state.session_factory() as session:
        campaign = session.get(Campaign, uuid.UUID(created["id"]))
        repo_replace_draft(session, campaign, share=80)

    response = api_client.post(f"/api/v1/campaigns/{created['id']}/fetch")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "dirty_draft"

    confirmed = api_client.post(
        f"/api/v1/campaigns/{created['id']}/fetch", json={"confirm_discard": True}
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["campaign"]["sync_state"] == "clean"


def repo_replace_draft(session, campaign: Campaign, *, share: int) -> None:
    """Test helper: rewrite the draft with a different first-stream share."""
    from app.repositories.campaigns import CampaignRepository, DraftStreamInput

    streams = [
        DraftStreamInput(
            keitaro_id=s.keitaro_id,
            name=s.name,
            type=s.type,
            schema=s.schema_,
            position=s.position,
            state=s.state,
            share=share if index == 0 else 100 - share,
            baseline_payload=s.baseline_payload,
        )
        for index, s in enumerate(campaign.streams)
    ]
    CampaignRepository().replace_draft(
        session,
        campaign_id=campaign.id,
        expected_revision=campaign.revision,
        streams=streams,
        sync_state=SyncState.DIRTY,
    )
    session.commit()


def test_fetch_unknown_campaign_is_404(api_client: TestClient) -> None:
    response = api_client.post(f"/api/v1/campaigns/{uuid.uuid4()}/fetch")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_fetch_malformed_id_is_400(api_client: TestClient) -> None:
    response = api_client.post("/api/v1/campaigns/not-a-uuid/fetch")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_fetch_timeout_is_504(api_client: TestClient, fake_keitaro: FakeKeitaroAdapter) -> None:
    created = _create(api_client).json()["campaign"]
    fake_keitaro.queue_failure("get_campaign", KeitaroTimeoutError("timed out"))

    response = api_client.post(f"/api/v1/campaigns/{created['id']}/fetch")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"


def test_fetch_stale_revision_is_409(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    created = _create(api_client).json()["campaign"]
    app = api_client.app
    original = fake_keitaro.get_campaign_streams

    def racing_get_streams(keitaro_id: int):
        with app.state.session_factory() as session:
            campaign = session.get(Campaign, uuid.UUID(created["id"]))
            campaign.revision += 1
            session.commit()
        return original(keitaro_id)

    fake_keitaro.get_campaign_streams = racing_get_streams  # type: ignore[method-assign]

    response = api_client.post(f"/api/v1/campaigns/{created['id']}/fetch")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_revision"


def test_no_response_contains_the_api_key(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter, api_settings
) -> None:
    _create(api_client)
    keitaro_id = _seed_remote_campaign(fake_keitaro)
    responses = [
        api_client.get("/api/v1/campaigns"),
        api_client.post("/api/v1/campaigns/import", json={"keitaro_campaign_id": keitaro_id}),
        api_client.post("/api/v1/campaigns/import", json={"keitaro_campaign_id": keitaro_id}),
        _create(api_client, key="k2", body={**CREATE_BODY, "country_code": "ZZ"}),
    ]

    secret = api_settings.keitaro_api_key.get_secret_value()
    for response in responses:
        assert secret not in response.text
