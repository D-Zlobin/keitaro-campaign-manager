"""Campaign import: clean baseline creation and duplicate rejection."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import BaselineSnapshot, Campaign, Stream, StreamOffer
from app.errors import ConflictError, NotFoundError, UpstreamTimeoutError
from app.keitaro.client import KeitaroTimeoutError
from app.keitaro.models import KeitaroOffer
from app.services.campaigns import CampaignService
from tests.fakes import DEFAULT_OFFER_ID, FakeKeitaroAdapter


def _seed_two_stream_campaign(fake: FakeKeitaroAdapter, **kwargs) -> int:
    campaign = fake.seed_campaign(**kwargs)
    fake.seed_stream(
        campaign_id=campaign.id,
        name="Google — DE",
        schema="redirect",
        action_type="redirect",
        action_payload="https://www.google.com/",
        position=0,
        weight=50,
        filters=[{"name": "country", "mode": "accept", "payload": ["DE"]}],
    )
    fake.seed_stream(
        campaign_id=campaign.id,
        name=f"Offer — {DEFAULT_OFFER_ID}",
        schema="landings",
        position=1,
        weight=50,
        filters=[{"name": "country", "mode": "reject", "payload": ["DE"]}],
        offers=[{"offer_id": DEFAULT_OFFER_ID, "state": "active", "share": 100}],
    )
    return campaign.id


def test_import_persists_baseline_and_draft(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    keitaro_id = _seed_two_stream_campaign(fake_keitaro, name="Imported DE")

    result = service.import_campaign(keitaro_id)

    assert result.campaign.keitaro_id == keitaro_id
    assert result.campaign.name == "Imported DE"
    assert result.campaign.country_code is None
    assert result.campaign.sync_state == "clean"
    assert result.campaign.revision == 1
    assert result.campaign.last_fetch_at is not None
    assert result.notices == []

    with session_factory() as session:
        campaign = session.scalars(select(Campaign)).one()
        assert campaign.baseline_canonical["campaign"]["id"] == keitaro_id
        assert len(campaign.baseline_canonical["streams"]) == 2
        snapshot = session.scalars(select(BaselineSnapshot)).one()
        assert snapshot.source.value == "fetch"

        streams = session.scalars(select(Stream).order_by(Stream.position)).all()
        assert [(s.keitaro_id, s.share, s.state.value) for s in streams] == [
            (900, 50, "active"),
            (901, 50, "active"),
        ]
        (offer,) = session.scalars(select(StreamOffer)).all()
        assert offer.offer_id == DEFAULT_OFFER_ID
        assert offer.offer_name == "Example Offer"  # resolved via get_offer
        assert offer.share == 100
        assert offer.keitaro_association_id is not None
        assert offer.baseline_payload["offer_id"] == DEFAULT_OFFER_ID


def test_import_includes_disabled_streams_with_zero_share(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign = fake_keitaro.seed_campaign(name="With disabled")
    fake_keitaro.seed_stream(campaign_id=campaign.id, name="Active", position=0, weight=100)
    fake_keitaro.seed_stream(
        campaign_id=campaign.id,
        name="Disabled",
        position=1,
        weight=25,
        state="disabled",
        offers=[{"offer_id": DEFAULT_OFFER_ID, "state": "active", "share": 100}],
    )

    result = service.import_campaign(campaign.id)

    assert result.campaign.sync_state == "clean"
    with session_factory() as session:
        streams = session.scalars(select(Stream).order_by(Stream.position)).all()
        assert [(s.name, s.state.value, s.share) for s in streams] == [
            ("Active", "active", 100),
            ("Disabled", "disabled", 0),
        ]
        # Disabled streams keep their remote offer shares (spec section 6.5
        # normalizes active streams only).
        (offer,) = session.scalars(select(StreamOffer)).all()
        assert offer.share == 100


def test_import_rejects_duplicate_without_replacing_state(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    keitaro_id = _seed_two_stream_campaign(fake_keitaro)
    first = service.import_campaign(keitaro_id)

    with pytest.raises(ConflictError) as exc_info:
        service.import_campaign(keitaro_id)

    assert exc_info.value.code == "campaign_exists"
    assert exc_info.value.details["campaign_id"] == str(first.campaign.id)
    with session_factory() as session:
        campaign = session.scalars(select(Campaign)).one()
        assert campaign.revision == 1  # untouched by the rejected import
        assert len(session.scalars(select(BaselineSnapshot)).all()) == 1


def test_import_unknown_remote_campaign_is_404(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    with pytest.raises(NotFoundError):
        service.import_campaign(424242)


def test_import_remotely_deleted_campaign_is_404(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    campaign = fake_keitaro.seed_campaign(state="deleted")

    with pytest.raises(NotFoundError):
        service.import_campaign(campaign.id)


def test_import_timeout_maps_to_504(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("get_campaign", KeitaroTimeoutError("timed out"))

    with pytest.raises(UpstreamTimeoutError):
        service.import_campaign(223)


def test_import_normalizes_invalid_stream_totals(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign = fake_keitaro.seed_campaign(name="Skewed")
    fake_keitaro.seed_stream(campaign_id=campaign.id, name="A", position=0, weight=60)
    fake_keitaro.seed_stream(campaign_id=campaign.id, name="B", position=1, weight=60)

    result = service.import_campaign(campaign.id)

    assert result.campaign.sync_state == "dirty"
    assert result.campaign.dirty is True
    assert any("redistributed equally" in notice for notice in result.notices)
    with session_factory() as session:
        streams = session.scalars(select(Stream).order_by(Stream.position)).all()
        assert [s.share for s in streams] == [50, 50]
        # The baseline keeps the raw remote weights for conflict detection.
        stored = session.scalars(select(Campaign)).one()
        baseline_weights = [s["weight"] for s in stored.baseline_canonical["streams"]]
        assert baseline_weights == [60, 60]


def test_import_normalizes_invalid_offer_shares(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    campaign = fake_keitaro.seed_campaign(name="Offer shares")
    fake_keitaro.offers[18002] = KeitaroOffer(id=18002, name="Second Offer", state="active")
    fake_keitaro.seed_stream(
        campaign_id=campaign.id,
        name="Offers",
        position=0,
        weight=100,
        offers=[
            {"offer_id": DEFAULT_OFFER_ID, "state": "active", "share": 30},
            {"offer_id": 18002, "state": "active", "share": 30},
        ],
    )

    result = service.import_campaign(campaign.id)

    assert result.campaign.sync_state == "dirty"
    assert any("active offer shares" in notice for notice in result.notices)
    with session_factory() as session:
        offers = session.scalars(select(StreamOffer).order_by(StreamOffer.offer_id)).all()
        assert [(o.offer_id, o.share, o.offer_name) for o in offers] == [
            (DEFAULT_OFFER_ID, 50, "Example Offer"),
            (18002, 50, "Second Offer"),
        ]
