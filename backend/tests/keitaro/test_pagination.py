"""Index-endpoint pagination with limit/offset (plan stage 3)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from app.keitaro.client import KeitaroClient, KeitaroInvalidResponseError
from app.upstream_retry import NO_RETRY
from tests.keitaro.fixtures import OFFER_JSON

BASE_URL = "https://keitaro.test/admin_api/v1"


def offer_json(offer_id: int) -> dict:
    return {**OFFER_JSON, "id": offer_id}


@pytest.fixture
def client() -> Iterator[KeitaroClient]:
    with KeitaroClient(
        base_url=BASE_URL,
        api_key="secret-key",
        timeout_seconds=5.0,
        retry_policy=NO_RETRY,
    ) as c:
        yield c


@respx.mock
def test_list_fetches_all_pages_via_limit_offset(client: KeitaroClient) -> None:
    page_one = [offer_json(i) for i in range(1, 501)]
    page_two = [offer_json(i) for i in range(501, 504)]
    route = respx.get(f"{BASE_URL}/offers")
    route.side_effect = [
        httpx.Response(200, json=page_one),
        httpx.Response(200, json=page_two),
    ]

    offers = client.list_offers()

    assert [offer.id for offer in offers] == list(range(1, 504))
    sent = [dict(call.request.url.params) for call in route.calls]
    assert sent[0]["limit"] == "500"
    assert sent[0]["offset"] == "0"
    assert sent[1]["limit"] == "500"
    assert sent[1]["offset"] == "500"


@respx.mock
def test_short_page_stops_pagination(client: KeitaroClient) -> None:
    route = respx.get(f"{BASE_URL}/offers")
    route.side_effect = [httpx.Response(200, json=[offer_json(1), offer_json(2)])]

    assert [offer.id for offer in client.list_offers()] == [1, 2]
    assert len(route.calls) == 1


@respx.mock
def test_endpoint_ignoring_pagination_returns_full_collection_once(
    client: KeitaroClient,
) -> None:
    """An endpoint that ignores limit/offset and returns everything in one
    response is consumed once; the short page stops iteration."""
    route = respx.get(f"{BASE_URL}/offers")
    route.side_effect = [httpx.Response(200, json=[offer_json(i) for i in range(1, 4)])]

    assert [offer.id for offer in client.list_offers()] == [1, 2, 3]
    assert len(route.calls) == 1


@respx.mock
def test_endpoint_ignoring_pagination_with_large_collection_deduplicates(
    client: KeitaroClient,
) -> None:
    """If an endpoint ignores the parameters but returns more than one page's
    worth, the repeated page adds no new IDs and iteration stops."""
    full = [offer_json(i) for i in range(1, 601)]
    route = respx.get(f"{BASE_URL}/offers")
    route.side_effect = [httpx.Response(200, json=full), httpx.Response(200, json=full)]

    offers = client.list_offers()
    assert [offer.id for offer in offers] == list(range(1, 601))
    assert len(route.calls) == 2


@respx.mock
def test_page_cap_aborts_as_incomplete(
    client: KeitaroClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.keitaro.client._MAX_PAGES", 2)
    page_one = [offer_json(i) for i in range(1, 501)]
    page_two = [offer_json(i) for i in range(501, 1001)]
    route = respx.get(f"{BASE_URL}/offers")
    route.side_effect = [httpx.Response(200, json=page_one), httpx.Response(200, json=page_two)]

    with pytest.raises(KeitaroInvalidResponseError, match="pages"):
        client.list_offers()
    assert len(route.calls) == 2


@respx.mock
def test_non_list_page_is_rejected(client: KeitaroClient) -> None:
    respx.get(f"{BASE_URL}/offers").respond(json={"unexpected": "object"})
    with pytest.raises(KeitaroInvalidResponseError):
        client.list_offers()
