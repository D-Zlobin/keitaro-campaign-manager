"""Catalog endpoints: Geo selector and offer autocomplete (plan stage 5).

The Geo selector is served from the backend's static ISO 3166-1 list; offer
autocomplete searches the Keitaro adapter's offers. These tests cover the
selector contracts, the active-association exclusion for the stream being
edited, and upstream failure mapping.
"""

from __future__ import annotations

import unicodedata
import uuid

import pytest
from fastapi.testclient import TestClient

from app.keitaro.client import KeitaroResponseError, KeitaroTimeoutError
from app.keitaro.models import KeitaroOffer
from tests.fakes import DEFAULT_OFFER_ID, FakeKeitaroAdapter

CREATE_BODY = {"name": "Example DE", "country_code": "DE", "offer_id": DEFAULT_OFFER_ID}


def _sort_key(name: str) -> str:
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()


def _create_campaign(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/campaigns", json=CREATE_BODY, headers={"Idempotency-Key": "key-1"}
    )
    assert response.status_code == 201
    return response.json()["campaign"]


def _add_offer(fake: FakeKeitaroAdapter, offer_id: int, name: str, state: str = "active") -> None:
    fake.offers[offer_id] = KeitaroOffer(id=offer_id, name=name, state=state)


# GET /geos --------------------------------------------------------------------


def test_list_geos_serves_the_static_iso_list(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/geos")

    assert response.status_code == 200
    geos = response.json()["geos"]
    assert len(geos) == 249
    assert {"code": "DE", "name": "Germany"} in geos
    assert {"code": "CL", "name": "Chile"} in geos
    # Alphabetically sorted by display name, accent-insensitive.
    names = [geo["name"] for geo in geos]
    assert names == sorted(names, key=_sort_key)
    assert names[:2] == ["Afghanistan", "Åland Islands"]


# GET /offers ------------------------------------------------------------------


def test_search_offers_returns_keitaro_matches(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    _add_offer(fake_keitaro, 18002, "Another Offer")
    _add_offer(fake_keitaro, 18003, "Example Two")

    response = api_client.get("/api/v1/offers", params={"search": "example"})

    assert response.status_code == 200
    assert response.json() == {
        "offers": [
            {"offer_id": DEFAULT_OFFER_ID, "name": "Example Offer"},
            {"offer_id": 18003, "name": "Example Two"},
        ]
    }


def test_search_offers_is_case_insensitive(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    response = api_client.get("/api/v1/offers", params={"search": "eXaMpLe"})

    assert response.status_code == 200
    assert response.json() == {"offers": [{"offer_id": DEFAULT_OFFER_ID, "name": "Example Offer"}]}


def test_search_offers_excludes_deleted_offers(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    _add_offer(fake_keitaro, 18002, "Example Deleted", state="deleted")

    response = api_client.get("/api/v1/offers", params={"search": "example"})

    assert response.status_code == 200
    assert response.json() == {"offers": [{"offer_id": DEFAULT_OFFER_ID, "name": "Example Offer"}]}


def test_search_offers_excludes_offers_active_in_the_edited_stream(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    _add_offer(fake_keitaro, 18002, "Example Two")
    campaign = _create_campaign(api_client)  # stream 901 actively holds offer 18001

    response = api_client.get(
        "/api/v1/offers",
        params={"search": "example", "campaign_id": campaign["id"], "stream_id": 901},
    )

    assert response.status_code == 200
    assert response.json() == {"offers": [{"offer_id": 18002, "name": "Example Two"}]}

    # Without the stream context there is no exclusion.
    unfiltered = api_client.get("/api/v1/offers", params={"search": "example"})
    assert [o["offer_id"] for o in unfiltered.json()["offers"]] == [DEFAULT_OFFER_ID, 18002]


def test_search_offers_keeps_disabled_associations_selectable(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)
    api_client.put(
        f"/api/v1/campaigns/{campaign['id']}/draft",
        json={"expected_revision": 1, "command": {"type": "delete_stream", "stream_id": 901}},
    )
    api_client.put(
        f"/api/v1/campaigns/{campaign['id']}/draft",
        json={
            "expected_revision": 2,
            "command": {"type": "remove_offer", "stream_id": 901, "offer_id": DEFAULT_OFFER_ID},
        },
    )

    response = api_client.get(
        "/api/v1/offers",
        params={"search": "example", "campaign_id": campaign["id"], "stream_id": 901},
    )

    # The disabled association may be re-added (reactivated), so it stays listed.
    assert response.status_code == 200
    assert response.json() == {"offers": [{"offer_id": DEFAULT_OFFER_ID, "name": "Example Offer"}]}


def test_search_offers_requires_both_exclusion_params(api_client: TestClient) -> None:
    campaign_id = str(uuid.uuid4())

    only_campaign = api_client.get(
        "/api/v1/offers", params={"search": "x", "campaign_id": campaign_id}
    )
    only_stream = api_client.get("/api/v1/offers", params={"search": "x", "stream_id": 901})

    assert only_campaign.status_code == 400
    assert only_campaign.json()["error"]["code"] == "bad_request"
    assert only_stream.status_code == 400


def test_search_offers_unknown_campaign_is_404(api_client: TestClient) -> None:
    response = api_client.get(
        "/api/v1/offers",
        params={"search": "x", "campaign_id": str(uuid.uuid4()), "stream_id": 901},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_search_offers_unknown_stream_is_404(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = _create_campaign(api_client)

    response = api_client.get(
        "/api/v1/offers",
        params={"search": "x", "campaign_id": campaign["id"], "stream_id": 424242},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "stream_not_found"


@pytest.mark.parametrize("params", [{}, {"search": ""}, {"search": "   "}])
def test_search_offers_rejects_empty_search(api_client: TestClient, params: dict) -> None:
    response = api_client.get("/api/v1/offers", params=params)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_search_offers_malformed_campaign_id_is_400(api_client: TestClient) -> None:
    response = api_client.get(
        "/api/v1/offers", params={"search": "x", "campaign_id": "nope", "stream_id": 901}
    )

    assert response.status_code == 400


def test_search_offers_upstream_failure_is_502(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("list_offers", KeitaroResponseError("list_offers", 500, "boom"))

    response = api_client.get("/api/v1/offers", params={"search": "x"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


def test_search_offers_timeout_is_504(
    api_client: TestClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("list_offers", KeitaroTimeoutError("timed out"))

    response = api_client.get("/api/v1/offers", params={"search": "x"})

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"
