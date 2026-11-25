"""Offer association editing per specification section 6.8."""

from __future__ import annotations

import pytest

from app.domain.entries import OfferEntry, StreamEntry
from app.domain.errors import (
    OfferNotFoundError,
    OfferRuleViolationError,
    UnsupportedStreamEditError,
)
from app.domain.offers import (
    add_offer,
    redistribute_offer_shares,
    remove_offer,
    validate_active_stream_offers,
)
from app.domain.states import OfferAssociationState, StreamState


def o(
    offer_id: int,
    *,
    state: OfferAssociationState = OfferAssociationState.ACTIVE,
    share: int = 0,
    keitaro_association_id: int | None = None,
) -> OfferEntry:
    return OfferEntry(
        offer_id=offer_id,
        state=state,
        share=share,
        keitaro_association_id=keitaro_association_id,
    )


def stream(*, state: StreamState = StreamState.ACTIVE, offer_capable: bool = True) -> StreamEntry:
    return StreamEntry(
        keitaro_id=900,
        position=0,
        state=state,
        share=100,
        offer_capable=offer_capable,
    )


def shares(offers: list[OfferEntry]) -> dict[int, int]:
    return {offer.offer_id: offer.share for offer in offers}


def states(offers: list[OfferEntry]) -> dict[int, str]:
    return {offer.offer_id: offer.state.value for offer in offers}


def test_add_first_offer_gets_full_share() -> None:
    result = add_offer(stream(), [], 2001)
    assert shares(result) == {2001: 100}


def test_add_redistributes_equally() -> None:
    offers = [o(2001, share=100, keitaro_association_id=10)]
    result = add_offer(stream(), offers, 2002, keitaro_association_id=11)
    assert shares(result) == {2001: 50, 2002: 50}
    third = add_offer(
        stream(),
        [
            o(2001, share=50, keitaro_association_id=10),
            o(2002, share=50, keitaro_association_id=11),
        ],
        2003,
        keitaro_association_id=12,
    )
    assert shares(third) == {2001: 34, 2002: 33, 2003: 33}


def test_new_offer_sorts_before_known_associations() -> None:
    """A locally added offer (no Keitaro ID yet) sorts as ID 0 for rounding."""
    offers = [o(2001, share=100, keitaro_association_id=5)]
    result = add_offer(stream(), offers, 2002)  # new: no association ID
    assert shares(result) == {2001: 50, 2002: 50}


def test_rounding_order_is_association_id_then_offer_id() -> None:
    offers = [
        o(5001, share=34, keitaro_association_id=30),
        o(5002, share=33, keitaro_association_id=10),
        o(5003, share=33, keitaro_association_id=20),
    ]
    result = redistribute_offer_shares(offers)
    assert shares(result) == {5001: 33, 5002: 34, 5003: 33}


def test_remove_redistributes_and_keeps_row_disabled() -> None:
    offers = [
        o(2001, share=34, keitaro_association_id=10),
        o(2002, share=33, keitaro_association_id=11),
        o(2003, share=33, keitaro_association_id=12),
    ]
    result = remove_offer(stream(), offers, 2001)
    assert states(result) == {2001: "disabled", 2002: "active", 2003: "active"}
    assert shares(result) == {2001: 0, 2002: 50, 2003: 50}


def test_reactivate_removed_offer_instead_of_duplicating() -> None:
    offers = [
        o(2001, share=50, keitaro_association_id=10),
        o(2002, state=OfferAssociationState.DISABLED, share=0, keitaro_association_id=11),
    ]
    result = add_offer(stream(), offers, 2002)
    assert len(result) == 2  # reactivated, not duplicated
    by_id = {offer.offer_id: offer for offer in result}
    assert by_id[2002].state is OfferAssociationState.ACTIVE
    assert by_id[2002].keitaro_association_id == 11  # original association kept
    assert shares(result) == {2001: 50, 2002: 50}


def test_remove_down_to_last_offer_leaves_100() -> None:
    offers = [
        o(2001, share=50, keitaro_association_id=10),
        o(2002, share=50, keitaro_association_id=11),
    ]
    result = remove_offer(stream(), offers, 2001)
    assert shares(result) == {2001: 0, 2002: 100}


def test_add_already_active_offer_rejected() -> None:
    offers = [o(2001, share=100, keitaro_association_id=10)]
    with pytest.raises(OfferRuleViolationError):
        add_offer(stream(), offers, 2001)


@pytest.mark.parametrize("offer_id", [0, -3])
def test_add_non_positive_offer_id_rejected(offer_id: int) -> None:
    with pytest.raises(OfferRuleViolationError):
        add_offer(stream(), [], offer_id)


def test_remove_unknown_offer_rejected() -> None:
    with pytest.raises(OfferNotFoundError):
        remove_offer(stream(), [o(2001, share=100)], 9999)


def test_remove_already_removed_offer_rejected() -> None:
    offers = [o(2001, state=OfferAssociationState.DISABLED, share=0, keitaro_association_id=10)]
    with pytest.raises(OfferRuleViolationError):
        remove_offer(stream(), offers, 2001)


def test_remove_last_active_offer_of_active_stream_rejected() -> None:
    offers = [
        o(2001, share=100, keitaro_association_id=10),
        o(2002, state=OfferAssociationState.DISABLED, share=0, keitaro_association_id=11),
    ]
    with pytest.raises(OfferRuleViolationError):
        remove_offer(stream(), offers, 2001)


def test_remove_last_active_offer_allowed_while_stream_disabled() -> None:
    offers = [
        o(2001, share=100, keitaro_association_id=10),
        o(2002, state=OfferAssociationState.DISABLED, share=0, keitaro_association_id=11),
    ]
    result = remove_offer(stream(state=StreamState.DISABLED), offers, 2001)
    assert states(result) == {2001: "disabled", 2002: "disabled"}
    assert shares(result) == {2001: 0, 2002: 0}


def test_offer_editing_rejected_on_non_offer_capable_stream() -> None:
    redirect_stream = stream(offer_capable=False)
    with pytest.raises(UnsupportedStreamEditError):
        add_offer(redirect_stream, [], 2001)
    with pytest.raises(UnsupportedStreamEditError):
        remove_offer(redirect_stream, [o(2001, share=100)], 2001)


def test_offer_editing_does_not_touch_stream_shares() -> None:
    """Offer functions return associations only; stream shares are structurally
    unaffected (spec: changing offers within one stream does not change
    stream-level shares)."""
    offers = [o(2001, share=100, keitaro_association_id=10)]
    before = shares(offers)
    add_offer(stream(), offers, 2002)
    assert shares(offers) == before


# Active-stream offer requirement (spec 6.8 rule 8) ---------------------------


def test_active_stream_without_active_offers_rejected() -> None:
    offers = [o(2001, state=OfferAssociationState.DISABLED, share=0)]
    with pytest.raises(OfferRuleViolationError):
        validate_active_stream_offers(stream(), offers)


def test_active_stream_with_active_offer_passes() -> None:
    offers = [
        o(2001, share=100, keitaro_association_id=10),
        o(2002, state=OfferAssociationState.DISABLED, share=0),
    ]
    validate_active_stream_offers(stream(), offers)


def test_disabled_stream_may_have_no_active_offers() -> None:
    validate_active_stream_offers(
        stream(state=StreamState.DISABLED),
        [o(2001, state=OfferAssociationState.DISABLED, share=0)],
    )


def test_non_offer_capable_stream_needs_no_offers() -> None:
    validate_active_stream_offers(stream(offer_capable=False), [])
