"""Database-level integrity: uniqueness, foreign keys, cascades, checks."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.db.engine import session_scope
from app.db.models import (
    BaselineSnapshot,
    BaselineSource,
    Campaign,
    IdempotencyRecord,
    IdempotencyState,
    OfferAssociationState,
    Stream,
    StreamOffer,
    StreamState,
    SyncState,
)
from tests.db.factories import canonical, draft_offer, draft_stream, fingerprint


def _campaign(**overrides) -> Campaign:
    values: dict = {
        "id": uuid.uuid4(),
        "keitaro_id": 9001,
        "name": "C",
        "revision": 1,
        "sync_state": SyncState.CLEAN,
        "baseline_canonical": canonical(),
        "baseline_fingerprint": fingerprint(),
    }
    values.update(overrides)
    return Campaign(**values)


def _stream(campaign_id: uuid.UUID, **overrides) -> Stream:
    values: dict = {
        "id": uuid.uuid4(),
        "campaign_id": campaign_id,
        "keitaro_id": 1001,
        "name": "S",
        "type": "regular",
        "schema_": "landings",
        "position": 0,
        "state": StreamState.ACTIVE,
        "share": 50,
        "baseline_payload": {"id": 1001},
    }
    values.update(overrides)
    return Stream(**values)


def _offer(stream_id: uuid.UUID, **overrides) -> StreamOffer:
    values: dict = {
        "id": uuid.uuid4(),
        "stream_id": stream_id,
        "offer_id": 2001,
        "offer_name": "O",
        "state": OfferAssociationState.ACTIVE,
        "share": 100,
    }
    values.update(overrides)
    return StreamOffer(**values)


def test_keitaro_campaign_id_is_unique(session: Session) -> None:
    session.add(_campaign(keitaro_id=42, baseline_canonical=canonical("one")))
    session.add(_campaign(keitaro_id=42, baseline_canonical=canonical("two")))
    with pytest.raises(IntegrityError):
        session.flush()


def test_keitaro_stream_id_is_unique_per_campaign(session: Session) -> None:
    campaign = _campaign()
    session.add(campaign)
    session.flush()
    session.add(_stream(campaign.id, keitaro_id=7))
    session.add(_stream(campaign.id, keitaro_id=7, position=1))
    with pytest.raises(IntegrityError):
        session.flush()


def test_same_keitaro_stream_id_allowed_across_campaigns(session: Session) -> None:
    first = _campaign(keitaro_id=1)
    second = _campaign(keitaro_id=2)
    session.add_all([first, second])
    session.flush()
    session.add(_stream(first.id, keitaro_id=7))
    session.add(_stream(second.id, keitaro_id=7))
    session.flush()


def test_one_association_per_stream_and_offer(session: Session) -> None:
    campaign = _campaign()
    session.add(campaign)
    session.flush()
    stream = _stream(campaign.id)
    session.add(stream)
    session.flush()
    session.add(_offer(stream.id, offer_id=5))
    session.add(_offer(stream.id, offer_id=5, share=0, state=OfferAssociationState.DISABLED))
    with pytest.raises(IntegrityError):
        session.flush()


def test_stream_rejects_unknown_campaign(session: Session) -> None:
    session.add(_stream(uuid.uuid4()))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.flush()


def test_offer_rejects_unknown_stream(session: Session) -> None:
    session.add(_offer(uuid.uuid4()))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.flush()


def test_snapshot_rejects_unknown_campaign(session: Session) -> None:
    session.add(
        BaselineSnapshot(
            campaign_id=uuid.uuid4(),
            canonical=canonical(),
            fingerprint=fingerprint(),
            source=BaselineSource.FETCH,
        )
    )
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        session.flush()


@pytest.mark.parametrize("share", [-1, 101, 150])
def test_stream_share_range_enforced(session: Session, share: int) -> None:
    campaign = _campaign()
    session.add(campaign)
    session.flush()
    session.add(_stream(campaign.id, share=share))
    with pytest.raises(IntegrityError, match="ck_streams_share_range"):
        session.flush()


@pytest.mark.parametrize("share", [-1, 101])
def test_offer_share_range_enforced(session: Session, share: int) -> None:
    campaign = _campaign()
    session.add(campaign)
    session.flush()
    stream = _stream(campaign.id)
    session.add(stream)
    session.flush()
    session.add(_offer(stream.id, share=share))
    with pytest.raises(IntegrityError, match="ck_stream_offers_share_range"):
        session.flush()


def test_invalid_sync_state_rejected_by_orm(session: Session) -> None:
    session.add(
        Campaign(
            keitaro_id=5,
            name="x",
            sync_state="bogus",
            baseline_canonical={},
            baseline_fingerprint=fingerprint(),
        )
    )
    with pytest.raises(StatementError):
        session.flush()
    session.rollback()


def test_sync_state_check_constraint_via_raw_update(session: Session) -> None:
    campaign = _campaign(keitaro_id=6)
    session.add(campaign)
    session.flush()
    with pytest.raises(IntegrityError, match="ck_campaigns_sync_state"):
        # Raw SQL bypasses the Python enum binder so the database CHECK is
        # what rejects the value. The PK is stored as undashed hex.
        session.execute(
            sa.text(f"UPDATE campaigns SET sync_state='bogus' WHERE id='{campaign.id.hex}'")
        )


def test_deleting_campaign_cascades_streams_offers_and_snapshots(
    campaigns, session_factory
) -> None:
    with session_scope(session_factory) as session:
        campaign = campaigns.create_campaign(
            session,
            keitaro_id=10,
            name="Cascade",
            baseline_canonical=canonical(),
            baseline_fingerprint=fingerprint(),
        )
        campaigns.replace_draft(
            session,
            campaign_id=campaign.id,
            expected_revision=1,
            streams=[draft_stream(1001, offers=(draft_offer(2001),))],
        )
        campaign_id = campaign.id

    with session_scope(session_factory) as session:
        session.delete(campaigns.require(session, campaign_id))

    with session_scope(session_factory) as session:
        assert session.scalars(sa.select(Stream)).all() == []
        assert session.scalars(sa.select(StreamOffer)).all() == []
        assert session.scalars(sa.select(BaselineSnapshot)).all() == []


def test_bulk_deleting_stream_cascades_offers_at_database_level(campaigns, session_factory) -> None:
    with session_scope(session_factory) as session:
        campaign = campaigns.create_campaign(
            session,
            keitaro_id=11,
            name="Bulk",
            baseline_canonical=canonical(),
            baseline_fingerprint=fingerprint(),
        )
        campaigns.replace_draft(
            session,
            campaign_id=campaign.id,
            expected_revision=1,
            streams=[draft_stream(1001, offers=(draft_offer(2001),))],
        )
        campaign_id = campaign.id

    with session_scope(session_factory) as session:
        campaign = campaigns.require(session, campaign_id)
        session.execute(sa.delete(Stream).where(Stream.campaign_id == campaign.id))

    with session_scope(session_factory) as session:
        assert session.scalars(sa.select(StreamOffer)).all() == []


def test_deleting_campaign_preserves_idempotency_record(session: Session) -> None:
    campaign = _campaign(keitaro_id=20, idempotency_key="key-20")
    session.add(campaign)
    session.flush()
    session.add(
        IdempotencyRecord(
            key="key-20",
            request_hash=fingerprint("r"),
            state=IdempotencyState.SUCCEEDED,
            result={"campaign_id": 20},
            campaign_id=campaign.id,
        )
    )
    session.flush()
    session.delete(campaign)
    session.flush()
    record = session.scalars(sa.select(IdempotencyRecord)).one()
    assert record.campaign_id is None  # SET NULL keeps the audit record
    assert record.result == {"campaign_id": 20}
