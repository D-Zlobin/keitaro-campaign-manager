"""Draft replacement: atomicity, revisions, rollback, pins, reactivation."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.db.engine import session_scope
from app.db.models import (
    BaselineSource,
    OfferAssociationState,
    Stream,
    StreamState,
    SyncState,
)
from app.repositories.exceptions import CampaignNotFoundError, StaleRevisionError
from tests.db.factories import canonical, draft_offer, draft_stream, fingerprint


def _seed_campaign(campaigns, session_factory, *, keitaro_id: int = 500):
    """Campaign with baseline A and a two-stream draft; returns its id."""
    with session_scope(session_factory) as session:
        campaign = campaigns.create_campaign(
            session,
            keitaro_id=keitaro_id,
            name="Seed",
            country_code="DE",
            baseline_canonical=canonical("baseline-a"),
            baseline_fingerprint=fingerprint("a"),
        )
        campaigns.replace_draft(
            session,
            campaign_id=campaign.id,
            expected_revision=1,
            streams=[
                draft_stream(1001, position=0, share=60, pinned=True),
                draft_stream(1002, position=1, share=40),
            ],
        )
        return campaign.id


def _draft_rows(session_factory, campaign_id) -> dict[int, tuple[int, bool, str]]:
    with session_scope(session_factory) as session:
        streams = session.scalars(sa.select(Stream).where(Stream.campaign_id == campaign_id)).all()
        return {s.keitaro_id: (s.share, s.pinned, s.state.value) for s in streams}


def test_replace_draft_swaps_rows_and_increments_revision(campaigns, session_factory) -> None:
    campaign_id = _seed_campaign(campaigns, session_factory)

    with session_scope(session_factory) as session:
        campaigns.replace_draft(
            session,
            campaign_id=campaign_id,
            expected_revision=2,
            sync_state=SyncState.DIRTY,
            streams=[
                draft_stream(
                    2001,
                    position=0,
                    share=100,
                    offers=(draft_offer(3001, share=100),),
                )
            ],
        )

    with session_scope(session_factory) as session:
        campaign = campaigns.require(session, campaign_id)
        assert campaign.revision == 3
        assert campaign.sync_state is SyncState.DIRTY
        assert [(s.keitaro_id, s.share) for s in campaign.streams] == [(2001, 100)]
        assert campaign.streams[0].offers[0].offer_id == 3001


def test_replace_draft_stale_revision_raises_and_changes_nothing(
    campaigns, session_factory
) -> None:
    campaign_id = _seed_campaign(campaigns, session_factory)
    before = _draft_rows(session_factory, campaign_id)

    with session_scope(session_factory) as session:
        try:
            campaigns.replace_draft(
                session,
                campaign_id=campaign_id,
                expected_revision=99,
                streams=[draft_stream(9999)],
            )
        except StaleRevisionError as exc:
            assert exc.expected == 99 and exc.actual == 2
        else:  # pragma: no cover - defensive
            raise AssertionError("StaleRevisionError not raised")

    with session_scope(session_factory) as session:
        campaign = campaigns.require(session, campaign_id)
        assert campaign.revision == 2
        assert _draft_rows(session_factory, campaign_id) == before


def test_failed_draft_replacement_rolls_back_baseline_and_draft(campaigns, session_factory) -> None:
    """A mid-transaction failure leaves draft AND baseline untouched."""
    campaign_id = _seed_campaign(campaigns, session_factory)
    before = _draft_rows(session_factory, campaign_id)

    try:
        with session_scope(session_factory) as session:
            campaigns.replace_baseline(
                session,
                campaign_id=campaign_id,
                canonical=canonical("baseline-b"),
                fingerprint=fingerprint("b"),
                source=BaselineSource.FETCH,
            )
            campaigns.replace_draft(
                session,
                campaign_id=campaign_id,
                expected_revision=2,
                streams=[draft_stream(1001, share=150)],  # violates range CHECK
            )
    except sa.exc.IntegrityError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("IntegrityError not raised")

    with session_scope(session_factory) as session:
        campaign = campaigns.require(session, campaign_id)
        assert campaign.revision == 2
        assert campaign.baseline_fingerprint == fingerprint("a")
        assert campaign.baseline_canonical == canonical("baseline-a")
        assert _draft_rows(session_factory, campaign_id) == before


def test_replace_draft_keeps_pins_and_zeroes_when_supplied(campaigns, session_factory) -> None:
    campaign_id = _seed_campaign(campaigns, session_factory)

    with session_scope(session_factory) as session:
        campaigns.replace_draft(
            session,
            campaign_id=campaign_id,
            expected_revision=2,
            streams=[
                draft_stream(1001, position=0, share=70, pinned=True),
                draft_stream(1002, position=1, share=0, state=StreamState.DISABLED),
            ],
        )

    assert _draft_rows(session_factory, campaign_id) == {
        1001: (70, True, "active"),
        1002: (0, False, "disabled"),
    }


def test_offer_reactivation_replaces_without_duplicate(campaigns, session_factory) -> None:
    """Re-adding a removed offer keeps a single row per (stream, offer)."""
    campaign_id = _seed_campaign(campaigns, session_factory)

    # Remove offer 3001 (disable the association).
    with session_scope(session_factory) as session:
        campaigns.replace_draft(
            session,
            campaign_id=campaign_id,
            expected_revision=2,
            streams=[
                draft_stream(
                    1001,
                    position=0,
                    share=100,
                    offers=(draft_offer(3001, share=0, state=OfferAssociationState.DISABLED),),
                )
            ],
        )

    # Re-add the same offer: replacement semantics reactivate the same row.
    with session_scope(session_factory) as session:
        campaigns.replace_draft(
            session,
            campaign_id=campaign_id,
            expected_revision=3,
            streams=[
                draft_stream(
                    1001,
                    position=0,
                    share=100,
                    offers=(draft_offer(3001, share=100),),
                )
            ],
        )

    with session_scope(session_factory) as session:
        campaign = campaigns.get_with_draft(session, campaign_id)
        stream = campaign.streams[0]
        assert len(stream.offers) == 1
        offer = stream.offers[0]
        assert offer.offer_id == 3001
        assert offer.share == 100


def test_replace_draft_on_missing_campaign_raises(campaigns, session_factory) -> None:
    with session_scope(session_factory) as session:
        try:
            campaigns.replace_draft(
                session,
                campaign_id=uuid.uuid4(),
                expected_revision=1,
                streams=[],
            )
        except CampaignNotFoundError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("CampaignNotFoundError not raised")
