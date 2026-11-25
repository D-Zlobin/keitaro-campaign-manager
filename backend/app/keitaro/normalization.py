"""Fetch normalization: remote state into reliable internal models.

Turns the raw campaign fetch (streams from the Keitaro adapter) into the
normalized draft input used to replace baseline and draft after a fetch or
upload (spec section 6.5):

- non-deleted active and disabled streams are included; remotely deleted
  streams (or streams in an unexpected remote state) are omitted with a
  diagnostic notice instead of being restored;
- pins are cleared and disabled stream shares zeroed;
- invalid stream share totals (anything but integral weights summing to
  exactly 100) are redistributed equally, with an explanatory notice and a
  dirty draft;
- active offer shares within each offer-based stream are normalized the
  same way; disabled streams keep their remote offer shares (spec section
  6.5 normalizes *active* streams only);
- every stream carries its raw remote payload as the baseline snapshot for
  fingerprints, partial updates, and rollback compensation.

Keitaro schema knowledge stays in this adapter package: only streams with
the offer-capable schema expose offer editing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.entries import OfferEntry, StreamEntry
from app.domain.offers import redistribute_offer_shares
from app.domain.shares import recalculate_shares
from app.domain.states import OfferAssociationState, StreamState
from app.keitaro.models import KeitaroOfferAssociation, KeitaroStream
from app.keitaro.payloads import STREAM_SCHEMA_OFFER_CAPABLE

_ACTIVE = StreamState.ACTIVE.value
_DISABLED = StreamState.DISABLED.value


@dataclass(frozen=True, slots=True)
class NormalizedOffer:
    offer_id: int
    offer_name: str
    keitaro_association_id: int | None
    state: OfferAssociationState
    share: int
    # Raw remote association, kept as the baseline snapshot for fingerprints,
    # partial updates, and rollback compensation (spec section 10).
    baseline_payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class NormalizedStream:
    keitaro_id: int
    name: str
    type: str
    schema: str
    position: int
    state: StreamState
    share: int
    offer_capable: bool
    offers: tuple[NormalizedOffer, ...]
    baseline_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NormalizedFetch:
    streams: tuple[NormalizedStream, ...]
    dirty: bool
    notices: tuple[str, ...]


def _is_integral(value: int | float) -> bool:
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def _offer_name(offer_names: Mapping[int, str] | None, offer_id: int) -> str:
    if offer_names and offer_id in offer_names:
        return offer_names[offer_id]
    return f"Offer {offer_id}"


def _normalize_offers(
    stream: KeitaroStream,
    stream_state: StreamState,
    offer_capable: bool,
    offer_names: Mapping[int, str] | None,
    notices: list[str],
) -> tuple[tuple[NormalizedOffer, ...], bool]:
    def to_entry(association: KeitaroOfferAssociation) -> OfferEntry:
        return OfferEntry(
            offer_id=association.offer_id,
            state=OfferAssociationState(association.state),
            share=int(association.share) if _is_integral(association.share) else 0,
            keitaro_association_id=association.id,
        )

    dirty = False
    raw_by_id = {association.id: association for association in stream.offers}
    entries = [to_entry(association) for association in stream.offers]
    if offer_capable and stream_state is StreamState.ACTIVE and entries:
        active = [a for a in stream.offers if a.state == OfferAssociationState.ACTIVE.value]
        integral = all(_is_integral(association.share) for association in active)
        active_total = sum(float(association.share) for association in active)
        shares_match = integral and sum(int(association.share) for association in active) == 100
        if not shares_match:
            displayed = int(active_total) if integral else round(active_total, 4)
            notices.append(
                f"Stream {stream.id}: active offer shares total "
                f"{displayed} instead of 100; redistributed equally."
            )
            dirty = True
            entries = redistribute_offer_shares(entries)

    def baseline_payload(entry: OfferEntry) -> dict[str, Any] | None:
        raw = raw_by_id.get(entry.keitaro_association_id)
        return raw.model_dump(by_alias=True) if raw is not None else None

    return (
        tuple(
            NormalizedOffer(
                offer_id=entry.offer_id,
                offer_name=_offer_name(offer_names, entry.offer_id),
                keitaro_association_id=entry.keitaro_association_id,
                state=entry.state,
                share=entry.share,
                baseline_payload=baseline_payload(entry),
            )
            for entry in entries
        ),
        dirty,
    )


def normalize_fetch(
    streams: Sequence[KeitaroStream],
    *,
    known_stream_ids: Collection[int] = (),
    offer_names: Mapping[int, str] | None = None,
) -> NormalizedFetch:
    """Normalize a raw stream fetch into draft-ready state with notices."""
    notices: list[str] = []
    dirty = False

    present_ids = {stream.id for stream in streams}
    for missing in sorted(set(known_stream_ids) - present_ids):
        notices.append(f"Stream {missing} was deleted remotely and is not restored.")

    included: list[KeitaroStream] = []
    for stream in streams:
        if stream.state not in (_ACTIVE, _DISABLED):
            notices.append(
                f"Stream {stream.id} has unexpected remote state {stream.state!r}; omitted."
            )
            continue
        included.append(stream)

    # Campaign-level share normalization over the active streams.
    active_entries = [
        StreamEntry(
            keitaro_id=stream.id,
            position=stream.position,
            state=StreamState(stream.state),
            share=int(stream.weight) if _is_integral(stream.weight) else 0,
        )
        for stream in included
        if stream.state == _ACTIVE
    ]
    stream_shares: dict[int, int]
    if active_entries:
        weights = [stream.weight for stream in included if stream.state == _ACTIVE]
        integral = all(_is_integral(weight) for weight in weights)
        if integral and sum(int(weight) for weight in weights) == 100:
            stream_shares = {
                stream.id: int(stream.weight) for stream in included if stream.state == _ACTIVE
            }
        else:
            total = sum(int(weight) if _is_integral(weight) else 0 for weight in weights)
            notices.append(
                f"Active stream shares total {total} instead of 100; redistributed equally."
            )
            dirty = True
            recalculated = recalculate_shares(active_entries)
            stream_shares = {stream.keitaro_id: stream.share for stream in recalculated}
    else:
        stream_shares = {}

    normalized: list[NormalizedStream] = []
    for stream in included:
        stream_state = StreamState(stream.state)
        offer_capable = stream.schema_ == STREAM_SCHEMA_OFFER_CAPABLE
        offers, offers_dirty = _normalize_offers(
            stream, stream_state, offer_capable, offer_names, notices
        )
        dirty = dirty or offers_dirty
        normalized.append(
            NormalizedStream(
                keitaro_id=stream.id,
                name=stream.name,
                type=stream.type,
                schema=stream.schema_,
                position=stream.position,
                state=stream_state,
                share=stream_shares.get(stream.id, 0),
                offer_capable=offer_capable,
                offers=offers,
                baseline_payload=stream.model_dump(by_alias=True),
            )
        )

    return NormalizedFetch(streams=tuple(normalized), dirty=dirty, notices=tuple(notices))
