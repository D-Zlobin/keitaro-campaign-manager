"""Offer association editing per specification section 6.8.

Active offer shares total exactly 100 within each offer-based stream, using
the same deterministic rounding as stream shares, ordered by Keitaro
association ID then offer ID (locally added associations, which have no ID
yet, sort first). Adding a previously removed offer reactivates its existing
association instead of duplicating it. Removing marks the association
disabled and never affects the global offer or stream-level shares.

An active offer-based stream must contain at least one active offer; an empty
active-offer list is permitted only while the stream is disabled.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from app.domain.distribution import distribute_equal_shares
from app.domain.entries import OfferEntry, StreamEntry, offer_sort_key
from app.domain.errors import (
    OfferNotFoundError,
    OfferRuleViolationError,
    UnsupportedStreamEditError,
)
from app.domain.states import OfferAssociationState, StreamState

TOTAL_SHARE = 100


def _require_offer(offers: Sequence[OfferEntry], offer_id: int) -> OfferEntry:
    for offer in offers:
        if offer.offer_id == offer_id:
            return offer
    raise OfferNotFoundError(offer_id)


def _require_offer_capable(stream: StreamEntry) -> None:
    if not stream.offer_capable:
        raise UnsupportedStreamEditError(
            f"Stream {stream.keitaro_id} does not support offer editing"
        )


def redistribute_offer_shares(offers: Sequence[OfferEntry]) -> list[OfferEntry]:
    """Recompute shares: active offers split 100 equally (deterministic
    rounding in association-then-offer order); disabled offers share 0."""
    active = sorted(
        (o for o in offers if o.state is OfferAssociationState.ACTIVE),
        key=offer_sort_key,
    )
    assigned = {
        offer.offer_id: share
        for offer, share in zip(
            active, distribute_equal_shares(TOTAL_SHARE, len(active)), strict=True
        )
    }
    return [replace(offer, share=assigned.get(offer.offer_id, 0)) for offer in offers]


def add_offer(
    stream: StreamEntry,
    offers: Sequence[OfferEntry],
    offer_id: int,
    *,
    keitaro_association_id: int | None = None,
) -> list[OfferEntry]:
    """Add an offer, or reactivate its disabled association if present.

    A locally added association starts without a Keitaro ID. Returns only the
    stream's associations — stream-level shares are never affected.
    """
    _require_offer_capable(stream)
    if offer_id <= 0:
        raise OfferRuleViolationError(f"Offer ID must be a positive integer, got {offer_id}")
    existing = next((o for o in offers if o.offer_id == offer_id), None)
    if existing is not None:
        if existing.state is OfferAssociationState.ACTIVE:
            raise OfferRuleViolationError(f"Offer {offer_id} is already active in this stream")
        updated = [
            replace(offer, state=OfferAssociationState.ACTIVE)
            if offer.offer_id == offer_id
            else offer
            for offer in offers
        ]
    else:
        updated = [
            *offers,
            OfferEntry(
                offer_id=offer_id,
                state=OfferAssociationState.ACTIVE,
                share=0,
                keitaro_association_id=keitaro_association_id,
            ),
        ]
    return redistribute_offer_shares(updated)


def remove_offer(
    stream: StreamEntry, offers: Sequence[OfferEntry], offer_id: int
) -> list[OfferEntry]:
    """Disable one association and redistribute the remaining active shares.

    The last active offer of an *active* stream cannot be removed (spec
    section 6.8 rule 8); while the stream is disabled it may.
    """
    _require_offer_capable(stream)
    target = _require_offer(offers, offer_id)
    if target.state is OfferAssociationState.DISABLED:
        raise OfferRuleViolationError(f"Offer {offer_id} is already removed")
    active = [o for o in offers if o.state is OfferAssociationState.ACTIVE]
    if stream.state is StreamState.ACTIVE and len(active) == 1:
        raise OfferRuleViolationError(
            f"Cannot remove the last active offer of active stream {stream.keitaro_id}"
        )
    updated = [
        replace(offer, state=OfferAssociationState.DISABLED, share=0)
        if offer.offer_id == offer_id
        else offer
        for offer in offers
    ]
    return redistribute_offer_shares(updated)


def validate_active_stream_offers(stream: StreamEntry, offers: Sequence[OfferEntry]) -> None:
    """Guard for restore and upload: an active offer-based stream needs at
    least one active offer (spec section 6.8 rule 8)."""
    if not stream.offer_capable:
        return
    if stream.state is StreamState.ACTIVE and not any(
        o.state is OfferAssociationState.ACTIVE for o in offers
    ):
        raise OfferRuleViolationError(
            f"Active stream {stream.keitaro_id} has no active offers; "
            "add an offer or disable the stream"
        )
