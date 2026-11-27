import re

import pytest
from pydantic import ValidationError

from app.keitaro.payloads import (
    CampaignCreatePayload,
    OfferAssociationPayload,
    StreamUpdatePayload,
    build_campaign_create_payload,
    build_google_stream_payload,
    build_offer_stream_payload,
)


def test_campaign_create_payload_matches_verified_contract() -> None:
    payload = build_campaign_create_payload(
        name="Example DE", group_id=10332, traffic_source_id=836, domain_id=1813
    )
    body = payload.to_api_dict()
    alias = body.pop("alias")
    assert re.fullmatch(r"[0-9a-f]{12}", alias)
    assert body == {
        "name": "Example DE",
        "type": "weight",
        "state": "active",
        "group_id": 10332,
        "traffic_source_id": 836,
        "domain_id": 1813,
    }


def test_campaign_create_payload_rejects_empty_alias() -> None:
    with pytest.raises(ValidationError):
        CampaignCreatePayload(
            name="Example DE",
            alias="",
            group_id=10332,
            traffic_source_id=836,
            domain_id=1813,
        )


def test_google_stream_payload_matches_verified_contract() -> None:
    payload = build_google_stream_payload(
        campaign_id=223, country_code="de", redirect_url="https://www.google.com/"
    )
    assert payload.to_api_dict() == {
        "campaign_id": 223,
        "schema": "redirect",
        "type": "regular",
        "name": "Google — DE",
        "position": 0,
        "weight": 50,
        "state": "active",
        "action_type": "redirect",
        "action_payload": "https://www.google.com/",
        "filters": [{"name": "country", "mode": "accept", "payload": ["DE"]}],
        "offers": [],
    }


def test_google_stream_payload_supports_action_options_representation() -> None:
    payload = build_google_stream_payload(
        campaign_id=223,
        country_code="DE",
        redirect_url="https://www.google.com/",
        redirect_via="action_options",
    )
    body = payload.to_api_dict()
    assert body["action_options"] == {"url": "https://www.google.com/"}
    assert "action_payload" not in body


def test_offer_stream_payload_matches_verified_contract() -> None:
    payload = build_offer_stream_payload(campaign_id=223, offer_id=18001, country_code="de")
    assert payload.to_api_dict() == {
        "campaign_id": 223,
        "schema": "landings",
        "type": "regular",
        "name": "Offer — 18001",
        "position": 1,
        "weight": 50,
        "state": "active",
        "action_type": "http",
        "filters": [{"name": "country", "mode": "reject", "payload": ["DE"]}],
        "offers": [{"offer_id": 18001, "share": 100, "state": "active"}],
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"offer_id": 0, "share": 100},
        {"offer_id": 1, "share": 101},
        {"offer_id": 1, "share": -1},
    ],
)
def test_offer_association_payload_validates_bounds(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        OfferAssociationPayload(**kwargs)


def test_stream_update_payload_sends_only_changed_fields() -> None:
    payload = StreamUpdatePayload(weight=0, state="disabled")
    assert payload.to_api_dict() == {"weight": 0, "state": "disabled"}


def test_stream_update_payload_serializes_set_fields_completely() -> None:
    payload = StreamUpdatePayload(
        offers=[OfferAssociationPayload(offer_id=18001, share=0, state="disabled")]
    )
    assert payload.to_api_dict() == {
        "offers": [{"offer_id": 18001, "share": 0, "state": "disabled"}]
    }


def test_stream_update_payload_can_clear_action_payload_explicitly() -> None:
    payload = StreamUpdatePayload(action_payload=None, weight=25)
    assert payload.to_api_dict() == {"action_payload": None, "weight": 25}
