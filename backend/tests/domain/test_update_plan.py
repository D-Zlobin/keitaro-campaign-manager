"""Baseline-versus-draft diffing: minimum partial-update plans, dirty state."""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.canonical import build_canonical_document
from app.domain.entries import DraftAssociationState, DraftStreamState
from app.domain.errors import DomainError
from app.domain.states import OfferAssociationState, StreamState
from app.domain.update_plan import is_dirty, plan_updates
from app.keitaro.models import KeitaroCampaign, KeitaroStream
from tests.keitaro.fixtures import CAMPAIGN_JSON, STREAM_LANDINGS_JSON

STREAM_ID = STREAM_LANDINGS_JSON["id"]
ASSOCIATION_ID = STREAM_LANDINGS_JSON["offers"][0]["id"]


def baseline_document() -> dict[str, Any]:
    campaign = KeitaroCampaign.model_validate(CAMPAIGN_JSON)
    stream = KeitaroStream.model_validate(STREAM_LANDINGS_JSON)
    return build_canonical_document(campaign, [stream])


def draft_stream(
    *,
    state: StreamState = StreamState.ACTIVE,
    share: int = 50,
    associations: tuple[DraftAssociationState, ...] | None = None,
) -> DraftStreamState:
    if associations is None:
        associations = (
            DraftAssociationState(
                offer_id=STREAM_LANDINGS_JSON["offers"][0]["offer_id"],
                state=OfferAssociationState.ACTIVE,
                share=100,
                keitaro_association_id=ASSOCIATION_ID,
            ),
        )
    return DraftStreamState(
        keitaro_id=STREAM_ID, state=state, share=share, associations=associations
    )


def test_unchanged_draft_generates_no_writes() -> None:
    baseline = baseline_document()
    assert plan_updates([draft_stream()], baseline) == []
    assert is_dirty([draft_stream()], baseline) is False


def test_pins_are_excluded_from_dirty_state() -> None:
    """Pin metadata alone can never produce a plan entry or dirty state."""
    baseline = baseline_document()
    draft = [draft_stream()]
    assert is_dirty(draft, baseline) is False  # pins are not even an input


def test_weight_change_plans_partial_update() -> None:
    baseline = baseline_document()
    (update,) = plan_updates([draft_stream(share=60)], baseline)
    assert update.keitaro_id == STREAM_ID
    assert update.state is None
    assert update.weight == 60
    assert update.associations == ()


def test_disabling_plans_state_and_zero_weight() -> None:
    baseline = baseline_document()
    (update,) = plan_updates([draft_stream(state=StreamState.DISABLED, share=0)], baseline)
    assert update.state is StreamState.DISABLED
    assert update.weight == 0


def test_association_share_change_plans_offer_only() -> None:
    baseline = baseline_document()
    draft = [
        draft_stream(
            associations=(
                DraftAssociationState(
                    offer_id=STREAM_LANDINGS_JSON["offers"][0]["offer_id"],
                    state=OfferAssociationState.ACTIVE,
                    share=50,
                    keitaro_association_id=ASSOCIATION_ID,
                ),
            )
        )
    ]
    (update,) = plan_updates(draft, baseline)
    assert update.state is None
    assert update.weight is None
    (association,) = update.associations
    assert association.keitaro_association_id == ASSOCIATION_ID
    assert association.share == 50
    assert association.state is OfferAssociationState.ACTIVE


def test_new_association_is_planned_as_addition() -> None:
    baseline = baseline_document()
    draft = [
        draft_stream(
            associations=(
                DraftAssociationState(
                    offer_id=18001,
                    state=OfferAssociationState.ACTIVE,
                    share=50,
                    keitaro_association_id=ASSOCIATION_ID,
                ),
                DraftAssociationState(
                    offer_id=18002,
                    state=OfferAssociationState.ACTIVE,
                    share=50,
                    keitaro_association_id=None,
                ),
            )
        )
    ]
    (update,) = plan_updates(draft, baseline)
    assert update.weight is None and update.state is None
    addition = next(a for a in update.associations if a.offer_id == 18002)
    assert addition.keitaro_association_id is None
    assert addition.state is OfferAssociationState.ACTIVE
    assert addition.share == 50
    changed = next(a for a in update.associations if a.offer_id == 18001)
    assert changed.share == 50  # redistributed from baseline 100


def test_removed_association_is_planned_as_disabled() -> None:
    baseline = baseline_document()
    draft = [
        draft_stream(
            associations=(
                DraftAssociationState(
                    offer_id=18001,
                    state=OfferAssociationState.DISABLED,
                    share=0,
                    keitaro_association_id=ASSOCIATION_ID,
                ),
            )
        )
    ]
    (update,) = plan_updates(draft, baseline)
    (association,) = update.associations
    assert association.keitaro_association_id == ASSOCIATION_ID
    assert association.state is OfferAssociationState.DISABLED
    assert association.share == 0
    assert is_dirty(draft, baseline) is True


def test_offer_change_includes_unchanged_active_associations_for_replacement() -> None:
    baseline = baseline_document()
    baseline["streams"][0]["offers"].append(
        {"id": ASSOCIATION_ID + 1, "offer_id": 18002, "state": "active", "share": 0}
    )
    draft = [
        draft_stream(
            associations=(
                DraftAssociationState(
                    offer_id=18001,
                    state=OfferAssociationState.ACTIVE,
                    share=100,
                    keitaro_association_id=ASSOCIATION_ID,
                ),
                DraftAssociationState(
                    offer_id=18002,
                    state=OfferAssociationState.DISABLED,
                    share=0,
                    keitaro_association_id=ASSOCIATION_ID + 1,
                ),
            )
        )
    ]

    (update,) = plan_updates(draft, baseline)
    assert [(a.offer_id, a.state) for a in update.associations] == [
        (18001, OfferAssociationState.ACTIVE),
        (18002, OfferAssociationState.DISABLED),
    ]


def test_locally_disabled_association_absent_from_baseline_is_clean() -> None:
    draft = [
        draft_stream(
            associations=(
                DraftAssociationState(
                    offer_id=18001,
                    state=OfferAssociationState.DISABLED,
                    share=0,
                    keitaro_association_id=None,
                ),
            )
        )
    ]

    baseline = baseline_document()
    baseline["streams"][0]["offers"] = []
    assert plan_updates(draft, baseline) == []
    assert is_dirty(draft, baseline) is False


def test_reactivated_association_planned_with_state_and_share() -> None:
    document = baseline_document()
    document["streams"][0]["offers"][0]["state"] = "disabled"
    draft = [draft_stream()]
    (update,) = plan_updates(draft, document)
    (association,) = update.associations
    assert association.state is OfferAssociationState.ACTIVE
    assert association.share == 100


def test_baseline_association_absent_from_draft_is_removal() -> None:
    baseline = baseline_document()
    draft = [draft_stream(associations=())]
    (update,) = plan_updates(draft, baseline)
    (association,) = update.associations
    assert association.keitaro_association_id == ASSOCIATION_ID
    assert association.state is OfferAssociationState.DISABLED


def test_mixed_changes_across_streams() -> None:
    campaign = KeitaroCampaign.model_validate(CAMPAIGN_JSON)
    second = KeitaroStream.model_validate(
        {**STREAM_LANDINGS_JSON, "id": 777, "weight": 50, "offers": []}
    )
    baseline = build_canonical_document(
        campaign,
        [
            KeitaroStream.model_validate(STREAM_LANDINGS_JSON),
            second,
        ],
    )
    draft = [
        draft_stream(share=70),  # weight change
        DraftStreamState(keitaro_id=777, state=StreamState.DISABLED, share=0, associations=()),
    ]
    updates = plan_updates(draft, baseline)
    assert [(u.keitaro_id, u.weight, u.state) for u in updates] == [
        (STREAM_ID, 70, None),
        (777, 0, StreamState.DISABLED),
    ]


def test_draft_stream_missing_from_baseline_rejected() -> None:
    with pytest.raises(DomainError):
        plan_updates(
            [DraftStreamState(keitaro_id=424242, state=StreamState.ACTIVE, share=100)],
            baseline_document(),
        )


def test_draft_association_missing_from_baseline_rejected() -> None:
    draft = [
        draft_stream(
            associations=(
                DraftAssociationState(
                    offer_id=18001,
                    state=OfferAssociationState.ACTIVE,
                    share=100,
                    keitaro_association_id=999999,
                ),
            )
        )
    ]
    with pytest.raises(DomainError):
        plan_updates(draft, baseline_document())


def test_disabled_stream_share_is_planned_as_zero_weight() -> None:
    """An active-in-baseline, disabled-in-draft stream always sends weight 0,
    even if its stored draft share were non-zero."""
    baseline = baseline_document()
    draft = [draft_stream(state=StreamState.DISABLED, share=0)]
    (update,) = plan_updates(draft, baseline)
    assert update.weight == 0
    assert update.state is StreamState.DISABLED


def test_dirty_reflects_any_planned_write() -> None:
    baseline = baseline_document()
    assert is_dirty([draft_stream(share=51)], baseline) is True
