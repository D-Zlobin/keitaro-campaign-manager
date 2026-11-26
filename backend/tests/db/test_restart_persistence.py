"""Backend-restart durability: committed state survives engine disposal."""

from __future__ import annotations

import sqlalchemy as sa

from app.db.engine import build_engine, make_session_factory, session_scope
from app.db.models import (
    BaselineSnapshot,
    BaselineSource,
    OfferAssociationState,
    StreamState,
    SyncState,
)
from tests.db.conftest import upgrade_database
from tests.db.factories import canonical, draft_offer, draft_stream, fingerprint


def test_all_state_survives_restart(database_url: str, campaigns) -> None:
    upgrade_database(database_url)
    first_engine = build_engine(database_url)
    try:
        factory = make_session_factory(first_engine)
        with session_scope(factory) as session:
            campaign = campaigns.create_campaign(
                session,
                keitaro_id=880,
                name="Survivor",
                country_code="DE",
                baseline_canonical=canonical("baseline"),
                baseline_fingerprint=fingerprint("f"),
                idempotency_key="survivor-key",
                idempotency_request_hash=fingerprint("h"),
            )
            campaigns.replace_draft(
                session,
                campaign_id=campaign.id,
                expected_revision=1,
                sync_state=SyncState.DIRTY,
                streams=[
                    draft_stream(
                        1001,
                        position=0,
                        share=100,
                        offers=(
                            draft_offer(
                                2001,
                                state=OfferAssociationState.DISABLED,
                                share=0,
                                keitaro_association_id=55,
                            ),
                            draft_offer(2002, share=100, pinned=True),
                        ),
                    ),
                    draft_stream(1002, position=1, share=0, state=StreamState.DISABLED),
                ],
            )
            campaigns.transition_sync_state(
                session, campaign_id=campaign.id, to_state=SyncState.SYNCING
            )
            campaigns.transition_sync_state(
                session, campaign_id=campaign.id, to_state=SyncState.NEEDS_ATTENTION
            )
            campaigns.mark_fetched(session, campaign.id)
            campaigns.mark_uploaded(session, campaign.id)
            campaign_id = campaign.id
    finally:
        first_engine.dispose()

    # Restart: brand-new engine and session factory on the same file.
    second_engine = build_engine(database_url)
    try:
        factory = make_session_factory(second_engine)
        with session_scope(factory) as session:
            campaign = campaigns.get_with_draft(session, campaign_id)
            assert campaign is not None
            assert campaign.keitaro_id == 880
            assert campaign.name == "Survivor"
            assert campaign.country_code == "DE"
            assert campaign.idempotency_key == "survivor-key"
            assert campaign.revision == 2
            assert campaign.sync_state is SyncState.NEEDS_ATTENTION
            assert campaign.baseline_fingerprint == fingerprint("f")
            assert campaign.baseline_canonical == canonical("baseline")
            assert campaign.last_fetch_at is not None
            assert campaign.last_upload_at is not None

            assert [(s.keitaro_id, s.share, s.state) for s in campaign.streams] == [
                (1001, 100, StreamState.ACTIVE),
                (1002, 0, StreamState.DISABLED),
            ]
            offers = {o.offer_id: o for o in campaign.streams[0].offers}
            assert set(offers) == {2001, 2002}
            assert offers[2001].state is OfferAssociationState.DISABLED
            assert offers[2001].share == 0
            assert offers[2001].pinned is False
            assert offers[2001].keitaro_association_id == 55
            assert offers[2001].baseline_payload is None
            # Offer pins are local metadata and must survive a restart too.
            assert offers[2002].share == 100
            assert offers[2002].pinned is True

            snapshots = session.scalars(
                sa.select(BaselineSnapshot).where(BaselineSnapshot.campaign_id == campaign_id)
            ).all()
            assert len(snapshots) == 1
            assert snapshots[0].source is BaselineSource.CREATE
    finally:
        second_engine.dispose()
