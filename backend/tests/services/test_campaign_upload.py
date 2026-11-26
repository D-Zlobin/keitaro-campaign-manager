"""Conflict-checked upload and partial-failure recovery (plan stage 6).

Covers: zero remote writes for clean/invalid/conflicting/blocked uploads,
exact partial-update payloads and ordering, rollback on write failure
(confirmed, failed, and unverifiable compensation), persisted
``needs_attention`` across a restart with operator-controlled recovery,
post-upload normalization with pin compatibility, and the no-false-success
rule when Keitaro applies different values than the uploaded draft.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.schemas import (
    AddOfferCommand,
    DeleteStreamCommand,
    DraftCommand,
    PinOfferCommand,
    RemoveOfferCommand,
)
from app.db.engine import build_engine, make_session_factory
from app.db.models import Campaign, Stream, StreamOffer, SyncState
from app.domain.states import OfferAssociationState, StreamState
from app.errors import (
    ConflictError,
    NotFoundError,
    RuleViolationError,
    UpstreamError,
    UpstreamTimeoutError,
)
from app.keitaro.client import KeitaroResponseError, KeitaroTimeoutError
from app.keitaro.models import KeitaroOffer
from app.repositories.campaigns import (
    CampaignRepository,
    DraftOfferInput,
    DraftStreamInput,
)
from app.services.campaigns import CampaignService, CreateCampaignCommand
from app.services.drafts import DraftService
from app.services.uploads import _VALUES_DIFFER_NOTICE, UploadService
from tests.fakes import DEFAULT_OFFER_ID, FakeKeitaroAdapter

COMMAND = CreateCampaignCommand(name="Example DE", country_code="DE", offer_id=DEFAULT_OFFER_ID)
SECOND_OFFER_ID = 18002
THIRD_OFFER_ID = 18003

# Keitaro IDs assigned by the fake during creation (see tests/fakes.py).
KEITARO_CAMPAIGN_ID = 500
GOOGLE_STREAM_ID = 900
OFFER_STREAM_ID = 901
OFFER_ASSOCIATION_ID = 7001


@pytest.fixture
def upload_service(
    session_factory: sessionmaker[Session], fake_keitaro: FakeKeitaroAdapter
) -> UploadService:
    return UploadService(session_factory=session_factory, keitaro=fake_keitaro)


@pytest.fixture
def draft_service(
    session_factory: sessionmaker[Session], fake_keitaro: FakeKeitaroAdapter
) -> DraftService:
    return DraftService(session_factory=session_factory, keitaro=fake_keitaro)


def _create(service: CampaignService) -> uuid.UUID:
    outcome = service.create_campaign(COMMAND, idempotency_key=f"create-{COMMAND.name}")
    return uuid.UUID(outcome.body["campaign"]["id"])


def _campaign(session_factory: sessionmaker[Session], campaign_id: uuid.UUID) -> Campaign:
    with session_factory() as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign


def _streams(session_factory: sessionmaker[Session], campaign_id: uuid.UUID) -> list[Stream]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(Stream)
                .where(Stream.campaign_id == campaign_id)
                .order_by(Stream.position, Stream.keitaro_id)
            )
        )


def _draft_offers(
    session_factory: sessionmaker[Session],
    campaign_id: uuid.UUID,
    stream_keitaro_id: int | None = None,
) -> dict[int, tuple[OfferAssociationState, int, int | None, bool]]:
    """Materialized draft offers by offer ID: (state, share, association ID, pin).

    Optionally scoped to one stream — the same offer ID may be associated in
    several streams of a campaign.
    """
    with session_factory() as session:
        query = (
            select(StreamOffer)
            .join(Stream)
            .where(Stream.campaign_id == campaign_id)
            .order_by(StreamOffer.keitaro_association_id, StreamOffer.offer_id)
        )
        if stream_keitaro_id is not None:
            query = query.where(Stream.keitaro_id == stream_keitaro_id)
        offers = session.scalars(query)
        return {
            offer.offer_id: (
                offer.state,
                offer.share,
                offer.keitaro_association_id,
                offer.pinned,
            )
            for offer in offers
        }


def _apply(
    draft_service: DraftService,
    session_factory: sessionmaker[Session],
    campaign_id: uuid.UUID,
    command: DraftCommand,
) -> int:
    revision = _campaign(session_factory, campaign_id).revision
    response = draft_service.apply_draft_command(
        campaign_id, expected_revision=revision, command=command
    )
    return response.campaign.revision


def _set_sync_state(
    session_factory: sessionmaker[Session], campaign_id: uuid.UUID, state: SyncState
) -> None:
    with session_factory() as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.sync_state = state
        session.commit()


def _import_three_stream_campaign(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> uuid.UUID:
    """Import a campaign with streams at 40/30/30, each offer-capable."""
    fake_keitaro.seed_campaign(keitaro_id=600, name="Three streams")
    for position, weight in ((0, 40), (1, 30), (2, 30)):
        fake_keitaro.seed_stream(
            campaign_id=600,
            name=f"Stream {position}",
            schema="landings",
            position=position,
            weight=weight,
            offers=[{"offer_id": DEFAULT_OFFER_ID, "share": 100, "state": "active"}],
        )
    response = service.import_campaign(600)
    assert response.campaign.sync_state == "clean"
    return response.campaign.id


# Successful upload -------------------------------------------------------------


def test_successful_upload_updates_remote_and_marks_clean(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)
    revision = _apply(
        draft_service,
        session_factory,
        campaign_id,
        DeleteStreamCommand(type="delete_stream", stream_id=GOOGLE_STREAM_ID),
    )
    assert revision == 2
    fingerprint_before = _campaign(session_factory, campaign_id).baseline_fingerprint

    result = upload_service.upload_campaign(campaign_id)

    assert result.campaign.dirty is False
    assert result.campaign.sync_state == "clean"
    assert result.campaign.revision == 3
    assert result.campaign.last_upload_at is not None
    assert result.campaign.last_fetch_at is not None
    assert result.notices == []
    # Only the changed managed fields were written, in plan order.
    assert fake_keitaro.update_payloads == [
        (GOOGLE_STREAM_ID, {"state": "disabled", "weight": 0}),
        (OFFER_STREAM_ID, {"weight": 100}),
    ]
    assert fake_keitaro.streams[GOOGLE_STREAM_ID].state == "disabled"
    assert fake_keitaro.streams[GOOGLE_STREAM_ID].weight == 0
    assert fake_keitaro.streams[OFFER_STREAM_ID].weight == 100
    # Baseline was replaced; the persisted draft matches it.
    campaign = _campaign(session_factory, campaign_id)
    assert campaign.baseline_fingerprint != fingerprint_before
    assert [s["weight"] for s in campaign.baseline_canonical["streams"]] == [0, 100]
    assert [(s.state, s.share) for s in _streams(session_factory, campaign_id)] == [
        (StreamState.DISABLED, 0),
        (StreamState.ACTIVE, 100),
    ]


def test_upload_sends_exact_partial_payloads_for_offer_changes(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    fake_keitaro.offers[SECOND_OFFER_ID] = KeitaroOffer(
        id=SECOND_OFFER_ID, name="Second Offer", state="active"
    )
    campaign_id = _create(service)
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        AddOfferCommand(type="add_offer", stream_id=OFFER_STREAM_ID, offer_id=SECOND_OFFER_ID),
    )
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        RemoveOfferCommand(
            type="remove_offer", stream_id=OFFER_STREAM_ID, offer_id=DEFAULT_OFFER_ID
        ),
    )
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        DeleteStreamCommand(type="delete_stream", stream_id=GOOGLE_STREAM_ID),
    )

    result = upload_service.upload_campaign(campaign_id)

    assert result.campaign.dirty is False
    assert fake_keitaro.update_payloads == [
        (GOOGLE_STREAM_ID, {"state": "disabled", "weight": 0}),
        (
            OFFER_STREAM_ID,
            {
                "weight": 100,
                "offers": [
                    {"offer_id": SECOND_OFFER_ID, "share": 100, "state": "active"},
                    {"offer_id": DEFAULT_OFFER_ID, "share": 0, "state": "disabled"},
                ],
            },
        ),
    ]
    # Remote associations: the removed offer is disabled, the added one created.
    remote_offers = {
        association.offer_id: association
        for association in fake_keitaro.streams[OFFER_STREAM_ID].offers
    }
    assert remote_offers[DEFAULT_OFFER_ID].state == "disabled"
    assert remote_offers[DEFAULT_OFFER_ID].share == 0
    assert remote_offers[SECOND_OFFER_ID].state == "active"
    assert remote_offers[SECOND_OFFER_ID].share == 100
    # The new association adopted its real Keitaro ID in the persisted draft.
    draft_offers = _draft_offers(session_factory, campaign_id)
    assert draft_offers[SECOND_OFFER_ID][2] == remote_offers[SECOND_OFFER_ID].id
    assert draft_offers[DEFAULT_OFFER_ID][0] is OfferAssociationState.DISABLED


# Rejections before any remote write --------------------------------------------


def test_clean_upload_is_rejected_without_remote_writes(
    service: CampaignService,
    upload_service: UploadService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)

    with pytest.raises(ConflictError) as exc_info:
        upload_service.upload_campaign(campaign_id)

    assert exc_info.value.code == "clean_draft"
    assert not any(call == "update_stream" for call in fake_keitaro.calls)
    assert _campaign(session_factory, campaign_id).revision == 1
    assert _campaign(session_factory, campaign_id).sync_state is SyncState.CLEAN


def test_remote_conflict_performs_no_writes_and_preserves_draft(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        DeleteStreamCommand(type="delete_stream", stream_id=GOOGLE_STREAM_ID),
    )
    fingerprint_before = _campaign(session_factory, campaign_id).baseline_fingerprint
    # The campaign changes in Keitaro after the baseline was stored.
    fake_keitaro.touch_stream(OFFER_STREAM_ID, weight=80)

    with pytest.raises(ConflictError) as exc_info:
        upload_service.upload_campaign(campaign_id)

    assert exc_info.value.code == "remote_conflict"
    assert not any(call == "update_stream" for call in fake_keitaro.calls)
    campaign = _campaign(session_factory, campaign_id)
    assert campaign.sync_state is SyncState.DIRTY
    assert campaign.revision == 2
    assert campaign.baseline_fingerprint == fingerprint_before
    # The local draft is preserved untouched.
    assert [(s.state, s.share) for s in _streams(session_factory, campaign_id)] == [
        (StreamState.DISABLED, 0),
        (StreamState.ACTIVE, 100),
    ]


def test_upload_rejected_when_needs_attention(
    service: CampaignService,
    upload_service: UploadService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)
    _set_sync_state(session_factory, campaign_id, SyncState.NEEDS_ATTENTION)

    with pytest.raises(ConflictError) as exc_info:
        upload_service.upload_campaign(campaign_id)

    assert exc_info.value.code == "needs_attention"
    assert not any(call == "update_stream" for call in fake_keitaro.calls)


def test_upload_rejected_when_syncing(
    service: CampaignService,
    upload_service: UploadService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)
    _set_sync_state(session_factory, campaign_id, SyncState.SYNCING)

    with pytest.raises(ConflictError) as exc_info:
        upload_service.upload_campaign(campaign_id)

    assert exc_info.value.code == "operation_in_progress"
    assert not any(call == "update_stream" for call in fake_keitaro.calls)


@pytest.mark.parametrize("case", ["share_total", "no_active_offer", "unknown_stream"])
def test_invalid_draft_is_rejected_without_remote_writes(
    case: str,
    service: CampaignService,
    upload_service: UploadService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)
    repo = CampaignRepository()
    google_share = 60 if case == "share_total" else 50
    offer_stream_share = 60 if case == "share_total" else 50
    offer_state = (
        OfferAssociationState.DISABLED
        if case == "no_active_offer"
        else OfferAssociationState.ACTIVE
    )
    offer_share = 0 if case == "no_active_offer" else 100
    streams = [
        DraftStreamInput(
            keitaro_id=GOOGLE_STREAM_ID,
            name="Google — DE",
            type="regular",
            schema="redirect",
            position=0,
            state=StreamState.ACTIVE,
            share=google_share,
        ),
        DraftStreamInput(
            keitaro_id=OFFER_STREAM_ID,
            name="Offer — 18001",
            type="regular",
            schema="landings",
            position=1,
            state=StreamState.ACTIVE,
            share=offer_stream_share,
            offers=(
                DraftOfferInput(
                    offer_id=DEFAULT_OFFER_ID,
                    offer_name="Example Offer",
                    state=offer_state,
                    share=offer_share,
                    keitaro_association_id=OFFER_ASSOCIATION_ID,
                ),
            ),
        ),
    ]
    if case == "unknown_stream":
        streams.append(
            DraftStreamInput(
                keitaro_id=999,
                name="Ghost",
                type="regular",
                schema="redirect",
                position=2,
                state=StreamState.DISABLED,
                share=0,
            )
        )
    with session_factory() as session:
        repo.replace_draft(
            session,
            campaign_id=campaign_id,
            expected_revision=1,
            streams=streams,
            sync_state=SyncState.DIRTY,
        )
        session.commit()

    with pytest.raises(RuleViolationError) as exc_info:
        upload_service.upload_campaign(campaign_id)

    assert exc_info.value.code == "invalid_draft"
    assert not any(call == "update_stream" for call in fake_keitaro.calls)
    # The rejected upload left the draft and its state untouched.
    assert _campaign(session_factory, campaign_id).sync_state is SyncState.DIRTY


def test_upload_fails_when_campaign_archived_remotely(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        DeleteStreamCommand(type="delete_stream", stream_id=GOOGLE_STREAM_ID),
    )
    fake_keitaro.archive_campaign(KEITARO_CAMPAIGN_ID)

    with pytest.raises(NotFoundError):
        upload_service.upload_campaign(campaign_id)

    assert not any(call == "update_stream" for call in fake_keitaro.calls)
    assert _campaign(session_factory, campaign_id).sync_state is SyncState.DIRTY


def test_preflight_timeout_maps_to_504_without_writes(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        DeleteStreamCommand(type="delete_stream", stream_id=GOOGLE_STREAM_ID),
    )
    fake_keitaro.queue_failure("get_campaign", KeitaroTimeoutError("slow"))

    with pytest.raises(UpstreamTimeoutError):
        upload_service.upload_campaign(campaign_id)

    assert not any(call == "update_stream" for call in fake_keitaro.calls)
    assert _campaign(session_factory, campaign_id).sync_state is SyncState.DIRTY


# Partial failure and recovery ----------------------------------------------------


def test_failed_write_rolls_back_and_keeps_draft_dirty(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        DeleteStreamCommand(type="delete_stream", stream_id=GOOGLE_STREAM_ID),
    )
    fingerprint_before = _campaign(session_factory, campaign_id).baseline_fingerprint
    fake_keitaro.queue_failure(
        "update_stream",
        None,  # the Google-stream disable succeeds
        KeitaroResponseError("update_stream", 500, "boom"),  # the weight update fails
    )

    with pytest.raises(UpstreamError) as exc_info:
        upload_service.upload_campaign(campaign_id)

    assert exc_info.value.code == "upload_failed"
    assert exc_info.value.details["rollback"] == "confirmed"
    assert exc_info.value.details["failed_stream_id"] == OFFER_STREAM_ID
    # The attempted writes were compensated in reverse order with the exact
    # inverse partial payloads (the failed write is compensated too, since a
    # failure can leave the server-side outcome ambiguous).
    assert fake_keitaro.update_payloads == [
        (GOOGLE_STREAM_ID, {"state": "disabled", "weight": 0}),
        (OFFER_STREAM_ID, {"weight": 100}),
        (OFFER_STREAM_ID, {"weight": 50}),
        (GOOGLE_STREAM_ID, {"state": "active", "weight": 50}),
    ]
    # Remote state matches the baseline again.
    assert fake_keitaro.streams[GOOGLE_STREAM_ID].state == "active"
    assert fake_keitaro.streams[GOOGLE_STREAM_ID].weight == 50
    assert fake_keitaro.streams[OFFER_STREAM_ID].weight == 50
    # The draft stays dirty with the intended values; the baseline is unchanged.
    campaign = _campaign(session_factory, campaign_id)
    assert campaign.sync_state is SyncState.DIRTY
    assert campaign.revision == 2
    assert campaign.baseline_fingerprint == fingerprint_before
    assert [(s.state, s.share) for s in _streams(session_factory, campaign_id)] == [
        (StreamState.DISABLED, 0),
        (StreamState.ACTIVE, 100),
    ]


def test_failed_compensation_marks_needs_attention_until_recovery(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
    migrated_database_url: str,
) -> None:
    campaign_id = _create(service)
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        DeleteStreamCommand(type="delete_stream", stream_id=GOOGLE_STREAM_ID),
    )
    fake_keitaro.queue_failure(
        "update_stream",
        None,  # the Google-stream disable succeeds and stays applied
        KeitaroResponseError("update_stream", 500, "boom"),  # the weight update fails
        KeitaroResponseError("update_stream", 500, "boom"),  # both compensations fail
        KeitaroResponseError("update_stream", 500, "boom"),
    )

    with pytest.raises(UpstreamError) as exc_info:
        upload_service.upload_campaign(campaign_id)

    assert exc_info.value.code == "upload_needs_attention"
    assert exc_info.value.details["rollback"] == "unconfirmed"
    assert _campaign(session_factory, campaign_id).sync_state is SyncState.NEEDS_ATTENTION
    update_calls_before = fake_keitaro.calls.count("update_stream")

    # needs_attention survives a backend restart (new engine on the same file).
    restarted_engine = build_engine(migrated_database_url)
    try:
        restarted = UploadService(
            session_factory=make_session_factory(restarted_engine), keitaro=fake_keitaro
        )
        with pytest.raises(ConflictError) as retry_info:
            restarted.upload_campaign(campaign_id)
        assert retry_info.value.code == "needs_attention"
    finally:
        restarted_engine.dispose()
    assert fake_keitaro.calls.count("update_stream") == update_calls_before

    # Draft work and plain fetch are blocked; only a confirmed recovery fetch
    # replaces the draft, and it reports the normalization it had to apply
    # (remote active shares total 50 after the partial upload).
    with pytest.raises(ConflictError):
        draft_service.apply_draft_command(
            campaign_id,
            expected_revision=2,
            command=PinOfferCommand(
                type="pin_offer", stream_id=OFFER_STREAM_ID, offer_id=DEFAULT_OFFER_ID
            ),
        )
    with pytest.raises(ConflictError):
        service.fetch_campaign(campaign_id, confirm_discard=False)
    recovery = service.fetch_campaign(campaign_id, confirm_discard=True)
    assert recovery.campaign.sync_state == "dirty"
    assert any("redistributed" in notice for notice in recovery.notices)
    assert [(s.state, s.share) for s in _streams(session_factory, campaign_id)] == [
        (StreamState.DISABLED, 0),
        (StreamState.ACTIVE, 100),
    ]

    # Uploading the recovered draft completes the arc back to clean.
    result = upload_service.upload_campaign(campaign_id)
    assert result.campaign.dirty is False
    assert fake_keitaro.streams[OFFER_STREAM_ID].weight == 100


def test_unverifiable_rollback_marks_needs_attention(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        DeleteStreamCommand(type="delete_stream", stream_id=GOOGLE_STREAM_ID),
    )
    fake_keitaro.queue_failure(
        "update_stream",
        None,
        KeitaroResponseError("update_stream", 500, "boom"),
    )
    # Preflight read succeeds; the rollback verification read times out.
    fake_keitaro.queue_failure("get_campaign_streams", None, KeitaroTimeoutError("slow"))

    with pytest.raises(UpstreamError) as exc_info:
        upload_service.upload_campaign(campaign_id)

    assert exc_info.value.code == "upload_needs_attention"
    assert exc_info.value.details["rollback"] == "unconfirmed"
    assert _campaign(session_factory, campaign_id).sync_state is SyncState.NEEDS_ATTENTION
    # The compensation writes themselves succeeded; only confirmation failed.
    assert fake_keitaro.streams[GOOGLE_STREAM_ID].state == "active"
    assert fake_keitaro.streams[GOOGLE_STREAM_ID].weight == 50


def test_post_upload_fetch_failure_marks_needs_attention(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _create(service)
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        DeleteStreamCommand(type="delete_stream", stream_id=GOOGLE_STREAM_ID),
    )
    fake_keitaro.queue_failure("get_campaign_streams", None, KeitaroTimeoutError("slow"))

    with pytest.raises(UpstreamError) as exc_info:
        upload_service.upload_campaign(campaign_id)

    assert exc_info.value.code == "upload_needs_attention"
    assert exc_info.value.details["reason"] == "post_upload_fetch_failed"
    assert _campaign(session_factory, campaign_id).sync_state is SyncState.NEEDS_ATTENTION
    # The writes did land; a confirmed recovery fetch restores a clean state.
    recovery = service.fetch_campaign(campaign_id, confirm_discard=True)
    assert recovery.campaign.sync_state == "clean"
    assert recovery.campaign.dirty is False


# Post-upload normalization and honesty -------------------------------------------


def _seed_pinned_three_offer_stream(
    service: CampaignService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> uuid.UUID:
    """Three-stream import; stream 900 ends with offers 33(pinned)/34/33.

    The pinned share (33) differs from what an equal three-way split assigns
    to the first association (34), so a normalization that redistributes the
    offers changes the pinned share and must drop the pin.
    """
    fake_keitaro.offers[SECOND_OFFER_ID] = KeitaroOffer(
        id=SECOND_OFFER_ID, name="Second Offer", state="active"
    )
    fake_keitaro.offers[THIRD_OFFER_ID] = KeitaroOffer(
        id=THIRD_OFFER_ID, name="Third Offer", state="active"
    )
    campaign_id = _import_three_stream_campaign(service, fake_keitaro)
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        AddOfferCommand(type="add_offer", stream_id=900, offer_id=SECOND_OFFER_ID),
    )
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        AddOfferCommand(type="add_offer", stream_id=900, offer_id=THIRD_OFFER_ID),
    )
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        PinOfferCommand(type="pin_offer", stream_id=900, offer_id=DEFAULT_OFFER_ID),
    )
    _apply(
        draft_service,
        session_factory,
        campaign_id,
        DeleteStreamCommand(type="delete_stream", stream_id=902),
    )
    return campaign_id


def _skew_stream_900_offer_shares(fake: FakeKeitaroAdapter, shares: dict[int, int]) -> None:
    stream = fake.streams[900]
    fake.touch_stream(
        900,
        offers=[
            association.model_copy(update={"share": shares[association.offer_id]})
            for association in stream.offers
        ],
    )


def test_upload_normalization_marks_dirty_and_drops_incompatible_pins(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _seed_pinned_three_offer_stream(
        service, draft_service, fake_keitaro, session_factory
    )
    # Keitaro applies skewed offer shares inside the non-atomic window,
    # leaving active offer shares at 30 instead of 100.
    fake_keitaro.post_write_hook = lambda fake: _skew_stream_900_offer_shares(
        fake, {DEFAULT_OFFER_ID: 10, SECOND_OFFER_ID: 10, THIRD_OFFER_ID: 10}
    )

    result = upload_service.upload_campaign(campaign_id)

    assert result.campaign.dirty is True
    assert result.campaign.sync_state == "dirty"
    assert any("redistributed" in notice for notice in result.notices)
    # The draft shows the truthful normalized state; the pin on the default
    # offer was dropped because its share changed (33 -> 34).
    draft_offers = _draft_offers(session_factory, campaign_id, stream_keitaro_id=900)
    assert [
        (draft_offers[offer_id][1], draft_offers[offer_id][3])
        for offer_id in (DEFAULT_OFFER_ID, SECOND_OFFER_ID, THIRD_OFFER_ID)
    ] == [(34, False), (33, False), (33, False)]
    campaign = _campaign(session_factory, campaign_id)
    assert [s["weight"] for s in campaign.baseline_canonical["streams"]] == [50, 50, 0]


def test_upload_values_differ_keeps_intended_draft_dirty(
    service: CampaignService,
    upload_service: UploadService,
    draft_service: DraftService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory: sessionmaker[Session],
) -> None:
    campaign_id = _seed_pinned_three_offer_stream(
        service, draft_service, fake_keitaro, session_factory
    )

    def diverge(fake: FakeKeitaroAdapter) -> None:
        # Keitaro stores a valid 40/30/30 offer split instead of 33/34/33.
        _skew_stream_900_offer_shares(
            fake, {DEFAULT_OFFER_ID: 40, SECOND_OFFER_ID: 30, THIRD_OFFER_ID: 30}
        )

    fake_keitaro.post_write_hook = diverge

    result = upload_service.upload_campaign(campaign_id)

    # No false success: the fetched state is valid but not what was uploaded,
    # so the editor keeps the intended draft dirty against the new baseline.
    assert result.campaign.dirty is True
    assert result.campaign.sync_state == "dirty"
    assert result.notices == [_VALUES_DIFFER_NOTICE]
    # The intended draft (and its offer pin) survives for review/re-upload.
    draft_offers = _draft_offers(session_factory, campaign_id, stream_keitaro_id=900)
    assert [
        (draft_offers[offer_id][1], draft_offers[offer_id][3])
        for offer_id in (DEFAULT_OFFER_ID, SECOND_OFFER_ID, THIRD_OFFER_ID)
    ] == [(33, True), (34, False), (33, False)]
    campaign = _campaign(session_factory, campaign_id)
    assert [s["weight"] for s in campaign.baseline_canonical["streams"]] == [50, 50, 0]


# Startup recovery -----------------------------------------------------------------


def test_reset_stale_syncing_restores_dirty_but_keeps_needs_attention(
    service: CampaignService, session_factory: sessionmaker[Session]
) -> None:
    syncing_id = _create(service)
    other = CreateCampaignCommand(name="Other", country_code="CL", offer_id=DEFAULT_OFFER_ID)
    outcome = service.create_campaign(other, idempotency_key="create-other")
    attention_id = uuid.UUID(outcome.body["campaign"]["id"])
    _set_sync_state(session_factory, syncing_id, SyncState.SYNCING)
    _set_sync_state(session_factory, attention_id, SyncState.NEEDS_ATTENTION)

    repo = CampaignRepository()
    with session_factory() as session:
        assert repo.reset_stale_syncing(session) == 1
        session.commit()

    assert _campaign(session_factory, syncing_id).sync_state is SyncState.DIRTY
    assert _campaign(session_factory, attention_id).sync_state is SyncState.NEEDS_ATTENTION
