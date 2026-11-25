import json

import httpx
import pytest
import respx

from app.keitaro.client import (
    KeitaroClient,
    KeitaroInvalidResponseError,
    KeitaroResponseError,
    KeitaroTimeoutError,
)
from app.keitaro.payloads import (
    StreamUpdatePayload,
    build_campaign_create_payload,
)
from tests.keitaro.fixtures import (
    CAMPAIGN_JSON,
    OFFER_JSON,
    STREAM_LANDINGS_JSON,
    STREAM_REDIRECT_JSON,
)

BASE_URL = "https://keitaro.test/admin_api/v1"


@pytest.fixture
def client() -> KeitaroClient:
    with KeitaroClient(base_url=BASE_URL, api_key="secret-key", timeout_seconds=5.0) as c:
        yield c


@respx.mock
def test_get_campaign_sends_api_key_header_and_parses(client: KeitaroClient) -> None:
    route = respx.get(f"{BASE_URL}/campaigns/223").respond(json=CAMPAIGN_JSON)
    campaign = client.get_campaign(223)
    assert campaign.id == 223
    assert route.calls[0].request.headers["Api-Key"] == "secret-key"
    assert "Authorization" not in route.calls[0].request.headers


@respx.mock
def test_get_campaign_streams_parses_both_stream_kinds(client: KeitaroClient) -> None:
    respx.get(f"{BASE_URL}/campaigns/223/streams").respond(
        json=[STREAM_REDIRECT_JSON, STREAM_LANDINGS_JSON]
    )
    streams = client.get_campaign_streams(223)
    assert [stream.schema_ for stream in streams] == ["redirect", "landings"]
    assert streams[1].offers[0].offer_id == 18001


@respx.mock
def test_get_offer_parses(client: KeitaroClient) -> None:
    respx.get(f"{BASE_URL}/offers/18001").respond(json=OFFER_JSON)
    assert client.get_offer(18001).name == "Example Offer"


@respx.mock
def test_error_status_maps_to_response_error_with_redacted_snippet(
    client: KeitaroClient,
) -> None:
    respx.get(f"{BASE_URL}/campaigns/223").respond(
        status_code=422, text='{"error":"bad key secret-key"}'
    )
    with pytest.raises(KeitaroResponseError) as exc_info:
        client.get_campaign(223)
    assert exc_info.value.status_code == 422
    assert "secret-key" not in exc_info.value.snippet
    assert "***" in exc_info.value.snippet


@respx.mock
def test_timeout_maps_to_timeout_error(client: KeitaroClient) -> None:
    respx.get(f"{BASE_URL}/campaigns/223").mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(KeitaroTimeoutError):
        client.get_campaign(223)


@respx.mock
def test_malformed_json_maps_to_invalid_response(client: KeitaroClient) -> None:
    respx.get(f"{BASE_URL}/campaigns/223").respond(
        content=b"<html>not json</html>", headers={"Content-Type": "text/html"}
    )
    with pytest.raises(KeitaroInvalidResponseError):
        client.get_campaign(223)


@respx.mock
def test_missing_required_fields_map_to_invalid_response(client: KeitaroClient) -> None:
    respx.get(f"{BASE_URL}/campaigns/223").respond(json={"name": "no id here"})
    with pytest.raises(KeitaroInvalidResponseError):
        client.get_campaign(223)


@respx.mock
def test_non_list_collection_response_is_rejected(client: KeitaroClient) -> None:
    respx.get(f"{BASE_URL}/offers").respond(json={"unexpected": "object"})
    with pytest.raises(KeitaroInvalidResponseError):
        client.list_offers()


@respx.mock
def test_create_campaign_sends_exact_contract_payload(client: KeitaroClient) -> None:
    route = respx.post(f"{BASE_URL}/campaigns").respond(json=CAMPAIGN_JSON)
    payload = build_campaign_create_payload(
        name="Example DE", group_id=10332, traffic_source_id=836, domain_id=1813
    )
    campaign = client.create_campaign(payload)
    assert campaign.id == 223
    body = json.loads(route.calls[0].request.content)
    assert body == payload.to_api_dict()


@respx.mock
def test_failed_creation_is_not_retried(client: KeitaroClient) -> None:
    route = respx.post(f"{BASE_URL}/campaigns").respond(status_code=500, text="boom")
    payload = build_campaign_create_payload(
        name="Example DE", group_id=10332, traffic_source_id=836, domain_id=1813
    )
    with pytest.raises(KeitaroResponseError):
        client.create_campaign(payload)
    assert len(route.calls) == 1


@respx.mock
def test_update_stream_sends_partial_payload_only(client: KeitaroClient) -> None:
    route = respx.put(f"{BASE_URL}/streams/425").respond(json=STREAM_LANDINGS_JSON)
    client.update_stream(425, StreamUpdatePayload(weight=0, state="disabled"))
    body = json.loads(route.calls[0].request.content)
    assert body == {"weight": 0, "state": "disabled"}


@respx.mock
def test_disable_and_enable_stream_use_dedicated_endpoints(client: KeitaroClient) -> None:
    disable = respx.post(f"{BASE_URL}/streams/425/disable").respond(json={})
    enable = respx.post(f"{BASE_URL}/streams/425/enable").respond(json={})
    client.disable_stream(425)
    client.enable_stream(425)
    assert disable.called and enable.called
