"""Synchronization-state machine enforcement."""

from __future__ import annotations

import pytest

from app.db.engine import session_scope
from app.db.models import SyncState
from app.repositories.exceptions import InvalidSyncTransitionError
from tests.db.factories import canonical, draft_stream, fingerprint


def _seed(campaigns, session_factory, keitaro_id: int = 800) -> object:
    with session_scope(session_factory) as session:
        campaign = campaigns.create_campaign(
            session,
            keitaro_id=keitaro_id,
            name="Sync",
            baseline_canonical=canonical(),
            baseline_fingerprint=fingerprint(),
        )
        campaigns.replace_draft(
            session,
            campaign_id=campaign.id,
            expected_revision=1,
            streams=[draft_stream(1001)],
        )
        return campaign.id


@pytest.mark.parametrize(
    "steps",
    [
        [SyncState.DIRTY],
        [SyncState.SYNCING, SyncState.CLEAN],
        [SyncState.DIRTY, SyncState.SYNCING, SyncState.DIRTY],
        [SyncState.DIRTY, SyncState.SYNCING, SyncState.NEEDS_ATTENTION, SyncState.CLEAN],
    ],
)
def test_valid_transition_sequences(campaigns, session_factory, steps) -> None:
    campaign_id = _seed(campaigns, session_factory)

    with session_scope(session_factory) as session:
        for state in steps:
            campaigns.transition_sync_state(session, campaign_id=campaign_id, to_state=state)
        assert campaigns.require(session, campaign_id).sync_state is steps[-1]


@pytest.mark.parametrize(
    ("reach", "attempt"),
    [
        ([SyncState.DIRTY, SyncState.SYNCING, SyncState.NEEDS_ATTENTION], SyncState.DIRTY),
        ([SyncState.DIRTY, SyncState.SYNCING, SyncState.NEEDS_ATTENTION], SyncState.SYNCING),
        ([], SyncState.NEEDS_ATTENTION),
        ([SyncState.DIRTY], SyncState.NEEDS_ATTENTION),
        ([], SyncState.CLEAN),
        ([SyncState.DIRTY, SyncState.SYNCING], SyncState.SYNCING),
    ],
)
def test_invalid_transitions_raise_and_leave_state_unchanged(
    campaigns, session_factory, reach, attempt
) -> None:
    campaign_id = _seed(campaigns, session_factory)

    with session_scope(session_factory) as session:
        for state in reach:
            campaigns.transition_sync_state(session, campaign_id=campaign_id, to_state=state)
        with pytest.raises(InvalidSyncTransitionError):
            campaigns.transition_sync_state(session, campaign_id=campaign_id, to_state=attempt)
        expected = reach[-1] if reach else SyncState.CLEAN
        assert campaigns.require(session, campaign_id).sync_state is expected
