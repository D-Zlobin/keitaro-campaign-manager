"""Upload endpoint tests: routing, error envelope, and startup recovery.

The conflict/rollback matrix lives in the service tests; here we verify the
HTTP contract of ``POST /api/v1/campaigns/{id}/upload`` and that app startup
resets campaigns left in ``syncing`` by a crash.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.models import Campaign, SyncState
from app.main import create_app
from tests.fakes import DEFAULT_OFFER_ID, FakeKeitaroAdapter


def _create_campaign(client: TestClient) -> str:
    response = client.post(
        "/api/v1/campaigns",
        json={"name": "Example DE", "country_code": "DE", "offer_id": DEFAULT_OFFER_ID},
        headers={"Idempotency-Key": f"create-{uuid.uuid4()}"},
    )
    assert response.status_code == 201
    return response.json()["campaign"]["id"]


def _delete_google_stream(client: TestClient, campaign_id: str, revision: int) -> int:
    response = client.put(
        f"/api/v1/campaigns/{campaign_id}/draft",
        json={
            "expected_revision": revision,
            "command": {"type": "delete_stream", "stream_id": 900},
        },
    )
    assert response.status_code == 200
    return response.json()["campaign"]["revision"]


def _set_sync_state(client: TestClient, campaign_id: str, state: SyncState) -> None:
    with client.app.state.session_factory() as session:
        campaign = session.get(Campaign, uuid.UUID(campaign_id))
        assert campaign is not None
        campaign.sync_state = state
        session.commit()


def test_upload_success(api_client: TestClient, fake_keitaro: FakeKeitaroAdapter) -> None:
    campaign_id = _create_campaign(api_client)
    revision = _delete_google_stream(api_client, campaign_id, revision=1)

    response = api_client.post(f"/api/v1/campaigns/{campaign_id}/upload")

    assert response.status_code == 200
    body = response.json()
    assert body["campaign"]["dirty"] is False
    assert body["campaign"]["sync_state"] == "clean"
    assert body["campaign"]["revision"] == revision + 1
    assert body["campaign"]["last_upload_at"] is not None
    assert body["notices"] == []
    assert fake_keitaro.streams[900].state == "disabled"
    assert fake_keitaro.streams[901].weight == 100


def test_upload_clean_draft_conflict(api_client: TestClient) -> None:
    campaign_id = _create_campaign(api_client)

    response = api_client.post(f"/api/v1/campaigns/{campaign_id}/upload")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "clean_draft"


def test_upload_remote_conflict(api_client: TestClient, fake_keitaro: FakeKeitaroAdapter) -> None:
    campaign_id = _create_campaign(api_client)
    _delete_google_stream(api_client, campaign_id, revision=1)
    fake_keitaro.touch_stream(901, weight=80)

    response = api_client.post(f"/api/v1/campaigns/{campaign_id}/upload")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "remote_conflict"
    assert not any(call == "update_stream" for call in fake_keitaro.calls)


def test_upload_unknown_campaign(api_client: TestClient) -> None:
    response = api_client.post(f"/api/v1/campaigns/{uuid.uuid4()}/upload")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_upload_needs_attention_conflict(api_client: TestClient) -> None:
    campaign_id = _create_campaign(api_client)
    _set_sync_state(api_client, campaign_id, SyncState.NEEDS_ATTENTION)

    response = api_client.post(f"/api/v1/campaigns/{campaign_id}/upload")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "needs_attention"


def test_startup_resets_stale_syncing(
    api_settings,
    fake_keitaro: FakeKeitaroAdapter,
    api_client: TestClient,
) -> None:
    campaign_id = _create_campaign(api_client)
    _set_sync_state(api_client, campaign_id, SyncState.SYNCING)

    # A restart (lifespan startup) resets the stale syncing state to dirty.
    with TestClient(create_app(api_settings, keitaro=fake_keitaro)) as restarted:
        response = restarted.get(f"/api/v1/campaigns/{campaign_id}")

    assert response.status_code == 200
    assert response.json()["campaign"]["sync_state"] == "dirty"


def test_startup_fails_clearly_when_database_not_migrated(
    tmp_path, fake_keitaro: FakeKeitaroAdapter
) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path}/unmigrated.db")

    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        with TestClient(create_app(settings, keitaro=fake_keitaro)):
            pass
