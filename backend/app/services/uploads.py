"""Conflict-checked upload and partial-failure recovery (plan stage 6).

Upload pushes the *persisted* draft to Keitaro (spec section 7.2); a client
never supplies a campaign snapshot. The sequence is:

1. Under the per-campaign lock (held by the API layer), re-read the draft,
   reject ``syncing``/``needs_attention`` states (409), validate the draft
   rules (422), and diff it against the stored baseline. An empty plan means
   there is nothing to upload (409 ``clean_draft``). The campaign then
   transitions to ``syncing``; a crash while ``syncing`` is reset to
   ``dirty`` at startup (single-process deployment), and the next upload's
   preflight detects any partial remote writes.
2. Preflight: fetch the remote campaign and streams and compare the remote
   fingerprint with the stored baseline fingerprint. A mismatch returns 409
   ``remote_conflict`` and performs no writes. Fingerprint checking is
   optimistic: Keitaro has no compare-and-set, so a remote write landing
   between the preflight and our updates is not detectable (spec section
   7.2); synchronization is never claimed to be atomic.
3. Apply only the planned partial updates, one stream PUT per changed
   stream, stopping at the first failure.
4. On failure, compensate every attempted write in reverse order (including
   the failed one — a timeout can leave the server-side outcome ambiguous),
   then verify with a fresh read that the managed remote values match the
   baseline. Compensation writes bump remote timestamps, so verification
   compares managed values (state, weight, association state/share) rather
   than the timestamp-bearing fingerprint. Confirmed rollback keeps the
   draft dirty and reports 502 ``upload_failed``; an unconfirmed rollback
   persists ``needs_attention`` (surviving restarts), blocks further uploads
   and draft edits, and requires a recovery fetch.
5. After successful writes, fetch the resulting state and transactionally
   replace baseline and draft. The result is reported clean only when the
   fetched managed state matches the intended values exactly and needs no
   normalization; otherwise the truthful fetched state (or, when Keitaro
   applied different but valid values, the intended draft) is kept dirty
   with an explanatory notice — never a false success. Pins are retained
   only for surviving active streams whose share is unchanged.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.api.schemas import CampaignUploadResponse
from app.db.engine import session_scope
from app.db.models import BaselineSource, SyncState
from app.domain.canonical import build_canonical_document, fingerprint_canonical, fingerprint_remote
from app.domain.entries import DraftAssociationState, DraftStreamState, OfferEntry, StreamEntry
from app.domain.errors import DomainError, OfferRuleViolationError, StreamRuleViolationError
from app.domain.offers import validate_active_stream_offers, validate_offer_share_invariants
from app.domain.shares import validate_share_invariants
from app.domain.states import OfferAssociationState
from app.domain.update_plan import StreamUpdate, is_dirty, plan_updates
from app.errors import ConflictError, NotFoundError, RuleViolationError, UpstreamError
from app.keitaro.adapter import KeitaroAdapter
from app.keitaro.client import KeitaroClientError
from app.keitaro.models import KeitaroCampaign, KeitaroOfferAssociation, KeitaroStream
from app.keitaro.normalization import NormalizedFetch, normalize_fetch
from app.keitaro.payloads import (
    STREAM_SCHEMA_OFFER_CAPABLE,
    OfferAssociationPayload,
    StreamUpdatePayload,
)
from app.repositories.campaigns import (
    CampaignRepository,
    DraftOfferInput,
    DraftStreamInput,
)
from app.services.campaigns import _campaign_summary, _read_error

logger = logging.getLogger(__name__)

CAMPAIGN_STATE_DELETED = "deleted"

_VALUES_DIFFER_NOTICE = (
    "Keitaro returned different values than the uploaded draft; "
    "review the campaign before uploading again."
)


def _as_int(value: int | float) -> int:
    return int(round(float(value)))


def _stream_update_payload(update: StreamUpdate) -> StreamUpdatePayload:
    """Partial PUT for one planned stream update: only changed fields."""
    fields: dict[str, Any] = {}
    if update.state is not None:
        fields["state"] = update.state.value
    if update.weight is not None:
        fields["weight"] = update.weight
    if update.associations:
        fields["offers"] = [
            OfferAssociationPayload(offer_id=a.offer_id, share=a.share, state=a.state.value)
            for a in update.associations
        ]
    return StreamUpdatePayload(**fields)


def _compensation_payload(
    update: StreamUpdate, baseline_stream: Mapping[str, Any]
) -> StreamUpdatePayload:
    """Inverse of one applied (or attempted) update, restoring baseline values.

    Associations the update created have no baseline counterpart; disabling
    them restores the baseline because removal is sent as ``disabled`` with
    share 0 (spec section 7.2).
    """
    fields: dict[str, Any] = {}
    if update.state is not None:
        fields["state"] = baseline_stream["state"]
    if update.weight is not None:
        fields["weight"] = _as_int(baseline_stream["weight"])
    if update.associations:
        baseline_by_offer = {
            int(entry["offer_id"]): entry for entry in baseline_stream.get("offers", [])
        }
        offers: list[OfferAssociationPayload] = []
        for association in update.associations:
            baseline_assoc = baseline_by_offer.get(association.offer_id)
            if baseline_assoc is None:
                offers.append(
                    OfferAssociationPayload(
                        offer_id=association.offer_id, share=0, state="disabled"
                    )
                )
            else:
                offers.append(
                    OfferAssociationPayload(
                        offer_id=association.offer_id,
                        share=_as_int(baseline_assoc["share"]),
                        state=baseline_assoc["state"],
                    )
                )
        fields["offers"] = offers
    return StreamUpdatePayload(**fields)


def _associations_match(
    fetched: Sequence[KeitaroOfferAssociation], baseline_offers: Sequence[Mapping[str, Any]]
) -> bool:
    """Compare association state/share by offer ID.

    An association absent on one side is equivalent to a disabled, zero-share
    association on the other: that is how removals (and compensated additions)
    are represented remotely.
    """
    baseline_by_offer = {int(entry["offer_id"]): entry for entry in baseline_offers}
    fetched_by_offer = {association.offer_id: association for association in fetched}
    for offer_id in set(baseline_by_offer) | set(fetched_by_offer):
        baseline_entry = baseline_by_offer.get(offer_id)
        fetched_entry = fetched_by_offer.get(offer_id)
        baseline_state = baseline_entry["state"] if baseline_entry else "disabled"
        baseline_share = float(baseline_entry["share"]) if baseline_entry else 0.0
        fetched_state = fetched_entry.state if fetched_entry else "disabled"
        fetched_share = float(fetched_entry.share) if fetched_entry else 0.0
        if baseline_state != fetched_state or baseline_share != fetched_share:
            return False
    return True


def _managed_state_matches_baseline(
    fetched: Sequence[KeitaroStream], baseline: Mapping[str, Any]
) -> bool:
    """Verify a rollback restored the baseline's managed values.

    Compares only the fields uploads can change (stream state/weight and
    association state/share); timestamps are excluded because compensation
    writes advance them, and filters/action data are never written by uploads.
    """
    baseline_streams = {int(entry["id"]): entry for entry in baseline.get("streams", [])}
    fetched_by_id = {stream.id: stream for stream in fetched}
    if set(fetched_by_id) != set(baseline_streams):
        return False
    for stream_id, baseline_stream in baseline_streams.items():
        stream = fetched_by_id[stream_id]
        if stream.state != baseline_stream["state"]:
            return False
        if float(stream.weight) != float(baseline_stream["weight"]):
            return False
        if not _associations_match(stream.offers, baseline_stream.get("offers", [])):
            return False
    return True


def _draft_states(streams: Sequence[DraftStreamInput]) -> list[DraftStreamState]:
    return [
        DraftStreamState(
            keitaro_id=stream.keitaro_id,
            state=stream.state,
            share=stream.share,
            associations=tuple(
                DraftAssociationState(
                    offer_id=offer.offer_id,
                    state=offer.state,
                    share=offer.share,
                    keitaro_association_id=offer.keitaro_association_id,
                )
                for offer in stream.offers
            ),
        )
        for stream in streams
    ]


def _stream_entries(streams: Sequence[DraftStreamInput]) -> list[StreamEntry]:
    return [
        StreamEntry(
            keitaro_id=stream.keitaro_id,
            position=stream.position,
            state=stream.state,
            share=stream.share,
            offer_capable=stream.schema == STREAM_SCHEMA_OFFER_CAPABLE,
        )
        for stream in streams
    ]


def _validate_draft(streams: Sequence[DraftStreamInput]) -> None:
    """Re-validate the persisted draft before any remote write (spec 7.2)."""
    entries = _stream_entries(streams)
    try:
        validate_share_invariants(entries)
        for entry, stream in zip(entries, streams, strict=True):
            offers = [
                OfferEntry(
                    offer_id=offer.offer_id,
                    state=offer.state,
                    share=offer.share,
                    keitaro_association_id=offer.keitaro_association_id,
                )
                for offer in stream.offers
            ]
            validate_active_stream_offers(entry, offers)
            if entry.offer_capable:
                validate_offer_share_invariants(offers)
    except (StreamRuleViolationError, OfferRuleViolationError) as exc:
        raise RuleViolationError(str(exc), code="invalid_draft") from exc


def _normalized_inputs(
    normalized: NormalizedFetch, intended_by_id: Mapping[int, DraftStreamInput]
) -> list[DraftStreamInput]:
    """Draft rows from the fetched (normalized) remote state.

    An offer pin survives only on an association that is still active with
    its share unchanged, so the pin keeps recording the share it was set at.
    """
    inputs: list[DraftStreamInput] = []
    for stream in normalized.streams:
        intended = intended_by_id.get(stream.keitaro_id)
        intended_offers = (
            {offer.offer_id: offer for offer in intended.offers} if intended is not None else {}
        )
        offers: list[DraftOfferInput] = []
        for offer in stream.offers:
            intended_offer = intended_offers.get(offer.offer_id)
            pinned = bool(
                intended_offer is not None
                and intended_offer.pinned
                and offer.state is OfferAssociationState.ACTIVE
                and offer.share == intended_offer.share
            )
            offers.append(
                DraftOfferInput(
                    offer_id=offer.offer_id,
                    offer_name=offer.offer_name,
                    state=offer.state,
                    share=offer.share,
                    keitaro_association_id=offer.keitaro_association_id,
                    pinned=pinned,
                    baseline_payload=offer.baseline_payload,
                )
            )
        inputs.append(
            DraftStreamInput(
                keitaro_id=stream.keitaro_id,
                name=stream.name,
                type=stream.type,
                schema=stream.schema,
                position=stream.position,
                state=stream.state,
                share=stream.share,
                baseline_payload=stream.baseline_payload,
                offers=tuple(offers),
            )
        )
    return inputs


def _rebound_intended_inputs(
    intended: Sequence[DraftStreamInput], normalized: NormalizedFetch
) -> list[DraftStreamInput]:
    """The intended draft with fresh Keitaro association IDs from the fetch.

    Used when Keitaro applied values that differ from the upload: keeping the
    intended values against the new baseline derives an honest dirty state so
    the operator can review and re-upload. Newly created associations adopt
    their real Keitaro IDs so a re-upload updates instead of duplicating.
    """
    normalized_by_id = {stream.keitaro_id: stream for stream in normalized.streams}
    inputs: list[DraftStreamInput] = []
    for stream in intended:
        normalized_stream = normalized_by_id.get(stream.keitaro_id)
        normalized_offers = (
            {offer.offer_id: offer for offer in normalized_stream.offers}
            if normalized_stream is not None
            else {}
        )
        offers: list[DraftOfferInput] = []
        for offer in stream.offers:
            fetched = normalized_offers.get(offer.offer_id)
            offers.append(
                DraftOfferInput(
                    offer_id=offer.offer_id,
                    offer_name=offer.offer_name,
                    state=offer.state,
                    share=offer.share,
                    keitaro_association_id=(
                        offer.keitaro_association_id
                        if offer.keitaro_association_id is not None
                        else (fetched.keitaro_association_id if fetched is not None else None)
                    ),
                    pinned=offer.pinned,
                    baseline_payload=(
                        fetched.baseline_payload if fetched is not None else offer.baseline_payload
                    ),
                )
            )
        inputs.append(
            DraftStreamInput(
                keitaro_id=stream.keitaro_id,
                name=stream.name,
                type=stream.type,
                schema=stream.schema,
                position=stream.position,
                state=stream.state,
                share=stream.share,
                baseline_payload=stream.baseline_payload,
                offers=tuple(offers),
            )
        )
    return inputs


class UploadService:
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

    def upload_campaign(self, campaign_id: uuid.UUID) -> CampaignUploadResponse:
        """Push the persisted dirty draft to Keitaro with conflict preflight.

        Callers serialize per campaign (the API holds the campaign lock).
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
                    "Reload it from Keitaro before uploading.",
                    code="needs_attention",
                )
            intended = [
                DraftStreamInput(
                    keitaro_id=stream.keitaro_id,
                    name=stream.name,
                    type=stream.type,
                    schema=stream.schema_,
                    position=stream.position,
                    state=stream.state,
                    share=stream.share,
                    baseline_payload=stream.baseline_payload,
                    offers=tuple(
                        DraftOfferInput(
                            offer_id=offer.offer_id,
                            offer_name=offer.offer_name,
                            state=offer.state,
                            share=offer.share,
                            keitaro_association_id=offer.keitaro_association_id,
                            pinned=offer.pinned,
                            baseline_payload=offer.baseline_payload,
                        )
                        for offer in stream.offers
                    ),
                )
                for stream in campaign.streams
            ]
            baseline = campaign.baseline_canonical
            baseline_fingerprint = campaign.baseline_fingerprint
            keitaro_id = campaign.keitaro_id
            expected_revision = campaign.revision

        _validate_draft(intended)
        try:
            plan = plan_updates(_draft_states(intended), baseline)
        except DomainError as exc:
            raise RuleViolationError(str(exc), code="invalid_draft") from exc
        if not plan:
            raise ConflictError(
                "The draft has no unsynchronized changes to upload.",
                code="clean_draft",
            )

        with session_scope(self._session_factory) as session:
            self._campaigns.transition_sync_state(
                session, campaign_id=campaign_id, to_state=SyncState.SYNCING
            )

        self._preflight(campaign_id, keitaro_id, baseline_fingerprint)
        self._apply_plan(campaign_id, keitaro_id, plan, baseline)

        try:
            final_campaign = self._keitaro.get_campaign(keitaro_id)
            final_streams = self._keitaro.get_campaign_streams(keitaro_id)
        except KeitaroClientError as exc:
            # The writes succeeded but the resulting state is unconfirmed;
            # block the campaign until an operator-controlled recovery fetch.
            self._transition(campaign_id, SyncState.NEEDS_ATTENTION)
            raise UpstreamError(
                "The upload finished but the resulting Keitaro state could not be "
                "fetched. The campaign needs attention; reload it from Keitaro to recover.",
                code="upload_needs_attention",
                details={"reason": "post_upload_fetch_failed"},
            ) from exc
        return self._finalize(
            campaign_id,
            intended=intended,
            expected_revision=expected_revision,
            final_campaign=final_campaign,
            final_streams=final_streams,
        )

    # Preflight ----------------------------------------------------------------

    def _preflight(
        self, campaign_id: uuid.UUID, keitaro_id: int, baseline_fingerprint: str
    ) -> None:
        """Fetch remote state and compare fingerprints before any write."""
        try:
            remote_campaign = self._keitaro.get_campaign(keitaro_id)
            remote_streams = self._keitaro.get_campaign_streams(keitaro_id)
        except KeitaroClientError as exc:
            self._transition(campaign_id, SyncState.DIRTY)
            raise _read_error(
                exc,
                not_found_message=(
                    "Campaign was not found in Keitaro; it may have been archived or deleted."
                ),
            ) from exc
        if remote_campaign.state == CAMPAIGN_STATE_DELETED:
            self._transition(campaign_id, SyncState.DIRTY)
            raise NotFoundError(
                "Campaign was not found in Keitaro; it may have been archived or deleted.",
                details={"keitaro_id": keitaro_id},
            )
        if fingerprint_remote(remote_campaign, remote_streams) != baseline_fingerprint:
            self._transition(campaign_id, SyncState.DIRTY)
            raise ConflictError(
                "The campaign changed in Keitaro. Reload it before uploading again.",
                code="remote_conflict",
                details={"keitaro_id": keitaro_id},
            )

    # Writes and compensation ----------------------------------------------------

    def _apply_plan(
        self,
        campaign_id: uuid.UUID,
        keitaro_id: int,
        plan: Sequence[StreamUpdate],
        baseline: Mapping[str, Any],
    ) -> None:
        completed: list[StreamUpdate] = []
        for update in plan:
            try:
                self._keitaro.update_stream(update.keitaro_id, _stream_update_payload(update))
            except KeitaroClientError as exc:
                self._compensate(campaign_id, keitaro_id, [*completed, update], baseline, exc)
            completed.append(update)

    def _compensate(
        self,
        campaign_id: uuid.UUID,
        keitaro_id: int,
        attempted: Sequence[StreamUpdate],
        baseline: Mapping[str, Any],
        cause: KeitaroClientError,
    ) -> None:
        """Best-effort rollback of every attempted write, then verification.

        Never returns: raises the upload error after the outcome is known.
        The failed write is compensated too — a timeout can mean the server
        applied it. Compensation runs in reverse application order and every
        compensation is attempted even if one fails; the verification read,
        not the individual call outcomes, decides whether rollback succeeded.
        """
        baseline_streams = {int(entry["id"]): entry for entry in baseline.get("streams", [])}
        for update in reversed(attempted):
            try:
                self._keitaro.update_stream(
                    update.keitaro_id,
                    _compensation_payload(update, baseline_streams[update.keitaro_id]),
                )
            except Exception:
                logger.exception(
                    "compensation write for stream %s of campaign %s failed",
                    update.keitaro_id,
                    campaign_id,
                )
        confirmed = False
        try:
            verification_streams = self._keitaro.get_campaign_streams(keitaro_id)
            confirmed = _managed_state_matches_baseline(verification_streams, baseline)
        except Exception:
            logger.exception("rollback verification fetch for campaign %s failed", campaign_id)
        failed_stream_id = attempted[-1].keitaro_id
        if confirmed:
            self._transition(campaign_id, SyncState.DIRTY)
            raise UpstreamError(
                f"Upload failed while updating stream {failed_stream_id}; "
                "the remote changes were rolled back.",
                code="upload_failed",
                details={"failed_stream_id": failed_stream_id, "rollback": "confirmed"},
            ) from cause
        self._transition(campaign_id, SyncState.NEEDS_ATTENTION)
        raise UpstreamError(
            f"Upload failed while updating stream {failed_stream_id} and the rollback "
            "could not be confirmed. The campaign needs attention; reload it from "
            "Keitaro to recover.",
            code="upload_needs_attention",
            details={"failed_stream_id": failed_stream_id, "rollback": "unconfirmed"},
        ) from cause

    # Finalization ---------------------------------------------------------------

    def _finalize(
        self,
        campaign_id: uuid.UUID,
        *,
        intended: Sequence[DraftStreamInput],
        expected_revision: int,
        final_campaign: KeitaroCampaign,
        final_streams: list[KeitaroStream],
    ) -> CampaignUploadResponse:
        """Replace baseline and draft with the post-upload remote state.

        Clean only when the fetched managed state matches the intended draft
        exactly and requires no normalization. Otherwise the draft stays
        dirty: with the intended values when Keitaro applied a different but
        valid state, or with the normalized fetched state when normalization
        was necessary — in both cases with an explanatory notice.
        """
        canonical = build_canonical_document(final_campaign, final_streams)
        fingerprint = fingerprint_canonical(canonical)
        intended_ids = {stream.keitaro_id for stream in intended}
        offer_names = {
            offer.offer_id: offer.offer_name for stream in intended for offer in stream.offers
        }
        normalized = normalize_fetch(
            final_streams, known_stream_ids=intended_ids, offer_names=offer_names
        )
        canonical_ids = {int(entry["id"]) for entry in canonical["streams"]}
        sets_equal = intended_ids == canonical_ids
        mismatches = plan_updates(_draft_states(intended), canonical) if sets_equal else None
        applied_exactly = sets_equal and not mismatches

        if applied_exactly and not normalized.dirty:
            inputs = _normalized_inputs(normalized, {s.keitaro_id: s for s in intended})
            notices = list(normalized.notices)
        else:
            notices = [*normalized.notices, _VALUES_DIFFER_NOTICE]
            if sets_equal and not normalized.dirty:
                inputs = _rebound_intended_inputs(intended, normalized)
            else:
                inputs = _normalized_inputs(normalized, {s.keitaro_id: s for s in intended})
        dirty = is_dirty(_draft_states(inputs), canonical)
        sync_state = SyncState.DIRTY if dirty else SyncState.CLEAN

        with session_scope(self._session_factory) as session:
            self._campaigns.require(session, campaign_id)
            self._campaigns.replace_baseline(
                session,
                campaign_id=campaign_id,
                canonical=canonical,
                fingerprint=fingerprint,
                source=BaselineSource.UPLOAD,
            )
            campaign = self._campaigns.replace_draft(
                session,
                campaign_id=campaign_id,
                expected_revision=expected_revision,
                streams=inputs,
                sync_state=sync_state,
            )
            self._campaigns.set_notices(session, campaign_id, notices)
            self._campaigns.mark_fetched(session, campaign_id)
            self._campaigns.mark_uploaded(session, campaign_id)
            return CampaignUploadResponse(campaign=_campaign_summary(campaign), notices=notices)

    # Internals ------------------------------------------------------------------

    def _transition(self, campaign_id: uuid.UUID, to_state: SyncState) -> None:
        with session_scope(self._session_factory) as session:
            self._campaigns.transition_sync_state(
                session, campaign_id=campaign_id, to_state=to_state
            )
