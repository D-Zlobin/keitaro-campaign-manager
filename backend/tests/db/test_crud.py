"""Campaign CRUD through the repository, read back in fresh sessions."""

from __future__ import annotations

import uuid

import pytest

from app.db.engine import session_scope
from app.db.models import (
    BaselineSource,
    Campaign,
    OfferAssociationState,
    StreamState,
    SyncState,
)
from app.repositories.exceptions import CampaignNotFoundError
from tests.db.factories import canonical, draft_offer, draft_stream, fingerprint


def _create_campaign_with_state(
    campaigns,
    session_factory,
    *,
    keitaro_id: int = 501,
    name: str = "Example DE",
    country_code: str | None = "DE",
) -> Campaign:
    with session_scope(session_factory) as session:
        campaign = campaigns.create_campaign(
            session,
            keitaro_id=keitaro_id,
            name=name,
            country_code=country_code,
            baseline_canonical=canonical("initial"),
            baseline_fingerprint=fingerprint("a"),
            idempotency_key=f"key-{keitaro_id}",
            idempotency_request_hash=fingerprint("r"),
        )
        campaigns.replace_draft(
            session,
            campaign_id=campaign.id,
            expected_revision=1,
            streams=[
                draft_stream(
                    1001,
                    name="Google — DE",
                    schema="redirect",
                    position=0,
                    share=50,
                    offers=(),
                ),
                draft_stream(
                    1002,
                    name="Offer — 2001",
                    position=1,
                    share=50,
                    offers=(
                        draft_offer(
                            2001,
                            keitaro_association_id=77,
                            baseline_payload={"id": 77, "offer_id": 2001},
                        ),
                    ),
                ),
            ],
        )
        return campaign


def test_create_and_read_back_campaign_with_draft(campaigns, session_factory) -> None:
    created = _create_campaign_with_state(campaigns, session_factory)

    with session_scope(session_factory) as session:
        campaign = campaigns.get_with_draft(session, created.id)
        assert campaign is not None
        assert campaign.keitaro_id == 501
        assert campaign.name == "Example DE"
        assert campaign.country_code == "DE"
        assert campaign.revision == 2  # initial draft replacement incremented it
        assert campaign.sync_state is SyncState.CLEAN
        assert campaign.idempotency_key == "key-501"
        assert campaign.baseline_fingerprint == fingerprint("a")
        assert campaign.baseline_canonical == canonical("initial")
        assert campaign.last_fetch_at is None
        assert campaign.last_upload_at is None

        assert [s.keitaro_id for s in campaign.streams] == [1001, 1002]
        assert campaign.streams[0].schema_ == "redirect"
        assert campaign.streams[1].state is StreamState.ACTIVE
        assert campaign.streams[1].offers[0].offer_id == 2001
        assert campaign.streams[1].offers[0].offer_name == "Offer 2001"
        assert campaign.streams[1].offers[0].keitaro_association_id == 77
        assert campaign.streams[1].offers[0].baseline_payload == {"id": 77, "offer_id": 2001}


def test_baseline_snapshot_row_created_with_campaign(campaigns, session_factory) -> None:
    created = _create_campaign_with_state(campaigns, session_factory)

    with session_scope(session_factory) as session:
        campaign = campaigns.require(session, created.id)
        snapshots = campaign.baseline_snapshots
        assert len(snapshots) == 1
        assert snapshots[0].source is BaselineSource.CREATE
        assert snapshots[0].canonical == canonical("initial")


def test_get_returns_none_for_missing(campaigns, session) -> None:
    assert campaigns.get(session, uuid.uuid4()) is None
    assert campaigns.get_with_draft(session, uuid.uuid4()) is None


def test_require_raises_for_missing(campaigns, session) -> None:
    with pytest.raises(CampaignNotFoundError):
        campaigns.require(session, uuid.uuid4())


def test_list_campaigns_orders_by_name_then_keitaro_id(campaigns, session_factory) -> None:
    _create_campaign_with_state(campaigns, session_factory, keitaro_id=3, name="Beta")
    _create_campaign_with_state(campaigns, session_factory, keitaro_id=1, name="Alpha")
    _create_campaign_with_state(campaigns, session_factory, keitaro_id=2, name="Alpha")

    with session_scope(session_factory) as session:
        listed = campaigns.list_campaigns(session)
        assert [(c.name, c.keitaro_id) for c in listed] == [
            ("Alpha", 1),
            ("Alpha", 2),
            ("Beta", 3),
        ]


def test_offer_states_round_trip(campaigns, session_factory) -> None:
    with session_scope(session_factory) as session:
        campaign = campaigns.create_campaign(
            session,
            keitaro_id=601,
            name="Offers",
            country_code=None,
            baseline_canonical=canonical(),
            baseline_fingerprint=fingerprint(),
        )
        campaigns.replace_draft(
            session,
            campaign_id=campaign.id,
            expected_revision=1,
            streams=[
                draft_stream(
                    1101,
                    offers=(
                        draft_offer(2101, state=OfferAssociationState.ACTIVE, share=60),
                        draft_offer(
                            2102,
                            state=OfferAssociationState.DISABLED,
                            share=0,
                            keitaro_association_id=88,
                        ),
                    ),
                )
            ],
        )
        campaign_id = campaign.id

    with session_scope(session_factory) as session:
        campaign = campaigns.get_with_draft(session, campaign_id)
        assert campaign is not None
        offers = campaign.streams[0].offers
        assert [(o.offer_id, o.state, o.share) for o in offers] == [
            (2101, OfferAssociationState.ACTIVE, 60),
            (2102, OfferAssociationState.DISABLED, 0),
        ]
