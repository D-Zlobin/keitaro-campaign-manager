"""Bounded retries: safe reads only, never writes (plan stage 3)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest
import respx

from app.keitaro.client import (
    KeitaroClient,
    KeitaroConnectionError,
    KeitaroInvalidResponseError,
    KeitaroResponseError,
    KeitaroTimeoutError,
)
from app.keitaro.payloads import (
    StreamUpdatePayload,
    build_campaign_create_payload,
)
from app.upstream_retry import RetryPolicy
from tests.keitaro.fixtures import CAMPAIGN_JSON

BASE_URL = "https://keitaro.test/admin_api/v1"


def make_client(sleeper: Callable[[float], None] | None = None) -> KeitaroClient:
    return KeitaroClient(
        base_url=BASE_URL,
        api_key="secret-key",
        timeout_seconds=5.0,
        retry_policy=RetryPolicy(sleeper=sleeper or (lambda _: None)),
    )


@pytest.fixture
def client() -> Iterator[KeitaroClient]:
    with make_client() as c:
        yield c


@respx.mock
def test_safe_read_retries_transient_5xx_until_success(client: KeitaroClient) -> None:
    route = respx.get(f"{BASE_URL}/campaigns/223")
    route.mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(502),
            httpx.Response(200, json=CAMPAIGN_JSON),
        ]
    )
    assert client.get_campaign(223).id == 223
    assert len(route.calls) == 3


@respx.mock
def test_safe_read_retries_429_until_success(client: KeitaroClient) -> None:
    route = respx.get(f"{BASE_URL}/campaigns/223")
    route.mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=CAMPAIGN_JSON),
        ]
    )
    assert client.get_campaign(223).id == 223
    assert len(route.calls) == 2


@respx.mock
def test_safe_read_retries_timeouts_until_success(client: KeitaroClient) -> None:
    route = respx.get(f"{BASE_URL}/campaigns/223")
    route.mock(
        side_effect=[
            httpx.ReadTimeout("too slow"),
            httpx.ConnectTimeout("too slow"),
            httpx.Response(200, json=CAMPAIGN_JSON),
        ]
    )
    assert client.get_campaign(223).id == 223
    assert len(route.calls) == 3


@respx.mock
def test_retry_exhaustion_on_5xx_raises_response_error(client: KeitaroClient) -> None:
    route = respx.get(f"{BASE_URL}/campaigns/223")
    route.mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(503),
            httpx.Response(503),
        ]
    )
    with pytest.raises(KeitaroResponseError) as exc_info:
        client.get_campaign(223)
    assert exc_info.value.status_code == 503
    assert len(route.calls) == 3  # initial + 2 retries


@respx.mock
def test_retry_exhaustion_on_timeout_raises_timeout_error(client: KeitaroClient) -> None:
    route = respx.get(f"{BASE_URL}/campaigns/223").mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(KeitaroTimeoutError):
        client.get_campaign(223)
    assert len(route.calls) == 3


@respx.mock
def test_non_retryable_4xx_fails_immediately(client: KeitaroClient) -> None:
    route = respx.get(f"{BASE_URL}/campaigns/223").respond(status_code=404, text="missing")
    with pytest.raises(KeitaroResponseError):
        client.get_campaign(223)
    assert len(route.calls) == 1


@respx.mock
def test_connection_errors_are_not_retried(client: KeitaroClient) -> None:
    route = respx.get(f"{BASE_URL}/campaigns/223").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(KeitaroConnectionError):
        client.get_campaign(223)
    assert len(route.calls) == 1


@respx.mock
def test_write_timeout_is_never_retried(client: KeitaroClient) -> None:
    route = respx.put(f"{BASE_URL}/streams/425").mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(KeitaroTimeoutError):
        client.update_stream(425, StreamUpdatePayload(weight=0))
    assert len(route.calls) == 1


@respx.mock
def test_write_5xx_is_never_retried(client: KeitaroClient) -> None:
    route = respx.post(f"{BASE_URL}/campaigns").respond(status_code=503, text="down")
    payload = build_campaign_create_payload(
        name="Example DE", group_id=10332, traffic_source_id=836, domain_id=1813
    )
    with pytest.raises(KeitaroResponseError):
        client.create_campaign(payload)
    assert len(route.calls) == 1


@respx.mock
def test_backoff_delays_are_exponential() -> None:
    delays: list[float] = []
    with make_client(sleeper=delays.append) as client:
        route = respx.get(f"{BASE_URL}/campaigns/223")
        route.mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(200, json=CAMPAIGN_JSON),
            ]
        )
        assert client.get_campaign(223).id == 223
    assert delays == [0.5, 1.0]


@respx.mock
def test_successful_response_after_retry_still_validated(client: KeitaroClient) -> None:
    route = respx.get(f"{BASE_URL}/campaigns/223")
    route.mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"name": "no id"}),
        ]
    )
    with pytest.raises(KeitaroInvalidResponseError):
        client.get_campaign(223)
