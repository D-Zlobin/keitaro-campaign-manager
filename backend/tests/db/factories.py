"""Builders for draft payloads and canonical baselines used in tests."""

from __future__ import annotations

from typing import Any

from app.db.models import BaselineSource, OfferAssociationState, StreamState, SyncState
from app.repositories.campaigns import DraftOfferInput, DraftStreamInput


def fingerprint(tag: str = "a") -> str:
    """A valid 64-char hex fingerprint stand-in."""
    return (tag * 64)[:64]


def canonical(tag: str = "v1", **extra: Any) -> dict[str, Any]:
    return {"tag": tag, **extra}


def draft_offer(
    offer_id: int = 2001,
    *,
    offer_name: str | None = None,
    state: OfferAssociationState = OfferAssociationState.ACTIVE,
    share: int = 100,
    keitaro_association_id: int | None = None,
    baseline_payload: dict[str, Any] | None = None,
) -> DraftOfferInput:
    return DraftOfferInput(
        offer_id=offer_id,
        offer_name=offer_name or f"Offer {offer_id}",
        state=state,
        share=share,
        keitaro_association_id=keitaro_association_id,
        baseline_payload=baseline_payload,
    )


def draft_stream(
    keitaro_id: int = 1001,
    *,
    name: str | None = None,
    type: str = "regular",
    schema: str = "landings",
    position: int = 0,
    state: StreamState = StreamState.ACTIVE,
    share: int = 50,
    pinned: bool = False,
    baseline_payload: dict[str, Any] | None = None,
    offers: tuple[DraftOfferInput, ...] = (),
) -> DraftStreamInput:
    return DraftStreamInput(
        keitaro_id=keitaro_id,
        name=name or f"Stream {keitaro_id}",
        type=type,
        schema=schema,
        position=position,
        state=state,
        share=share,
        pinned=pinned,
        baseline_payload=baseline_payload if baseline_payload is not None else {"id": keitaro_id},
        offers=offers,
    )


__all__ = [
    "BaselineSource",
    "OfferAssociationState",
    "StreamState",
    "SyncState",
    "canonical",
    "draft_offer",
    "draft_stream",
    "fingerprint",
]
