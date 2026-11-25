import pytest
from pydantic import ValidationError

from app.keitaro.models import (
    KeitaroCampaign,
    KeitaroDomain,
    KeitaroGroup,
    KeitaroOffer,
    KeitaroStream,
    KeitaroTrafficSource,
)
from tests.keitaro.fixtures import (
    CAMPAIGN_JSON,
    DOMAIN_JSON,
    GROUP_JSON,
    OFFER_JSON,
    STREAM_LANDINGS_JSON,
    STREAM_REDIRECT_JSON,
    TRAFFIC_SOURCE_JSON,
)


def test_campaign_parses_and_preserves_unmanaged_fields() -> None:
    campaign = KeitaroCampaign.model_validate(CAMPAIGN_JSON)
    assert campaign.id == 223
    assert campaign.type == "weight"
    assert campaign.domain_id == 1813
    assert campaign.updated_at == "2026-11-24 22:35:00"
    dumped = campaign.model_dump()
    assert dumped["uniqueness_method"] == "ip"
    assert dumped["domain"] == "kt-emulator.test"


def test_landings_stream_parses_associations_and_filters() -> None:
    stream = KeitaroStream.model_validate(STREAM_LANDINGS_JSON)
    assert stream.schema_ == "landings"
    assert stream.action_type == "http"
    assert stream.is_active
    assert stream.filters[0].name == "country"
    assert stream.filters[0].mode == "reject"
    association = stream.offers[0]
    assert association.id == 693
    assert association.offer_id == 18001
    assert association.share == 100
    # Target instance reports null stream updated_at; models must tolerate it.
    assert stream.updated_at is None
    assert stream.created_at == "2026-11-24 22:30:47"


def test_redirect_stream_parses_documented_action_payload() -> None:
    stream = KeitaroStream.model_validate(STREAM_REDIRECT_JSON)
    assert stream.schema_ == "redirect"
    assert stream.action_type == "redirect"
    assert stream.action_payload == "https://www.google.com/"
    assert stream.offers == []


def test_offer_deleted_state_detection() -> None:
    offer = KeitaroOffer.model_validate(OFFER_JSON)
    assert not offer.is_deleted
    assert offer.country == ["CL"]
    deleted = KeitaroOffer.model_validate({**OFFER_JSON, "state": "deleted"})
    assert deleted.is_deleted


def test_configuration_resources_parse() -> None:
    assert KeitaroDomain.model_validate(DOMAIN_JSON).state == "active"
    assert KeitaroGroup.model_validate(GROUP_JSON).type == "campaigns"
    assert KeitaroTrafficSource.model_validate(TRAFFIC_SOURCE_JSON).state == "active"


@pytest.mark.parametrize(
    "model,payload",
    [
        (KeitaroCampaign, {k: v for k, v in CAMPAIGN_JSON.items() if k != "id"}),
        (KeitaroStream, {k: v for k, v in STREAM_LANDINGS_JSON.items() if k != "id"}),
        (KeitaroOffer, {k: v for k, v in OFFER_JSON.items() if k != "id"}),
        (KeitaroStream, {**STREAM_LANDINGS_JSON, "offers": [{"offer_id": 1}]}),
    ],
)
def test_missing_required_ids_are_rejected(model: type, payload: dict) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)
