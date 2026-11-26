"""Draft editing endpoints: detail, commands, recalculation, and conflicts.

Covers plan stage 5: every draft mutation, malformed IDs, stale revisions,
unsupported edits, rollback on rejection, reload persistence, disabled-row
and pin behavior, offer reactivation, and the active-stream-needs-offers
rule. Share arithmetic itself is unit-tested in tests/domain; here the
backend is exercised end to end through HTTP.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Campaign, SyncState
from app.keitaro.client import KeitaroResponseError, KeitaroTimeoutError
from app.keitaro.models import KeitaroOffer
from tests.fakes import DEFAULT_OFFER_ID, FakeKeitaroAdapter

CREATE_BODY = {"name": "Example DE", "country_code": "DE", "offer_id": DEFAULT_OFFER_ID}
SECOND_OFFER_ID = 18002


def _create_campaign(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/campaigns", json=CREATE_BODY, headers={"Idempotency-Key": "key-1"}
    )
    assert response.status_code == 201
    return response.json()["campaign"]


def _import_campaign(
    client: TestClient, fake: FakeKeitaroAdapter, *, weights: tuple[int, ...] = (34, 33, 33)
) -> dict:
    """Import a remote campaign whose streams hold one offer each."""
    campaign = fake.seed_campaign(name="Imported")
    for position, weight in enumerate(weights):
        fake.seed_stream(
            campaign_id=campaign.id,
            name=f"Stream {position}",
            position=position,
            weight=weight,
            offers=[{"offer_id": DEFAULT_OFFER_ID + position, "state": "active", "share": 100}],
        )
    response = client.post("/api/v1/campaigns/import", json={"keitaro_campaign_id": campaign.id})
    assert response.status_code == 201
    return response.json()["campaign"]


def _detail(client: TestClient, campaign_id: str) -> dict:
    response = client.get(f"/api/v1/campaigns/{campaign_id}")
    assert response.status_code == 200
    return response.json()


def _put(client: TestClient, campaign_id: str, revision: int, command: dict):
    return client.put(
        f"/api/v1/campaigns/{campaign_id}/draft",
        json={"expected_revision": revision, "command": command},
    )


def _stream(detail: dict, keitaro_id: int) -> dict:
    return next(s for s in detail["streams"] if s["keitaro_id"] == keitaro_id)


def _seed_second_offer(fake: FakeKeitaroAdapter) -> None:
    fake.offers[SECOND_OFFER_ID] = KeitaroOffer(
        id=SECOND_OFFER_ID, name="Second Offer", state="active"
    )


# GET /campaigns/{id} ----------------------------------------------------------


def test_get_detail_returns_full_draft_state(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)

    detail = _detail(api_client, campaign["id"])

    assert detail["campaign"]["id"] == campaign["id"]
    assert detail["campaign"]["revision"] == 1
    assert detail["campaign"]["dirty"] is False
    assert detail["campaign"]["sync_state"] == "clean"
    assert detail["notices"] == []
    assert detail["upload_blocked"] is False
    assert [s["keitaro_id"] for s in detail["streams"]] == [900, 901]

    google = _stream(detail, 900)
    assert google["name"] == "Google — DE"
    assert google["schema"] == "redirect"
    assert google["position"] == 0
    assert google["state"] == "active"
    assert google["share"] == 50
    assert google["pinned"] is False
    assert google["offer_capable"] is False
    assert google["offers"] == []

    offer_stream = _stream(detail, 901)
    assert offer_stream["schema"] == "landings"
    assert offer_stream["offer_capable"] is True
    assert offer_stream["offers"] == [
        {
            "offer_id": DEFAULT_OFFER_ID,
            "offer_name": "Example Offer",
            "keitaro_association_id": 7001,
            "state": "active",
            "share": 100,
        }
    ]


def test_get_detail_unknown_campaign_is_404(api_client: TestClient) -> None:
    response = api_client.get(f"/api/v1/campaigns/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_get_detail_malformed_id_is_400(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/campaigns/not-a-uuid")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_get_detail_shows_persisted_fetch_notices(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = fake_keitaro.seed_campaign(name="Skewed")
    fake_keitaro.seed_stream(campaign_id=campaign.id, name="A", position=0, weight=80)
    fake_keitaro.seed_stream(campaign_id=campaign.id, name="B", position=1, weight=80)
    imported = api_client.post(
        "/api/v1/campaigns/import", json={"keitaro_campaign_id": campaign.id}
    )
    assert imported.status_code == 201

    detail = _detail(api_client, imported.json()["campaign"]["id"])

    assert detail["campaign"]["dirty"] is True
    assert detail["notices"] == [
        "Active stream shares total 160 instead of 100; redistributed equally."
    ]


# Stream commands ---------------------------------------------------------------


def test_delete_stream_recalculates_and_marks_dirty(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _import_campaign(api_client, fake_keitaro)

    response = _put(api_client, campaign["id"], 1, {"type": "delete_stream", "stream_id": 901})

    assert response.status_code == 200
    body = response.json()
    assert body["campaign"]["revision"] == 2
    assert body["campaign"]["dirty"] is True
    assert body["campaign"]["sync_state"] == "dirty"
    # Disabled row remains visible with share 0 and no pin; the rest split 100.
    deleted = _stream(body, 901)
    assert deleted["state"] == "disabled"
    assert deleted["share"] == 0
    assert deleted["pinned"] is False
    assert _stream(body, 900)["share"] == 50
    assert _stream(body, 902)["share"] == 50


def test_delete_last_unpinned_stream_below_pinned_100_rejected(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _import_campaign(api_client, fake_keitaro)
    pinned = _put(api_client, campaign["id"], 1, {"type": "pin_stream", "stream_id": 900})
    assert pinned.status_code == 200
    deleted = _put(api_client, campaign["id"], 2, {"type": "delete_stream", "stream_id": 901})
    assert deleted.status_code == 200
    assert _stream(deleted.json(), 902)["share"] == 66

    rejected = _put(api_client, campaign["id"], 3, {"type": "delete_stream", "stream_id": 902})

    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "stream_rule_violation"
    # The rejected mutation changed nothing.
    detail = _detail(api_client, campaign["id"])
    assert detail["campaign"]["revision"] == 3
    assert _stream(detail, 902)["state"] == "active"


def test_restore_stream_recalculates_and_can_return_to_clean(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _import_campaign(api_client, fake_keitaro)
    deleted = _put(api_client, campaign["id"], 1, {"type": "delete_stream", "stream_id": 902})
    assert deleted.json()["campaign"]["dirty"] is True

    restored = _put(api_client, campaign["id"], 2, {"type": "restore_stream", "stream_id": 902})

    assert restored.status_code == 200
    body = restored.json()
    stream = _stream(body, 902)
    assert stream["state"] == "active"
    assert stream["pinned"] is False
    # Recalculated shares match the baseline again, so the draft is clean.
    assert [(_stream(body, i)["share"]) for i in (900, 901, 902)] == [34, 33, 33]
    assert body["campaign"]["dirty"] is False
    assert body["campaign"]["sync_state"] == "clean"


def test_pin_stream_keeps_current_share_and_stays_clean(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _import_campaign(api_client, fake_keitaro)

    response = _put(api_client, campaign["id"], 1, {"type": "pin_stream", "stream_id": 900})

    assert response.status_code == 200
    body = response.json()
    assert _stream(body, 900)["pinned"] is True
    assert [(_stream(body, i)["share"]) for i in (900, 901, 902)] == [34, 33, 33]
    # Pin-only metadata never makes the draft dirty.
    assert body["campaign"]["dirty"] is False
    assert body["campaign"]["sync_state"] == "clean"
    assert body["campaign"]["revision"] == 2


def test_unpin_stream_redistributes_remainder(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _import_campaign(api_client, fake_keitaro)
    _put(api_client, campaign["id"], 1, {"type": "pin_stream", "stream_id": 900})
    _put(api_client, campaign["id"], 2, {"type": "delete_stream", "stream_id": 901})

    response = _put(api_client, campaign["id"], 3, {"type": "unpin_stream", "stream_id": 900})

    assert response.status_code == 200
    body = response.json()
    assert _stream(body, 900)["pinned"] is False
    assert _stream(body, 900)["share"] == 50
    assert _stream(body, 902)["share"] == 50


@pytest.mark.parametrize(
    "command,disable_first",
    [
        ({"type": "pin_stream", "stream_id": 901}, True),  # pin a disabled stream
        ({"type": "delete_stream", "stream_id": 901}, True),  # delete a disabled stream
        ({"type": "unpin_stream", "stream_id": 900}, False),  # unpin an unpinned stream
        ({"type": "restore_stream", "stream_id": 900}, False),  # restore an active stream
    ],
)
def test_invalid_state_transitions_are_422(
    api_client: TestClient,
    fake_keitaro: FakeKeitaroAdapter,
    command: dict,
    disable_first: bool,
) -> None:
    campaign = _import_campaign(api_client, fake_keitaro)
    revision = 1
    if disable_first:
        setup = _put(
            api_client, campaign["id"], revision, {"type": "delete_stream", "stream_id": 901}
        )
        assert setup.status_code == 200
        revision += 1

    response = _put(api_client, campaign["id"], revision, command)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "stream_rule_violation"


def test_stream_command_unknown_stream_is_404(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _import_campaign(api_client, fake_keitaro)

    response = _put(api_client, campaign["id"], 1, {"type": "delete_stream", "stream_id": 99999})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "stream_not_found"


# Offer commands ----------------------------------------------------------------


def test_add_offer_redistributes_and_persists(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    _seed_second_offer(fake_keitaro)
    campaign = _create_campaign(api_client)

    response = _put(
        api_client,
        campaign["id"],
        1,
        {"type": "add_offer", "stream_id": 901, "offer_id": SECOND_OFFER_ID},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["campaign"]["dirty"] is True
    offers = _stream(body, 901)["offers"]
    # Deterministic order: associations without a Keitaro ID sort first.
    assert [(o["offer_id"], o["state"], o["share"]) for o in offers] == [
        (SECOND_OFFER_ID, "active", 50),
        (DEFAULT_OFFER_ID, "active", 50),
    ]
    added = offers[0]
    assert added["offer_name"] == "Second Offer"
    assert added["keitaro_association_id"] is None
    # Stream-level shares are untouched by offer edits.
    assert _stream(body, 900)["share"] == 50
    assert _stream(body, 901)["share"] == 50


def test_add_offer_unknown_in_keitaro_is_422_without_changes(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)

    response = _put(
        api_client, campaign["id"], 1, {"type": "add_offer", "stream_id": 901, "offer_id": 999999}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "offer_unavailable"
    detail = _detail(api_client, campaign["id"])
    assert detail["campaign"]["revision"] == 1
    assert len(_stream(detail, 901)["offers"]) == 1


def test_add_offer_keitaro_timeout_is_504_without_changes(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    _seed_second_offer(fake_keitaro)
    campaign = _create_campaign(api_client)
    fake_keitaro.queue_failure("get_offer", KeitaroTimeoutError("timed out"))

    response = _put(
        api_client,
        campaign["id"],
        1,
        {"type": "add_offer", "stream_id": 901, "offer_id": SECOND_OFFER_ID},
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"
    assert _detail(api_client, campaign["id"])["campaign"]["revision"] == 1


def test_add_offer_keitaro_rejection_is_502(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    _seed_second_offer(fake_keitaro)
    campaign = _create_campaign(api_client)
    fake_keitaro.queue_failure("get_offer", KeitaroResponseError("get_offer", 500, "boom"))

    response = _put(
        api_client,
        campaign["id"],
        1,
        {"type": "add_offer", "stream_id": 901, "offer_id": SECOND_OFFER_ID},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


def test_add_offer_on_redirect_stream_is_422(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    _seed_second_offer(fake_keitaro)
    campaign = _create_campaign(api_client)

    response = _put(
        api_client,
        campaign["id"],
        1,
        {"type": "add_offer", "stream_id": 900, "offer_id": SECOND_OFFER_ID},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_stream_edit"


def test_remove_offer_disables_and_redistributes(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    _seed_second_offer(fake_keitaro)
    campaign = _create_campaign(api_client)
    _put(
        api_client,
        campaign["id"],
        1,
        {"type": "add_offer", "stream_id": 901, "offer_id": SECOND_OFFER_ID},
    )

    response = _put(
        api_client,
        campaign["id"],
        2,
        {"type": "remove_offer", "stream_id": 901, "offer_id": DEFAULT_OFFER_ID},
    )

    assert response.status_code == 200
    offers = _stream(response.json(), 901)["offers"]
    assert [(o["offer_id"], o["state"], o["share"]) for o in offers] == [
        (SECOND_OFFER_ID, "active", 100),
        (DEFAULT_OFFER_ID, "disabled", 0),
    ]


def test_remove_last_active_offer_of_active_stream_is_422(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)

    response = _put(
        api_client,
        campaign["id"],
        1,
        {"type": "remove_offer", "stream_id": 901, "offer_id": DEFAULT_OFFER_ID},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "offer_rule_violation"


def test_readding_removed_offer_reactivates_without_duplicate(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)
    _put(api_client, campaign["id"], 1, {"type": "delete_stream", "stream_id": 901})
    removed = _put(
        api_client,
        campaign["id"],
        2,
        {"type": "remove_offer", "stream_id": 901, "offer_id": DEFAULT_OFFER_ID},
    )
    assert removed.status_code == 200  # allowed while the stream is disabled

    readded = _put(
        api_client,
        campaign["id"],
        3,
        {"type": "add_offer", "stream_id": 901, "offer_id": DEFAULT_OFFER_ID},
    )

    assert readded.status_code == 200
    offers = _stream(readded.json(), 901)["offers"]
    assert len(offers) == 1
    assert offers[0]["offer_id"] == DEFAULT_OFFER_ID
    assert offers[0]["state"] == "active"
    assert offers[0]["share"] == 100
    assert offers[0]["keitaro_association_id"] == 7001  # same association, reactivated


def test_add_offer_already_active_is_422(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)

    response = _put(
        api_client,
        campaign["id"],
        1,
        {"type": "add_offer", "stream_id": 901, "offer_id": DEFAULT_OFFER_ID},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "offer_rule_violation"


def test_restore_offer_stream_without_active_offers_is_422(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)
    _put(api_client, campaign["id"], 1, {"type": "delete_stream", "stream_id": 901})
    _put(
        api_client,
        campaign["id"],
        2,
        {"type": "remove_offer", "stream_id": 901, "offer_id": DEFAULT_OFFER_ID},
    )

    response = _put(api_client, campaign["id"], 3, {"type": "restore_stream", "stream_id": 901})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "offer_rule_violation"
    detail = _detail(api_client, campaign["id"])
    assert _stream(detail, 901)["state"] == "disabled"
    assert detail["campaign"]["revision"] == 3


def test_remove_offer_unknown_offer_is_404(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)

    response = _put(
        api_client,
        campaign["id"],
        1,
        {"type": "remove_offer", "stream_id": 901, "offer_id": 424242},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "offer_not_found"


# Revisions, persistence, and blocked states -------------------------------------


def test_stale_revision_is_409_without_changes(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)

    response = _put(api_client, campaign["id"], 99, {"type": "delete_stream", "stream_id": 900})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "stale_revision"
    assert error["details"] == {"expected_revision": 99, "actual_revision": 1}
    detail = _detail(api_client, campaign["id"])
    assert detail["campaign"]["revision"] == 1
    assert _stream(detail, 900)["state"] == "active"


def test_mutations_persist_across_reload(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _import_campaign(api_client, fake_keitaro)
    _put(api_client, campaign["id"], 1, {"type": "pin_stream", "stream_id": 900})
    _put(api_client, campaign["id"], 2, {"type": "delete_stream", "stream_id": 901})

    detail = _detail(api_client, campaign["id"])

    assert detail["campaign"]["revision"] == 3
    assert _stream(detail, 900)["pinned"] is True
    assert _stream(detail, 901)["state"] == "disabled"
    assert _stream(detail, 901)["share"] == 0
    assert _stream(detail, 902)["share"] == 66


@pytest.mark.parametrize(
    "payload",
    [
        {"command": {"type": "delete_stream", "stream_id": 900}},  # no revision
        {"expected_revision": 1},  # no command
        {"expected_revision": -1, "command": {"type": "delete_stream", "stream_id": 900}},
        {"expected_revision": 1, "command": {"type": "rename_stream", "stream_id": 900}},
        {"expected_revision": 1, "command": {"type": "delete_stream", "stream_id": 0}},
        {"expected_revision": 1, "command": {"type": "delete_stream", "stream_id": "x"}},
        # Manual percentages and renames are not editable: extra fields fail.
        {
            "expected_revision": 1,
            "command": {"type": "delete_stream", "stream_id": 900, "share": 70},
        },
        {
            "expected_revision": 1,
            "command": {"type": "add_offer", "stream_id": 901, "offer_id": -5},
        },
    ],
)
def test_malformed_draft_requests_are_400(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter, payload: dict
) -> None:
    campaign = _create_campaign(api_client)

    response = api_client.put(f"/api/v1/campaigns/{campaign['id']}/draft", json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert _detail(api_client, campaign["id"])["campaign"]["revision"] == 1


def test_draft_put_unknown_campaign_is_404(api_client: TestClient) -> None:
    response = _put(api_client, str(uuid.uuid4()), 1, {"type": "delete_stream", "stream_id": 900})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_needs_attention_blocks_editing_and_marks_upload_blocked(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)
    with api_client.app.state.session_factory() as session:
        row = session.get(Campaign, uuid.UUID(campaign["id"]))
        row.sync_state = SyncState.NEEDS_ATTENTION
        session.commit()

    detail = _detail(api_client, campaign["id"])
    assert detail["upload_blocked"] is True

    response = _put(api_client, campaign["id"], 1, {"type": "delete_stream", "stream_id": 900})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "needs_attention"
    assert _detail(api_client, campaign["id"])["campaign"]["revision"] == 1
