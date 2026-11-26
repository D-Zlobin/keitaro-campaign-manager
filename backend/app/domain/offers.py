"""Offer association editing per specification section 6.8.

Active offer shares total exactly 100 within each offer-based stream. Pins
are local-only metadata: a pinned active offer keeps its share while the
remaining active offers split the rest equally, using the same deterministic
rounding as stream shares, ordered by Keitaro association ID then offer ID
(locally added associations, which have no ID yet, sort first). Removing an
association clears its pin; re-adding a previously removed offer reactivates
the association unpinned. Removing never affects the global offer or
stream-level shares.

Definitions per offer-based stream: ``A`` active associations; ``P`` sum of
shares of active pinned associations; ``U`` active unpinned associations in
rounding order; ``R = 100 - P``. An active offer-based stream must contain
at least one active offer; an empty active-offer list is permitted only
while the stream is disabled.

All functions are pure: they return new entry lists and raise
:class:`~app.domain.errors.OfferRuleViolationError` instead of producing an
invalid state, so a rejected operation can never mutate the caller's draft.
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


def _active(offers: Sequence[OfferEntry]) -> list[OfferEntry]:
    return [o for o in offers if o.state is OfferAssociationState.ACTIVE]


def _pinned_total(offers: Sequence[OfferEntry]) -> int:
    return sum(o.share for o in offers if o.state is OfferAssociationState.ACTIVE and o.pinned)


def redistribute_offer_shares(offers: Sequence[OfferEntry]) -> list[OfferEntry]:
    """Recompute shares after a mutation.

    - Disabled (removed) associations: share 0, pin cleared.
    - Active pinned associations: share unchanged.
    - Active unpinned associations: ``R`` divided equally with deterministic
      rounding in association-then-offer order.

    Raises :class:`OfferRuleViolationError` when pinned shares exceed 100 or
    when no unpinned active offer remains while pinned shares total less
    than 100.
    """
    pinned_total = _pinned_total(offers)
    if pinned_total > TOTAL_SHARE:
        raise OfferRuleViolationError(
            f"Pinned offer shares total {pinned_total}, exceeding {TOTAL_SHARE}"
        )
    active = _active(offers)
    unpinned = sorted((o for o in active if not o.pinned), key=offer_sort_key)
    available = TOTAL_SHARE - pinned_total
    # With no active offers at all (permitted only while the stream is
    # disabled) there is nothing to redistribute.
    if active and not unpinned and pinned_total != TOTAL_SHARE:
        raise OfferRuleViolationError(
            "No unpinned active offer remains while pinned shares total "
            f"{pinned_total} (< {TOTAL_SHARE})"
        )
    assigned = {
        offer.offer_id: share
        for offer, share in zip(
            unpinned, distribute_equal_shares(available, len(unpinned)), strict=True
        )
    }
    result: list[OfferEntry] = []
    for offer in offers:
        if offer.state is OfferAssociationState.DISABLED:
            result.append(replace(offer, share=0, pinned=False))
        elif offer.pinned:
            result.append(offer)
        else:
            result.append(replace(offer, share=assigned[offer.offer_id]))
    return result


def validate_offer_share_invariants(offers: Sequence[OfferEntry]) -> None:
    """Validate one stream's associations (used before upload).

    Checks the disabled-association hygiene and, when active offers exist,
    the exact-100 total and pin rules, without recalculating.
    """
    for offer in offers:
        if offer.state is OfferAssociationState.DISABLED and (offer.share != 0 or offer.pinned):
            raise OfferRuleViolationError(
                f"Removed offer {offer.offer_id} must have share 0 and no pin"
            )
    active = _active(offers)
    if not active:
        return
    total = sum(o.share for o in active)
    if total != TOTAL_SHARE:
        raise OfferRuleViolationError(f"Active offer shares total {total}, expected {TOTAL_SHARE}")
    pinned_total = _pinned_total(offers)
    if pinned_total > TOTAL_SHARE:
        raise OfferRuleViolationError(
            f"Pinned offer shares total {pinned_total}, exceeding {TOTAL_SHARE}"
        )
    if not any(not o.pinned for o in active) and pinned_total != TOTAL_SHARE:
        raise OfferRuleViolationError(
            f"No unpinned active offer while pinned shares total {pinned_total} (< {TOTAL_SHARE})"
        )


def add_offer(
    stream: StreamEntry,
    offers: Sequence[OfferEntry],
    offer_id: int,
    *,
    keitaro_association_id: int | None = None,
) -> list[OfferEntry]:
    """Add an offer, or reactivate its disabled association if present.

    A locally added association starts without a Keitaro ID and unpinned, as
    does a reactivated one. Returns only the stream's associations —
    stream-level shares are never affected.
    """
    _require_offer_capable(stream)
    if offer_id <= 0:
        raise OfferRuleViolationError(f"Offer ID must be a positive integer, got {offer_id}")
    existing = next((o for o in offers if o.offer_id == offer_id), None)
    if existing is not None:
        if existing.state is OfferAssociationState.ACTIVE:
            raise OfferRuleViolationError(f"Offer {offer_id} is already active in this stream")
        updated = [
            replace(offer, state=OfferAssociationState.ACTIVE, pinned=False)
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
    """Disable one association (clearing its pin) and redistribute the rest.

    The last active offer of an *active* stream cannot be removed (spec
    section 6.8 rule 8); while the stream is disabled it may. Removing is
    also rejected when it would leave no unpinned active offer while pinned
    shares total less than 100.
    """
    _require_offer_capable(stream)
    target = _require_offer(offers, offer_id)
    if target.state is OfferAssociationState.DISABLED:
        raise OfferRuleViolationError(f"Offer {offer_id} is already removed")
    active = _active(offers)
    if stream.state is StreamState.ACTIVE and len(active) == 1:
        raise OfferRuleViolationError(
            f"Cannot remove the last active offer of active stream {stream.keitaro_id}"
        )
    updated = [
        replace(offer, state=OfferAssociationState.DISABLED, share=0, pinned=False)
        if offer.offer_id == offer_id
        else offer
        for offer in offers
    ]
    return redistribute_offer_shares(updated)


def pin_offer(stream: StreamEntry, offers: Sequence[OfferEntry], offer_id: int) -> list[OfferEntry]:
    """Pin an active offer at its current share; redistribute the remainder."""
    _require_offer_capable(stream)
    target = _require_offer(offers, offer_id)
    if target.state is OfferAssociationState.DISABLED:
        raise OfferRuleViolationError(f"Removed offer {offer_id} cannot be pinned")
    if target.pinned:
        raise OfferRuleViolationError(f"Offer {offer_id} is already pinned")
    updated = [
        replace(offer, pinned=True) if offer.offer_id == offer_id else offer for offer in offers
    ]
    return redistribute_offer_shares(updated)


def unpin_offer(
    stream: StreamEntry, offers: Sequence[OfferEntry], offer_id: int
) -> list[OfferEntry]:
    """Unpin an offer; it becomes eligible for equal redistribution."""
    _require_offer_capable(stream)
    target = _require_offer(offers, offer_id)
    if not target.pinned:
        raise OfferRuleViolationError(f"Offer {offer_id} is not pinned")
    updated = [
        replace(offer, pinned=False) if offer.offer_id == offer_id else offer for offer in offers
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
