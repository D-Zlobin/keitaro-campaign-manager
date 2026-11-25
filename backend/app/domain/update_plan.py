"""Baseline-versus-draft diffing: the minimum partial-update plan.

Dirty state (spec section 6.9) is derived purely from Keitaro-managed draft
fields compared with the stored canonical baseline: stream state and weight,
offer association state and share. Pins, positions, names, and UI metadata
are excluded and therefore can never produce an update or make a draft
dirty.

The plan (spec section 7.2) contains one entry per changed stream with only
the fields that differ from the baseline, so unchanged streams and
associations generate no remote writes. Disabled streams are planned with
``weight=0``; removed associations are planned as ``disabled``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.entries import DraftStreamState
from app.domain.errors import DomainError
from app.domain.states import OfferAssociationState, StreamState


@dataclass(frozen=True, slots=True)
class AssociationUpdate:
    """One association write within a stream update."""

    offer_id: int
    keitaro_association_id: int | None
    state: OfferAssociationState
    share: int


@dataclass(frozen=True, slots=True)
class StreamUpdate:
    """One partial stream update. ``None`` fields are omitted from the write."""

    keitaro_id: int
    state: StreamState | None = None
    weight: int | None = None
    associations: tuple[AssociationUpdate, ...] = ()

    @property
    def has_writes(self) -> bool:
        return self.state is not None or self.weight is not None or bool(self.associations)


def _target_weight(stream: DraftStreamState) -> int:
    return stream.share if stream.state is StreamState.ACTIVE else 0


def _baseline_streams(baseline: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    streams = baseline.get("streams")
    if not isinstance(streams, list):
        raise DomainError("Baseline canonical document has no 'streams' collection")
    return {int(entry["id"]): entry for entry in streams}


def _plan_associations(
    draft: DraftStreamState, baseline_entry: Mapping[str, Any]
) -> tuple[AssociationUpdate, ...]:
    baseline_associations: dict[int, Mapping[str, Any]] = {}
    baseline_by_offer: dict[int, Mapping[str, Any]] = {}
    for entry in baseline_entry.get("offers", []):
        baseline_associations[int(entry["id"])] = entry
        baseline_by_offer[int(entry["offer_id"])] = entry

    updates: list[AssociationUpdate] = []
    seen_ids: set[int] = set()
    for association in draft.associations:
        baseline_assoc: Mapping[str, Any] | None = None
        if association.keitaro_association_id is not None:
            baseline_assoc = baseline_associations.get(association.keitaro_association_id)
            if baseline_assoc is None:
                raise DomainError(
                    f"Draft association {association.keitaro_association_id} of stream "
                    f"{draft.keitaro_id} is missing from the baseline"
                )
            seen_ids.add(association.keitaro_association_id)
        else:
            baseline_assoc = baseline_by_offer.get(association.offer_id)
            if baseline_assoc is not None:
                # Known offer without a local association ID: bind to the
                # baseline association (e.g. rows created before an upload).
                seen_ids.add(int(baseline_assoc["id"]))
        if baseline_assoc is None:
            # New association: send it for creation.
            if association.state is OfferAssociationState.ACTIVE:
                updates.append(
                    AssociationUpdate(
                        offer_id=association.offer_id,
                        keitaro_association_id=None,
                        state=association.state,
                        share=association.share,
                    )
                )
            continue
        baseline_state = baseline_assoc["state"]
        baseline_share = baseline_assoc["share"]
        if association.state != baseline_state or int(association.share) != int(baseline_share):
            updates.append(
                AssociationUpdate(
                    offer_id=association.offer_id,
                    keitaro_association_id=int(baseline_assoc["id"]),
                    state=association.state,
                    share=association.share,
                )
            )

    # Baseline associations the draft no longer carries at all are removals.
    for association_id, baseline_assoc in baseline_associations.items():
        if association_id not in seen_ids:
            updates.append(
                AssociationUpdate(
                    offer_id=int(baseline_assoc["offer_id"]),
                    keitaro_association_id=association_id,
                    state=OfferAssociationState.DISABLED,
                    share=0,
                )
            )
    return tuple(updates)


def plan_updates(
    draft: Sequence[DraftStreamState], baseline: Mapping[str, Any]
) -> list[StreamUpdate]:
    """Diff the draft against the canonical baseline; one entry per changed
    stream, with only the managed fields that differ (``None`` = omit)."""
    baseline_streams = _baseline_streams(baseline)
    updates: list[StreamUpdate] = []
    for stream in draft:
        baseline_entry = baseline_streams.get(stream.keitaro_id)
        if baseline_entry is None:
            raise DomainError(f"Draft stream {stream.keitaro_id} is missing from the baseline")
        state_update: StreamState | None = None
        if stream.state != baseline_entry["state"]:
            state_update = stream.state
        weight_update: int | None = None
        if _target_weight(stream) != int(baseline_entry["weight"]):
            weight_update = _target_weight(stream)
        association_updates = _plan_associations(stream, baseline_entry)
        if state_update is not None or weight_update is not None or association_updates:
            updates.append(
                StreamUpdate(
                    keitaro_id=stream.keitaro_id,
                    state=state_update,
                    weight=weight_update,
                    associations=association_updates,
                )
            )
    return updates


def is_dirty(draft: Sequence[DraftStreamState], baseline: Mapping[str, Any]) -> bool:
    """Dirty state: any planned write means a managed field diverged."""
    return bool(plan_updates(draft, baseline))
