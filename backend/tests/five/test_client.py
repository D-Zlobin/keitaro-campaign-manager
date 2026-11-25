"""Five API backend adapter: Geo list and offer autocomplete (plan stage 3).

Tests run against the assumed contract documented in ``app.five``; the
credentials, auth shape, search parameter, and offer-ID mapping remain
unverified upstream and are covered here as the executable specification.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest
import respx

from app.five.client import FiveApiClient
from app.five.errors import (
    FiveApiInvalidResponseError,
    FiveApiResponseError,
    FiveApiTimeoutError,
)
from app.upstream_retry import NO_RETRY, RetryPolicy

BASE_URL = "https://five.test"


def make_client(sleeper: Callable[[float], None] | None = None) -> FiveApiClient:
    return FiveApiClient(
        base_url=BASE_URL,
        api_key="five-secret",
        timeout_seconds=5.0,
        retry_policy=RetryPolicy(sleeper=sleeper or (lambda _: None)),
    )


@pytest.fixture
def client() -> Iterator[FiveApiClient]:
    with make_client() as c:
        yield c


@respx.mock
def test_list_geos_sends_bearer_and_maps_to_geo_options(
    client: FiveApiClient,
) -> None:
    route = respx.get(f"{BASE_URL}/geos").respond(
        json=[
            {"code": "de", "name": "Germany"},
            {"code": "cl", "name": "Chile"},
        ]
    )
    geos = client.list_geos()
    assert [(geo.code, geo.name) for geo in geos] == [
        ("CL", "Chile"),
        ("DE", "Germany"),
    ]
    assert route.calls[0].request.headers["Authorization"] == "Bearer five-secret"


@respx.mock
def test_list_geos_empty_result(client: FiveApiClient) -> None:
    respx.get(f"{BASE_URL}/geos").respond(json=[])
    assert client.list_geos() == []


@respx.mock
def test_list_geos_malformed_json_rejected(client: FiveApiClient) -> None:
    respx.get(f"{BASE_URL}/geos").respond(content=b"<html>", headers={"Content-Type": "text/html"})
    with pytest.raises(FiveApiInvalidResponseError):
        client.list_geos()


@respx.mock
def test_list_geos_non_list_rejected(client: FiveApiClient) -> None:
    respx.get(f"{BASE_URL}/geos").respond(json={"data": []})
    with pytest.raises(FiveApiInvalidResponseError, match="non-list"):
        client.list_geos()


@respx.mock
def test_list_geos_invalid_entry_rejected(client: FiveApiClient) -> None:
    respx.get(f"{BASE_URL}/geos").respond(json=[{"code": "DE"}])  # missing name
    with pytest.raises(FiveApiInvalidResponseError, match="validation"):
        client.list_geos()


@respx.mock
def test_list_geos_bad_code_rejected(client: FiveApiClient) -> None:
    respx.get(f"{BASE_URL}/geos").respond(json=[{"code": "deu", "name": "Germany"}])
    with pytest.raises(FiveApiInvalidResponseError):
        client.list_geos()


@respx.mock
def test_search_offers_sends_search_param_and_maps_resolved_ids(
    client: FiveApiClient,
) -> None:
    route = respx.get(f"{BASE_URL}/offers").respond(
        json=[
            {"keitaro_offer_id": 42, "name": "Zeta"},
            {"keitaro_offer_id": 7, "name": "Alpha"},
        ]
    )
    offers = client.search_offers("term")
    assert [(offer.offer_id, offer.name) for offer in offers] == [
        (7, "Alpha"),
        (42, "Zeta"),
    ]
    assert route.calls[0].request.url.params["search"] == "term"


@respx.mock
def test_search_offers_empty_result(client: FiveApiClient) -> None:
    respx.get(f"{BASE_URL}/offers").respond(json=[])
    assert client.search_offers("nothing") == []


@respx.mock
def test_search_offers_entry_missing_keitaro_id_rejected(client: FiveApiClient) -> None:
    respx.get(f"{BASE_URL}/offers").respond(json=[{"id": 5, "name": "No Keitaro ID"}])
    with pytest.raises(FiveApiInvalidResponseError, match="validation"):
        client.search_offers("x")


@respx.mock
def test_search_offers_requires_non_empty_term(client: FiveApiClient) -> None:
    with pytest.raises(ValueError):
        client.search_offers("   ")


@respx.mock
def test_timeout_retries_then_exhausts(client: FiveApiClient) -> None:
    route = respx.get(f"{BASE_URL}/geos").mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(FiveApiTimeoutError):
        client.list_geos()
    assert len(route.calls) == 3


@respx.mock
def test_retryable_5xx_retries_then_exhausts(client: FiveApiClient) -> None:
    route = respx.get(f"{BASE_URL}/geos").respond(status_code=503, text="down")
    with pytest.raises(FiveApiResponseError) as exc_info:
        client.list_geos()
    assert exc_info.value.status_code == 503
    assert len(route.calls) == 3


@respx.mock
def test_non_retryable_status_fails_immediately(client: FiveApiClient) -> None:
    route = respx.get(f"{BASE_URL}/geos").respond(status_code=401, text="bad token five-secret")
    with pytest.raises(FiveApiResponseError) as exc_info:
        client.list_geos()
    assert len(route.calls) == 1
    # The credential never appears in error snippets.
    assert "five-secret" not in exc_info.value.snippet
    assert "***" in exc_info.value.snippet


@respx.mock
def test_retry_recovers_with_valid_result() -> None:
    delays: list[float] = []
    with make_client(sleeper=delays.append) as client:
        route = respx.get(f"{BASE_URL}/geos")
        route.mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(200, json=[{"code": "DE", "name": "Germany"}]),
            ]
        )
        assert [geo.code for geo in client.list_geos()] == ["DE"]
    assert delays == [0.5]


@respx.mock
def test_no_retries_when_policy_disables_them() -> None:
    with FiveApiClient(
        base_url=BASE_URL,
        api_key="five-secret",
        timeout_seconds=5.0,
        retry_policy=NO_RETRY,
    ) as client:
        route = respx.get(f"{BASE_URL}/geos").respond(status_code=503, text="down")
        with pytest.raises(FiveApiResponseError):
            client.list_geos()
        assert len(route.calls) == 1
