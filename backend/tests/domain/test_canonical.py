"""Canonical projections and fingerprints (spec section 7.1)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.domain.canonical import (
    SHA256_HEX_LENGTH,
    build_canonical_document,
    canonical_json,
    fingerprint_canonical,
    fingerprint_remote,
)
from app.keitaro.models import KeitaroCampaign, KeitaroStream
from tests.keitaro.fixtures import (
    CAMPAIGN_JSON,
    STREAM_LANDINGS_JSON,
    STREAM_REDIRECT_JSON,
)


def campaign(**changes: Any) -> KeitaroCampaign:
    return KeitaroCampaign.model_validate({**CAMPAIGN_JSON, **changes})


def landings_stream(**changes: Any) -> KeitaroStream:
    return KeitaroStream.model_validate({**STREAM_LANDINGS_JSON, **changes})


def redirect_stream(**changes: Any) -> KeitaroStream:
    return KeitaroStream.model_validate({**STREAM_REDIRECT_JSON, **changes})


def fingerprint(c: KeitaroCampaign, streams: list[KeitaroStream]) -> str:
    return fingerprint_remote(c, streams)


def test_fingerprint_is_64_hex_chars() -> None:
    value = fingerprint(campaign(), [landings_stream(), redirect_stream()])
    assert len(value) == SHA256_HEX_LENGTH
    int(value, 16)  # valid hex


def test_stream_collection_order_does_not_matter() -> None:
    streams = [landings_stream(), redirect_stream()]
    assert fingerprint(campaign(), streams) == fingerprint(campaign(), list(reversed(streams)))


def test_filter_and_association_order_does_not_matter() -> None:
    stream = landings_stream(
        filters=list(reversed(landings_stream().filters)),
        offers=list(reversed(landings_stream().offers)),
    )
    assert fingerprint(campaign(), [stream]) == fingerprint(campaign(), [landings_stream()])


def test_json_formatting_and_key_order_do_not_matter() -> None:
    document = build_canonical_document(campaign(), [landings_stream(), redirect_stream()])
    first = fingerprint_canonical(document)
    # Re-serialize with indentation and no key sorting, then parse back.
    reparsed = json.loads(json.dumps(document, indent=4, sort_keys=False))
    assert fingerprint_canonical(reparsed) == first


def test_integral_floats_fingerprint_like_ints() -> None:
    as_float = landings_stream(
        weight=50.0, offers=[{"id": 693, "offer_id": 18001, "state": "active", "share": 100.0}]
    )
    assert fingerprint(campaign(), [as_float]) == fingerprint(campaign(), [landings_stream()])


MANAGED_CHANGES = [
    pytest.param(
        lambda: campaign(updated_at="2026-11-24 23:00:00"), None, id="campaign-updated-at"
    ),
    pytest.param(
        None,
        lambda: landings_stream(weight=51),
        id="stream-weight",
    ),
    pytest.param(
        None,
        lambda: landings_stream(state="disabled"),
        id="stream-state",
    ),
    pytest.param(
        None,
        lambda: landings_stream(position=5),
        id="stream-position",
    ),
    pytest.param(
        None,
        lambda: landings_stream(schema="redirect"),
        id="stream-schema",
    ),
    pytest.param(
        None,
        lambda: landings_stream(type="default"),
        id="stream-type",
    ),
    pytest.param(
        None,
        lambda: landings_stream(updated_at="2026-11-25 08:00:00"),
        id="stream-updated-at",
    ),
    pytest.param(
        None,
        lambda: landings_stream(action_payload="https://example.com/"),
        id="stream-action-payload",
    ),
    pytest.param(
        None,
        lambda: landings_stream(action_type="redirect"),
        id="stream-action-type",
    ),
    pytest.param(
        None,
        lambda: landings_stream(
            filters=[{"id": 51, "name": "country", "mode": "reject", "payload": ["FR"]}]
        ),
        id="filter-payload",
    ),
    pytest.param(
        None,
        lambda: landings_stream(
            filters=[{"id": 52, "name": "country", "mode": "reject", "payload": ["DE"]}]
        ),
        id="filter-id",
    ),
    pytest.param(
        None,
        lambda: landings_stream(
            offers=[{"id": 693, "offer_id": 18001, "state": "active", "share": 50}]
        ),
        id="association-share",
    ),
    pytest.param(
        None,
        lambda: landings_stream(
            offers=[{"id": 693, "offer_id": 18001, "state": "disabled", "share": 100}]
        ),
        id="association-state",
    ),
    pytest.param(
        None,
        lambda: landings_stream(
            offers=[{"id": 693, "offer_id": 18002, "state": "active", "share": 100}]
        ),
        id="association-offer-id",
    ),
]


@pytest.mark.parametrize(("campaign_change", "stream_change"), MANAGED_CHANGES)
def test_any_managed_remote_change_alters_fingerprint(campaign_change, stream_change) -> None:
    baseline = fingerprint(campaign(), [landings_stream()])
    changed_campaign = campaign_change() if campaign_change else campaign()
    streams = [stream_change()] if stream_change else [landings_stream()]
    assert fingerprint(changed_campaign, streams) != baseline


UNMANAGED_CHANGES = [
    pytest.param({"name": "Renamed campaign"}, None, id="campaign-name"),
    pytest.param({"group_id": 1}, None, id="campaign-group"),
    pytest.param({"alias": "other-alias"}, None, id="campaign-alias"),
    pytest.param({"brand_new_field": "x"}, None, id="campaign-unknown-extra"),
    pytest.param(None, {"name": "Renamed stream"}, id="stream-name"),
    pytest.param(None, {"comments": "edited"}, id="stream-comments"),
    pytest.param(None, {"created_at": "2020-01-01 00:00:00"}, id="stream-created-at"),
    pytest.param(None, {"triggers": [{"id": 1}]}, id="stream-triggers"),
    pytest.param(None, {"landings": [{"id": 9}]}, id="stream-landings-list"),
    pytest.param(None, {"offer_selection": "on_click"}, id="stream-offer-selection"),
    pytest.param(None, {"some_future_field": [1, 2]}, id="stream-unknown-extra"),
]


@pytest.mark.parametrize(("campaign_change", "stream_change"), UNMANAGED_CHANGES)
def test_unmanaged_changes_keep_fingerprint(campaign_change, stream_change) -> None:
    baseline = fingerprint(campaign(), [landings_stream()])
    changed_campaign = campaign(**(campaign_change or {}))
    streams = [landings_stream(**(stream_change or {}))]
    assert fingerprint(changed_campaign, streams) == baseline


def test_document_contains_only_managed_fields() -> None:
    document = build_canonical_document(campaign(), [redirect_stream()])
    campaign_doc = document["campaign"]
    assert set(campaign_doc) == {"id", "updated_at"}
    stream_doc = document["streams"][0]
    assert set(stream_doc) == {
        "action",
        "filters",
        "id",
        "offers",
        "position",
        "schema",
        "state",
        "type",
        "updated_at",
        "weight",
    }
    assert set(stream_doc["action"]) == {"options", "payload", "type"}
    assert stream_doc["offers"] == []


def test_canonical_json_is_deterministic() -> None:
    document = build_canonical_document(campaign(), [landings_stream()])
    assert canonical_json(document) == canonical_json(
        json.loads(json.dumps(document, sort_keys=False))
    )
