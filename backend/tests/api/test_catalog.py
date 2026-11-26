"""Catalog endpoints: Geo selector and offer autocomplete (plan stage 5).

The browser talks only to the backend; these tests cover the selector
contracts, the active-association exclusion for the stream being edited,
upstream failure mapping, and credential containment.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.schemas import OfferOption
from app.config import Settings
from app.five.client import FiveApiClient
from app.five.errors import FiveApiResponseError, FiveApiTimeoutError
from app.main import create_app
from app.upstream_retry import NO_RETRY
from tests.fakes import DEFAULT_OFFER_ID, FakeFiveApiClient, FakeKeitaroAdapter

CREATE_BODY = {"name": "Example DE", "country_code": "DE", "offer_id": DEFAULT_OFFER_ID}


def _create_campaign(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/campaigns", json=CREATE_BODY, headers={"Idempotency-Key": "key-1"}
    )
    assert response.status_code == 201
    return response.json()["campaign"]


# GET /geos --------------------------------------------------------------------


def test_list_geos_returns_selector_options(
    api_client: TestClient, fake_five: FakeFiveApiClient
) -> None:
    response = api_client.get("/api/v1/geos")

    assert response.status_code == 200
    assert response.json() == {
        "geos": [
            {"code": "DE", "name": "Germany"},
            {"code": "CL", "name": "Chile"},
        ]
    }
    assert fake_five.calls == ["list_geos"]


def test_list_geos_empty_result(api_client: TestClient, fake_five: FakeFiveApiClient) -> None:
    fake_five.geos = []

    response = api_client.get("/api/v1/geos")

    assert response.status_code == 200
    assert response.json() == {"geos": []}


def test_list_geos_upstream_failure_is_502(
    api_client: TestClient, fake_five: FakeFiveApiClient
) -> None:
    fake_five.failure = FiveApiResponseError("list_geos", 500, "boom")

    response = api_client.get("/api/v1/geos")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


def test_list_geos_timeout_is_504(api_client: TestClient, fake_five: FakeFiveApiClient) -> None:
    fake_five.failure = FiveApiTimeoutError("timed out")

    response = api_client.get("/api/v1/geos")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"


def test_list_geos_unconfigured_is_503(
    api_settings: Settings, fake_keitaro: FakeKeitaroAdapter
) -> None:
    client = TestClient(create_app(api_settings, keitaro=fake_keitaro))

    response = client.get("/api/v1/geos")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


# GET /offers ------------------------------------------------------------------


def test_search_offers_returns_matches(
    api_client: TestClient, fake_five: FakeFiveApiClient
) -> None:
    fake_five.offers.append(OfferOption(offer_id=18002, name="Another Offer"))

    response = api_client.get("/api/v1/offers", params={"search": "example"})

    assert response.status_code == 200
    assert response.json() == {"offers": [{"offer_id": DEFAULT_OFFER_ID, "name": "Example Offer"}]}


def test_search_offers_excludes_offers_active_in_the_edited_stream(
    api_client: TestClient, fake_five: FakeFiveApiClient, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_five.offers.append(OfferOption(offer_id=18002, name="Example Two"))
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
    api_client: TestClient, fake_five: FakeFiveApiClient, fake_keitaro: FakeKeitaroAdapter
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
    api_client: TestClient, fake_five: FakeFiveApiClient
) -> None:
    fake_five.failure = FiveApiResponseError("search_offers", 503, "boom")

    response = api_client.get("/api/v1/offers", params={"search": "x"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


def test_search_offers_timeout_is_504(api_client: TestClient, fake_five: FakeFiveApiClient) -> None:
    fake_five.failure = FiveApiTimeoutError("timed out")

    response = api_client.get("/api/v1/offers", params={"search": "x"})

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"


def test_search_offers_unconfigured_is_503(
    api_settings: Settings, fake_keitaro: FakeKeitaroAdapter
) -> None:
    client = TestClient(create_app(api_settings, keitaro=fake_keitaro))

    response = client.get("/api/v1/offers", params={"search": "x"})

    assert response.status_code == 503


def test_five_api_key_never_reaches_responses(
    api_settings: Settings, fake_keitaro: FakeKeitaroAdapter
) -> None:
    """Even an upstream error body echoing the credential stays server-side."""
    secret = "five-secret-key"

    def failing_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"provider error for key {secret}")

    five = FiveApiClient(
        base_url="https://five.test",
        api_key=secret,
        transport=httpx.MockTransport(failing_transport),
        retry_policy=NO_RETRY,
    )
    client = TestClient(create_app(api_settings, keitaro=fake_keitaro, five=five))

    responses = [
        client.get("/api/v1/offers", params={"search": "x"}),
        client.get("/api/v1/geos"),
    ]

    for response in responses:
        assert response.status_code == 502
        assert secret not in response.text
