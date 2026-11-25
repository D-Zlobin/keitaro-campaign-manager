"""Fetch normalization into draft-ready state (spec section 6.5)."""

from __future__ import annotations

from typing import Any

from app.keitaro.models import KeitaroStream
from app.keitaro.normalization import normalize_fetch
from tests.keitaro.fixtures import STREAM_LANDINGS_JSON, STREAM_REDIRECT_JSON

LANDINGS_ID = STREAM_LANDINGS_JSON["id"]
REDIRECT_ID = STREAM_REDIRECT_JSON["id"]


def landings(**changes: Any) -> KeitaroStream:
    return KeitaroStream.model_validate({**STREAM_LANDINGS_JSON, **changes})


def redirect(**changes: Any) -> KeitaroStream:
    return KeitaroStream.model_validate({**STREAM_REDIRECT_JSON, **changes})


def by_id(normalized) -> dict[int, Any]:
    return {item.keitaro_id: item for item in normalized.streams}


def shares(result) -> dict[int, int]:
    return {item.keitaro_id: item.share for item in result.streams}


def test_valid_fetch_passes_through_clean() -> None:
    result = normalize_fetch([landings(), redirect()])
    assert result.dirty is False
    assert result.notices == ()
    assert shares(result) == {LANDINGS_ID: 50, REDIRECT_ID: 50}
    first, second = result.streams
    assert first.state.value == "active"
    assert second.state.value == "active"


def test_normalized_streams_carry_no_pin_state() -> None:
    """Normalization emits pin-free state (spec 6.5 step 3: clear all pins)."""
    result = normalize_fetch([landings()])
    assert not hasattr(result.streams[0], "pinned")


def test_offer_capability_derived_from_schema() -> None:
    result = normalize_fetch([landings(), redirect()])
    items = by_id(result)
    assert items[LANDINGS_ID].offer_capable is True
    assert items[REDIRECT_ID].offer_capable is False


def test_disabled_stream_zeroed_and_kept() -> None:
    result = normalize_fetch([landings(state="disabled"), redirect(weight=100)])
    disabled = by_id(result)[LANDINGS_ID]
    assert disabled.state.value == "disabled"
    assert disabled.share == 0
    assert shares(result)[REDIRECT_ID] == 100
    assert result.dirty is False


def test_disabled_stream_keeps_remote_offer_shares() -> None:
    """Spec 6.5 normalizes active streams only."""
    result = normalize_fetch([landings(state="disabled")])
    disabled = by_id(result)[LANDINGS_ID]
    assert [offer.share for offer in disabled.offers] == [100]
    assert result.dirty is False


def test_remotely_deleted_known_stream_reported_not_restored() -> None:
    result = normalize_fetch([landings()], known_stream_ids=[LANDINGS_ID, 999])
    assert 999 not in by_id(result)
    assert any("999" in notice and "deleted remotely" in notice for notice in result.notices)


def test_unexpected_remote_state_omitted_with_notice() -> None:
    result = normalize_fetch([landings(state="pending_delete")])
    assert result.streams == ()
    assert any("pending_delete" in notice for notice in result.notices)


def test_invalid_stream_total_redistributed_with_notice_and_dirty() -> None:
    result = normalize_fetch([landings(weight=60), redirect(weight=60)])
    assert result.dirty is True
    assert shares(result) == {LANDINGS_ID: 50, REDIRECT_ID: 50}
    assert any("total 120" in notice for notice in result.notices)


def test_fractional_stream_weights_redistributed() -> None:
    result = normalize_fetch(
        [
            landings(weight=33.33),
            landings(id=426, position=1, weight=33.33),
            landings(id=427, position=2, weight=33.34),
        ]
    )
    assert result.dirty is True
    assert shares(result) == {425: 34, 426: 33, 427: 33}
    assert any("redistributed equally" in notice for notice in result.notices)


def test_single_active_stream_at_100_kept() -> None:
    result = normalize_fetch([landings(weight=100)])
    assert shares(result) == {LANDINGS_ID: 100}
    assert result.dirty is False


def test_all_disabled_campaign_is_clean() -> None:
    result = normalize_fetch([landings(state="disabled"), redirect(state="disabled")])
    assert shares(result) == {LANDINGS_ID: 0, REDIRECT_ID: 0}
    assert result.dirty is False
    assert result.notices == ()


def test_invalid_offer_total_normalized_with_notice_and_dirty() -> None:
    offers = [
        {"id": 693, "offer_id": 18001, "state": "active", "share": 60},
        {"id": 694, "offer_id": 18002, "state": "active", "share": 60},
    ]
    result = normalize_fetch([landings(offers=offers)])
    assert result.dirty is True
    offer_shares = {offer.offer_id: offer.share for offer in result.streams[0].offers}
    assert offer_shares == {18001: 50, 18002: 50}
    assert any("offer shares total 120" in notice for notice in result.notices)


def test_fractional_offer_shares_normalized() -> None:
    offers = [
        {"id": 693, "offer_id": 18001, "state": "active", "share": 33.33},
        {"id": 694, "offer_id": 18002, "state": "active", "share": 33.33},
        {"id": 695, "offer_id": 18003, "state": "active", "share": 33.34},
    ]
    result = normalize_fetch([landings(offers=offers)])
    assert result.dirty is True
    offer_shares = {offer.offer_id: offer.share for offer in result.streams[0].offers}
    assert offer_shares == {18001: 34, 18002: 33, 18003: 33}


def test_single_active_offer_at_100_kept() -> None:
    result = normalize_fetch([landings(weight=100)])
    assert [offer.share for offer in result.streams[0].offers] == [100]
    assert result.dirty is False


def test_offer_names_resolved_from_mapping_with_fallback() -> None:
    named = normalize_fetch([landings()], offer_names={18001: "Example Offer"})
    assert named.streams[0].offers[0].offer_name == "Example Offer"
    unnamed = normalize_fetch([landings()])
    assert unnamed.streams[0].offers[0].offer_name == "Offer 18001"


def test_baseline_payload_preserves_raw_remote_stream() -> None:
    result = normalize_fetch([landings()])
    payload = result.streams[0].baseline_payload
    assert payload["id"] == LANDINGS_ID
    assert payload["schema"] == "landings"
    assert payload["weight"] == 50
    # Unmanaged extras survive for compensation and diagnostics.
    assert payload["comments"] == ""
    assert payload["offers"][0]["id"] == 693


def test_offer_baseline_payload_preserves_raw_association() -> None:
    result = normalize_fetch([landings()])
    offer = result.streams[0].offers[0]
    assert offer.baseline_payload is not None
    assert offer.baseline_payload["id"] == 693
    assert offer.baseline_payload["offer_id"] == 18001
    assert offer.baseline_payload["share"] == 100


def test_offer_baseline_payload_keeps_raw_share_after_normalization() -> None:
    skewed = landings(
        offers=[
            {"id": 693, "offer_id": 18001, "state": "active", "share": 30},
            {"id": 694, "offer_id": 18002, "state": "active", "share": 30},
        ]
    )
    result = normalize_fetch([skewed])
    offers = result.streams[0].offers
    assert [offer.share for offer in offers] == [50, 50]  # normalized draft
    assert [offer.baseline_payload["share"] for offer in offers] == [30, 30]  # raw baseline


def test_non_offer_stream_offers_pass_through_untouched() -> None:
    redirect_with_offers = redirect(
        weight=100,
        offers=[{"id": 700, "offer_id": 18001, "state": "active", "share": 60}],
    )
    result = normalize_fetch([redirect_with_offers])
    assert result.dirty is False
    assert [offer.share for offer in result.streams[0].offers] == [60]
