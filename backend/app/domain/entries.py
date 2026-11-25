"""Plain-data inputs for the pure domain functions.

These frozen datatypes decouple the domain calculations from both the ORM
rows and the Keitaro wire models: services map persistence/remote state into
these entries, call the pure functions, and map the results back.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.states import OfferAssociationState, StreamState


@dataclass(frozen=True, slots=True)
class StreamEntry:
    """One draft stream as seen by the share calculations.

    ``offer_capable`` is caller-computed (e.g. from the remote schema) so the
    domain stays free of Keitaro schema-name knowledge.
    """

    keitaro_id: int
    position: int
    state: StreamState
    share: int
    pinned: bool = False
    offer_capable: bool = False


@dataclass(frozen=True, slots=True)
class OfferEntry:
    """One offer association as seen by the offer share calculations."""

    offer_id: int
    state: OfferAssociationState
    share: int
    keitaro_association_id: int | None = None


def offer_sort_key(offer: OfferEntry) -> tuple[int, int]:
    """Deterministic rounding order: association ID, then offer ID.

    Locally added associations have no Keitaro ID yet and sort as 0 (before
    all real IDs, which are positive).
    """
    association_id = offer.keitaro_association_id or 0
    return (association_id, offer.offer_id)


@dataclass(frozen=True, slots=True)
class DraftAssociationState:
    """One draft association as seen by the update planner."""

    offer_id: int
    state: OfferAssociationState
    share: int
    keitaro_association_id: int | None = None


@dataclass(frozen=True, slots=True)
class DraftStreamState:
    """One draft stream as seen by the update planner.

    Unlike :class:`StreamEntry` this carries no position or pin: neither is a
    Keitaro-managed field, so they can never produce an update.
    """

    keitaro_id: int
    state: StreamState
    share: int
    associations: tuple[DraftAssociationState, ...] = ()
