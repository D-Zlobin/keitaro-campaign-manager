"""Operator-initiated fetch: clean/dirty enforcement and normalization.

Covers plan stage 4 fetch behavior: transactional baseline/draft replacement,
pin clearing, revision increments, normalization/deletion notices, dirty-fetch
409 with and without confirmation, needs_attention recovery, and upstream
error mapping.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import Campaign, Stream, StreamOffer, SyncState
from app.errors import ConflictError, NotFoundError, UpstreamTimeoutError
from app.keitaro.client import KeitaroTimeoutError
from app.repositories.campaigns import CampaignRepository
from app.repositories.exceptions import StaleRevisionError
from app.services.campaigns import CampaignService, CreateCampaignCommand
from tests.fakes import DEFAULT_OFFER_ID, FakeKeitaroAdapter

COMMAND = CreateCampaignCommand(name="Example DE", country_code="DE", offer_id=DEFAULT_OFFER_ID)


def _create(service: CampaignService) -> uuid.UUID:
    outcome = service.create_campaign(COMMAND, idempotency_key=f"create-{COMMAND.name}")
    return uuid.UUID(outcome.body["campaign"]["id"])


def _streams(session_factory, campaign_id: uuid.UUID) -> list[Stream]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(Stream)
                .where(Stream.campaign_id == campaign_id)
                .order_by(Stream.position, Stream.keitaro_id)
            )
        )


def test_clean_fetch_replaces_baseline_and_draft(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign_id = _create(service)
    first_stream, second_stream = _streams(session_factory, campaign_id)
    # Remote weights change to another valid total; timestamps move.
    fake_keitaro.touch_stream(first_stream.keitaro_id, weight=70)
    fake_keitaro.touch_stream(second_stream.keitaro_id, weight=30)
    fake_keitaro.touch_campaign(500)
    with session_factory() as session:
        fingerprint_before = session.get(Campaign, campaign_id).baseline_fingerprint

    result = service.fetch_campaign(campaign_id, confirm_discard=False)

    assert result.campaign.sync_state == "clean"
    assert result.campaign.revision == 2
    assert result.campaign.last_fetch_at is not None
    assert result.notices == []
    assert [(s.share, s.pinned) for s in _streams(session_factory, campaign_id)] == [
        (70, False),
        (30, False),
    ]
    with session_factory() as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign.baseline_fingerprint != fingerprint_before
        baseline_weights = [s["weight"] for s in campaign.baseline_canonical["streams"]]
        assert baseline_weights == [70, 30]


def test_fetch_clears_pins(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign_id = _create(service)
    with session_factory() as session:
        stream = session.scalars(select(Stream)).first()
        stream.pinned = True
        session.commit()

    service.fetch_campaign(campaign_id, confirm_discard=False)

    assert [s.pinned for s in _streams(session_factory, campaign_id)] == [False, False]


def test_dirty_fetch_without_confirmation_is_rejected(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign_id = _create(service)
    repo = CampaignRepository()
    with session_factory() as session:
        campaign = repo.require(session, campaign_id)
        repo.replace_draft(
            session,
            campaign_id=campaign_id,
            expected_revision=campaign.revision,
            streams=_draft_with_share(session, campaign_id, share=80),
            sync_state=SyncState.DIRTY,
        )
        session.commit()
    fake_keitaro.touch_stream(900, weight=70)
    fake_keitaro.touch_stream(901, weight=30)

    with pytest.raises(ConflictError) as exc_info:
        service.fetch_campaign(campaign_id, confirm_discard=False)

    assert exc_info.value.code == "dirty_draft"
    # Neither baseline nor draft changed.
    assert [s.share for s in _streams(session_factory, campaign_id)] == [80, 20]
    with session_factory() as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign.revision == 2
        assert campaign.sync_state is SyncState.DIRTY
        baseline_weights = [s["weight"] for s in campaign.baseline_canonical["streams"]]
        assert baseline_weights == [50, 50]


def test_dirty_fetch_with_confirmation_discards_draft(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign_id = _create(service)
    repo = CampaignRepository()
    with session_factory() as session:
        campaign = repo.require(session, campaign_id)
        repo.replace_draft(
            session,
            campaign_id=campaign_id,
            expected_revision=campaign.revision,
            streams=_draft_with_share(session, campaign_id, share=80),
            sync_state=SyncState.DIRTY,
        )
        session.commit()

    result = service.fetch_campaign(campaign_id, confirm_discard=True)

    assert result.campaign.sync_state == "clean"
    assert result.campaign.revision == 3
    assert [s.share for s in _streams(session_factory, campaign_id)] == [50, 50]


def _draft_with_share(session, campaign_id: uuid.UUID, *, share: int):
    """Current draft rows with the stream shares replaced (first gets ``share``)."""
    from app.repositories.campaigns import DraftOfferInput, DraftStreamInput

    campaign = CampaignRepository().get_with_draft(session, campaign_id)
    inputs = []
    for position, s in enumerate(campaign.streams):
        inputs.append(
            DraftStreamInput(
                keitaro_id=s.keitaro_id,
                name=s.name,
                type=s.type,
                schema=s.schema_,
                position=s.position,
                state=s.state,
                share=share if position == 0 else 100 - share,
                pinned=False,
                baseline_payload=s.baseline_payload,
                offers=tuple(
                    DraftOfferInput(
                        offer_id=o.offer_id,
                        offer_name=o.offer_name,
                        state=o.state,
                        share=o.share,
                        keitaro_association_id=o.keitaro_association_id,
                        baseline_payload=o.baseline_payload,
                    )
                    for o in s.offers
                ),
            )
        )
    return inputs


def test_needs_attention_requires_confirmation_and_recovers(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign_id = _create(service)
    repo = CampaignRepository()
    with session_factory() as session:
        repo.transition_sync_state(session, campaign_id=campaign_id, to_state=SyncState.SYNCING)
        repo.transition_sync_state(
            session, campaign_id=campaign_id, to_state=SyncState.NEEDS_ATTENTION
        )
        session.commit()

    with pytest.raises(ConflictError):
        service.fetch_campaign(campaign_id, confirm_discard=False)

    result = service.fetch_campaign(campaign_id, confirm_discard=True)

    assert result.campaign.sync_state == "clean"
    with session_factory() as session:
        assert session.get(Campaign, campaign_id).sync_state is SyncState.CLEAN


def test_fetch_while_syncing_is_rejected(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign_id = _create(service)
    with session_factory() as session:
        CampaignRepository().transition_sync_state(
            session, campaign_id=campaign_id, to_state=SyncState.SYNCING
        )
        session.commit()

    with pytest.raises(ConflictError) as exc_info:
        service.fetch_campaign(campaign_id, confirm_discard=False)

    assert exc_info.value.code == "operation_in_progress"


def test_fetch_unknown_campaign_is_404(service: CampaignService) -> None:
    with pytest.raises(NotFoundError):
        service.fetch_campaign(uuid.uuid4(), confirm_discard=False)


def test_fetch_remotely_deleted_campaign_is_404(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign_id = _create(service)
    fake_keitaro.touch_campaign(500, state="deleted")

    with pytest.raises(NotFoundError):
        service.fetch_campaign(campaign_id, confirm_discard=False)


def test_fetch_timeout_maps_to_504(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign_id = _create(service)
    fake_keitaro.queue_failure("get_campaign", KeitaroTimeoutError("timed out"))

    with pytest.raises(UpstreamTimeoutError):
        service.fetch_campaign(campaign_id, confirm_discard=False)


def test_fetch_notes_remotely_deleted_streams(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign_id = _create(service)
    fake_keitaro.remove_stream(901)
    fake_keitaro.touch_stream(900, weight=100)

    result = service.fetch_campaign(campaign_id, confirm_discard=False)

    assert any("Stream 901 was deleted remotely" in notice for notice in result.notices)
    assert [s.keitaro_id for s in _streams(session_factory, campaign_id)] == [900]


def test_fetch_normalizes_invalid_remote_totals_with_notice(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign_id = _create(service)
    fake_keitaro.touch_stream(900, weight=30)
    fake_keitaro.touch_stream(901, weight=30)

    result = service.fetch_campaign(campaign_id, confirm_discard=False)

    assert result.campaign.sync_state == "dirty"
    assert any("instead of 100" in notice for notice in result.notices)
    assert [s.share for s in _streams(session_factory, campaign_id)] == [50, 50]
    # The baseline keeps the raw remote weights so the draft stays dirty.
    with session_factory() as session:
        baseline_weights = [
            s["weight"] for s in session.get(Campaign, campaign_id).baseline_canonical["streams"]
        ]
        assert baseline_weights == [30, 30]


def test_fetch_normalizes_offer_shares(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign_id = _create(service)
    stream = fake_keitaro.streams[901]
    association = stream.offers[0].model_copy(update={"share": 40})
    fake_keitaro.touch_stream(901, offers=[association])

    result = service.fetch_campaign(campaign_id, confirm_discard=False)

    assert result.campaign.sync_state == "dirty"
    assert any("active offer shares" in notice for notice in result.notices)
    with session_factory() as session:
        (offer,) = session.scalars(select(StreamOffer)).all()
        assert offer.share == 100
        # Raw association share kept in the baseline payload.
        assert offer.baseline_payload["share"] == 40


def test_fetch_stale_revision_surfaces_as_conflict(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign_id = _create(service)
    original = fake_keitaro.get_campaign_streams

    def racing_get_streams(keitaro_id: int):
        # Another writer bumps the local revision mid-fetch.
        with session_factory() as session:
            campaign = session.get(Campaign, campaign_id)
            campaign.revision += 1
            session.commit()
        return original(keitaro_id)

    fake_keitaro.get_campaign_streams = racing_get_streams  # type: ignore[method-assign]

    with pytest.raises(StaleRevisionError):
        service.fetch_campaign(campaign_id, confirm_discard=False)
