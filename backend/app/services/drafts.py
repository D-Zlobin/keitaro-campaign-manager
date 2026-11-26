"""Draft detail and transactional local editing (plan stage 5).

The editor loads :class:`CampaignDetailResponse` and mutates the draft with
single commands (delete/restore/pin/unpin stream, add/remove offer). All
share recalculation happens here through the pure domain functions — the
client never sends percentages, names, or new streams — and every mutation
is persisted atomically with an ``expected_revision`` guard and a derived
dirty state (spec section 6.9: dirty compares only Keitaro-managed fields
against the canonical baseline, so pin-only changes stay clean).

Structure mirrors the stage-4 fetch: draft state is read in one transaction,
the command is applied purely (plus one safe remote read when a *new* offer
must be validated and named), and the replacement draft is written in a
second transaction whose revision check rejects interleaved writers. Domain
rule violations therefore never partially change the draft.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.api.schemas import (
    AddOfferCommand,
    CampaignDetailResponse,
    DeleteStreamCommand,
    DraftCommand,
    DraftOffer,
    DraftStream,
    PinStreamCommand,
    RemoveOfferCommand,
    RestoreStreamCommand,
    UnpinStreamCommand,
)
from app.db.engine import session_scope
from app.db.models import Stream, StreamOffer, SyncState
from app.domain.entries import (
    DraftAssociationState,
    DraftStreamState,
    OfferEntry,
    StreamEntry,
    offer_sort_key,
)
from app.domain.errors import StreamNotFoundError
from app.domain.offers import add_offer, remove_offer, validate_active_stream_offers
from app.domain.shares import delete_stream, pin_stream, restore_stream, unpin_stream
from app.domain.update_plan import is_dirty
from app.errors import ConflictError, NotFoundError
from app.keitaro.adapter import KeitaroAdapter
from app.keitaro.client import KeitaroClientError
from app.keitaro.payloads import STREAM_SCHEMA_OFFER_CAPABLE
from app.repositories.campaigns import (
    CampaignRepository,
    DraftOfferInput,
    DraftStreamInput,
)
from app.services.campaigns import _campaign_summary, _upstream_error, validate_offer_available


@dataclass(frozen=True, slots=True)
class _StreamSnapshot:
    """One draft stream and its associations, detached from the session."""

    keitaro_id: int
    name: str
    type: str
    schema: str
    position: int
    entry: StreamEntry
    offers: tuple[OfferEntry, ...]
    offer_names: dict[int, str]
    offer_baselines: dict[int, dict[str, Any] | None]
    baseline_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _DraftSnapshot:
    revision: int
    baseline_canonical: dict[str, Any]
    notices: tuple[str, ...]
    streams: tuple[_StreamSnapshot, ...]


def _offer_entry(offer: StreamOffer) -> OfferEntry:
    return OfferEntry(
        offer_id=offer.offer_id,
        state=offer.state,
        share=offer.share,
        keitaro_association_id=offer.keitaro_association_id,
    )


def _stream_snapshot(stream: Stream) -> _StreamSnapshot:
    return _StreamSnapshot(
        keitaro_id=stream.keitaro_id,
        name=stream.name,
        type=stream.type,
        schema=stream.schema_,
        position=stream.position,
        entry=StreamEntry(
            keitaro_id=stream.keitaro_id,
            position=stream.position,
            state=stream.state,
            share=stream.share,
            pinned=stream.pinned,
            offer_capable=stream.schema_ == STREAM_SCHEMA_OFFER_CAPABLE,
        ),
        offers=tuple(_offer_entry(offer) for offer in stream.offers),
        offer_names={offer.offer_id: offer.offer_name for offer in stream.offers},
        offer_baselines={offer.offer_id: offer.baseline_payload for offer in stream.offers},
        baseline_payload=stream.baseline_payload,
    )


def _require_entry(entries: list[StreamEntry], keitaro_id: int) -> StreamEntry:
    for entry in entries:
        if entry.keitaro_id == keitaro_id:
            return entry
    raise StreamNotFoundError(keitaro_id)


def _stream_response(
    *,
    keitaro_id: int,
    name: str,
    type: str,
    schema: str,
    position: int,
    entry: StreamEntry,
    offers: list[DraftOffer],
) -> DraftStream:
    return DraftStream(
        keitaro_id=keitaro_id,
        name=name,
        type=type,
        schema=schema,
        position=position,
        state=entry.state,
        share=entry.share,
        pinned=entry.pinned,
        offer_capable=entry.offer_capable,
        offers=offers,
    )


class DraftService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        keitaro: KeitaroAdapter,
        campaigns: CampaignRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._keitaro = keitaro
        self._campaigns = campaigns or CampaignRepository()

    # Detail -----------------------------------------------------------------

    def get_campaign_detail(self, campaign_id: uuid.UUID) -> CampaignDetailResponse:
        """Load the editor state: draft streams/offers, pins, revision, dirty
        state, persisted notices, and the upload-blocking sync state."""
        with session_scope(self._session_factory) as session:
            campaign = self._campaigns.get_with_draft(session, campaign_id)
            if campaign is None:
                raise NotFoundError(
                    "Campaign not found.", details={"campaign_id": str(campaign_id)}
                )
            streams = [
                _stream_response(
                    keitaro_id=stream.keitaro_id,
                    name=stream.name,
                    type=stream.type,
                    schema=stream.schema_,
                    position=stream.position,
                    entry=StreamEntry(
                        keitaro_id=stream.keitaro_id,
                        position=stream.position,
                        state=stream.state,
                        share=stream.share,
                        pinned=stream.pinned,
                        offer_capable=stream.schema_ == STREAM_SCHEMA_OFFER_CAPABLE,
                    ),
                    offers=[
                        DraftOffer(
                            offer_id=offer.offer_id,
                            offer_name=offer.offer_name,
                            keitaro_association_id=offer.keitaro_association_id,
                            state=offer.state,
                            share=offer.share,
                        )
                        for offer in stream.offers
                    ],
                )
                for stream in campaign.streams
            ]
            return CampaignDetailResponse(
                campaign=_campaign_summary(campaign),
                streams=streams,
                notices=list(campaign.notices),
                upload_blocked=campaign.sync_state
                in (SyncState.SYNCING, SyncState.NEEDS_ATTENTION),
            )

    # Commands ---------------------------------------------------------------

    def apply_draft_command(
        self,
        campaign_id: uuid.UUID,
        *,
        expected_revision: int,
        command: DraftCommand,
    ) -> CampaignDetailResponse:
        """Apply one editing command and persist the recalculated draft.

        Rejected commands (unknown IDs, rule violations, stale revisions,
        blocked synchronization states) leave the persisted draft untouched.
        """
        with session_scope(self._session_factory) as session:
            campaign = self._campaigns.get_with_draft(session, campaign_id)
            if campaign is None:
                raise NotFoundError(
                    "Campaign not found.", details={"campaign_id": str(campaign_id)}
                )
            if campaign.sync_state is SyncState.SYNCING:
                raise ConflictError(
                    "A synchronization operation is already in progress for this campaign.",
                    code="operation_in_progress",
                )
            if campaign.sync_state is SyncState.NEEDS_ATTENTION:
                raise ConflictError(
                    "This campaign needs attention after a failed synchronization. "
                    "Reload it from Keitaro before editing.",
                    code="needs_attention",
                )
            snapshot = _DraftSnapshot(
                revision=campaign.revision,
                baseline_canonical=campaign.baseline_canonical,
                notices=tuple(campaign.notices),
                streams=tuple(_stream_snapshot(stream) for stream in campaign.streams),
            )

        entries = [stream.entry for stream in snapshot.streams]
        offers = {stream.keitaro_id: list(stream.offers) for stream in snapshot.streams}
        new_entries, new_offers = self._apply(entries, offers, command)
        new_offer_names = self._resolve_new_offer_names(command, snapshot)
        inputs = self._draft_inputs(snapshot, new_entries, new_offers, new_offer_names)
        dirty = is_dirty(
            [
                DraftStreamState(
                    keitaro_id=entry.keitaro_id,
                    state=entry.state,
                    share=entry.share,
                    associations=tuple(
                        DraftAssociationState(
                            offer_id=offer.offer_id,
                            state=offer.state,
                            share=offer.share,
                            keitaro_association_id=offer.keitaro_association_id,
                        )
                        for offer in new_offers[entry.keitaro_id]
                    ),
                )
                for entry in new_entries
            ],
            snapshot.baseline_canonical,
        )
        sync_state = SyncState.DIRTY if dirty else SyncState.CLEAN

        with session_scope(self._session_factory) as session:
            campaign = self._campaigns.replace_draft(
                session,
                campaign_id=campaign_id,
                expected_revision=expected_revision,
                streams=inputs,
                sync_state=sync_state,
            )
            summary = _campaign_summary(campaign)
        return CampaignDetailResponse(
            campaign=summary,
            streams=[
                _stream_response(
                    keitaro_id=stream_input.keitaro_id,
                    name=stream_input.name,
                    type=stream_input.type,
                    schema=stream_input.schema,
                    position=stream_input.position,
                    entry=_require_entry(new_entries, stream_input.keitaro_id),
                    offers=[
                        DraftOffer(
                            offer_id=offer.offer_id,
                            offer_name=offer.offer_name,
                            keitaro_association_id=offer.keitaro_association_id,
                            state=offer.state,
                            share=offer.share,
                        )
                        for offer in stream_input.offers
                    ],
                )
                for stream_input in inputs
            ],
            notices=list(snapshot.notices),
            upload_blocked=False,
        )

    # Internals ----------------------------------------------------------------

    def _apply(
        self,
        entries: list[StreamEntry],
        offers: dict[int, list[OfferEntry]],
        command: DraftCommand,
    ) -> tuple[list[StreamEntry], dict[int, list[OfferEntry]]]:
        if isinstance(command, DeleteStreamCommand):
            return delete_stream(entries, command.stream_id), offers
        if isinstance(command, RestoreStreamCommand):
            restored = restore_stream(entries, command.stream_id)
            target = _require_entry(restored, command.stream_id)
            validate_active_stream_offers(target, offers[command.stream_id])
            return restored, offers
        if isinstance(command, PinStreamCommand):
            return pin_stream(entries, command.stream_id), offers
        if isinstance(command, UnpinStreamCommand):
            return unpin_stream(entries, command.stream_id), offers
        if isinstance(command, AddOfferCommand):
            target = _require_entry(entries, command.stream_id)
            updated = add_offer(target, offers[command.stream_id], command.offer_id)
            return entries, {**offers, command.stream_id: updated}
        if isinstance(command, RemoveOfferCommand):
            target = _require_entry(entries, command.stream_id)
            updated = remove_offer(target, offers[command.stream_id], command.offer_id)
            return entries, {**offers, command.stream_id: updated}
        raise TypeError(f"Unsupported draft command: {type(command).__name__}")

    def _resolve_new_offer_names(
        self, command: DraftCommand, snapshot: _DraftSnapshot
    ) -> dict[int, str]:
        """Display-name snapshot for a newly added offer, validated against
        Keitaro (spec section 6.8: only existing, non-deleted offers).

        Reactivations reuse the persisted name and need no remote call; only
        a brand-new association triggers a safe read.
        """
        if not isinstance(command, AddOfferCommand):
            return {}
        stream = next(s for s in snapshot.streams if s.keitaro_id == command.stream_id)
        if command.offer_id in stream.offer_names:
            return {}
        try:
            offer = validate_offer_available(self._keitaro, command.offer_id)
        except KeitaroClientError as exc:
            raise _upstream_error(exc) from exc
        return {command.offer_id: offer.name}

    def _draft_inputs(
        self,
        snapshot: _DraftSnapshot,
        entries: list[StreamEntry],
        offers: dict[int, list[OfferEntry]],
        new_offer_names: dict[int, str],
    ) -> list[DraftStreamInput]:
        streams_by_id = {stream.keitaro_id: stream for stream in snapshot.streams}
        inputs: list[DraftStreamInput] = []
        for entry in entries:
            stream = streams_by_id[entry.keitaro_id]
            inputs.append(
                DraftStreamInput(
                    keitaro_id=entry.keitaro_id,
                    name=stream.name,
                    type=stream.type,
                    schema=stream.schema,
                    position=stream.position,
                    state=entry.state,
                    share=entry.share,
                    pinned=entry.pinned,
                    baseline_payload=stream.baseline_payload,
                    # Deterministic association-then-offer order (spec section
                    # 6.8), matching how the rows sort on the next load: new
                    # associations without a Keitaro ID come first.
                    offers=tuple(
                        DraftOfferInput(
                            offer_id=offer.offer_id,
                            offer_name=stream.offer_names.get(
                                offer.offer_id, new_offer_names.get(offer.offer_id, "")
                            ),
                            state=offer.state,
                            share=offer.share,
                            keitaro_association_id=offer.keitaro_association_id,
                            baseline_payload=stream.offer_baselines.get(offer.offer_id),
                        )
                        for offer in sorted(offers[entry.keitaro_id], key=offer_sort_key)
                    ),
                )
            )
        return inputs
