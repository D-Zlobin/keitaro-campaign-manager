"""Baseline replacement and immutable snapshot history."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from app.db.engine import session_scope
from app.db.models import BaselineSnapshot, BaselineSource
from tests.db.factories import canonical, draft_stream, fingerprint


def _seed(campaigns, session_factory, keitaro_id: int = 700) -> object:
    with session_scope(session_factory) as session:
        campaign = campaigns.create_campaign(
            session,
            keitaro_id=keitaro_id,
            name="Baseline",
            baseline_canonical=canonical("v1"),
            baseline_fingerprint=fingerprint("a"),
        )
        campaigns.replace_draft(
            session,
            campaign_id=campaign.id,
            expected_revision=1,
            streams=[draft_stream(1001)],
        )
        return campaign.id


def test_replace_baseline_appends_snapshot_and_updates_campaign(campaigns, session_factory) -> None:
    campaign_id = _seed(campaigns, session_factory)

    with session_scope(session_factory) as session:
        snapshot = campaigns.replace_baseline(
            session,
            campaign_id=campaign_id,
            canonical=canonical("v2"),
            fingerprint=fingerprint("b"),
            source=BaselineSource.FETCH,
        )
        assert snapshot.source is BaselineSource.FETCH

    with session_scope(session_factory) as session:
        campaign = campaigns.require(session, campaign_id)
        assert campaign.baseline_canonical == canonical("v2")
        assert campaign.baseline_fingerprint == fingerprint("b")
        snapshots = campaign.baseline_snapshots
        assert len(snapshots) == 2
        assert [s.source for s in snapshots] == [BaselineSource.CREATE, BaselineSource.FETCH]
        # Earlier snapshots are immutable: the first row still holds v1 data.
        assert snapshots[0].canonical == canonical("v1")
        assert snapshots[0].fingerprint == fingerprint("a")
        assert snapshots[1].canonical == canonical("v2")


def test_snapshot_canonical_isolation(campaigns, session_factory) -> None:
    """Mutating the dict after replacement must not corrupt stored rows."""
    campaign_id = _seed(campaigns, session_factory)
    canonical_doc = canonical("v2", streams=[{"id": 1}])

    with session_scope(session_factory) as session:
        campaigns.replace_baseline(
            session,
            campaign_id=campaign_id,
            canonical=canonical_doc,
            fingerprint=fingerprint("b"),
            source=BaselineSource.UPLOAD,
        )
        canonical_doc["streams"][0]["id"] = 999
        canonical_doc["extra"] = "mutated"

    with session_scope(session_factory) as session:
        campaign = campaigns.require(session, campaign_id)
        assert campaign.baseline_canonical == {"tag": "v2", "streams": [{"id": 1}]}
        snapshot = campaign.baseline_snapshots[-1]
        assert snapshot.canonical == {"tag": "v2", "streams": [{"id": 1}]}


def test_mark_fetched_and_uploaded_timestamps(campaigns, session_factory) -> None:
    campaign_id = _seed(campaigns, session_factory)
    fetch_time = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    upload_time = datetime(2026, 11, 25, 11, 30, tzinfo=UTC)

    with session_scope(session_factory) as session:
        campaigns.mark_fetched(session, campaign_id, when=fetch_time)
        campaigns.mark_uploaded(session, campaign_id, when=upload_time)

    with session_scope(session_factory) as session:
        campaign = campaigns.require(session, campaign_id)
        # SQLite returns naive datetimes; compare the instants.
        assert campaign.last_fetch_at == fetch_time.replace(tzinfo=None)
        assert campaign.last_upload_at == upload_time.replace(tzinfo=None)


def test_snapshot_rows_are_never_updated(campaigns, session_factory) -> None:
    campaign_id = _seed(campaigns, session_factory)

    with session_scope(session_factory) as session:
        first = session.scalars(
            sa.select(BaselineSnapshot).where(BaselineSnapshot.campaign_id == campaign_id)
        ).one()
        first_id = first.id
        created = first.created_at

    # A later replacement adds a row instead of touching the first one.
    with session_scope(session_factory) as session:
        campaigns.replace_baseline(
            session,
            campaign_id=campaign_id,
            canonical=canonical("v2"),
            fingerprint=fingerprint("b"),
            source=BaselineSource.FETCH,
        )

    with session_scope(session_factory) as session:
        snapshots = session.scalars(
            sa.select(BaselineSnapshot)
            .where(BaselineSnapshot.campaign_id == campaign_id)
            .order_by(BaselineSnapshot.created_at)
        ).all()
        assert len(snapshots) == 2
        assert snapshots[0].id == first_id
        assert snapshots[0].created_at == created
        assert snapshots[0].fingerprint == fingerprint("a")
