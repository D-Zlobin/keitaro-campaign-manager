"""Durable creation-idempotency record behavior."""

from __future__ import annotations

import pytest

from app.db.engine import session_scope
from app.db.models import IdempotencyState
from app.repositories.idempotency import (
    IdempotencyPayloadConflict,
    IdempotencyRecordStateError,
)
from tests.db.factories import canonical, fingerprint

KEY = "create-req-1"
HASH = fingerprint("hash-1")


def test_begin_claims_new_record_in_progress(idempotency, session) -> None:
    lease = idempotency.begin(session, key=KEY, request_hash=HASH)
    assert lease.acquired is True
    assert lease.record.state is IdempotencyState.IN_PROGRESS
    assert lease.record.result is None
    session.commit()


def test_begin_same_key_same_hash_returns_existing(idempotency, session_factory) -> None:
    with session_scope(session_factory) as session:
        first = idempotency.begin(session, key=KEY, request_hash=HASH)
        record_id = first.record.id

    with session_scope(session_factory) as session:
        second = idempotency.begin(session, key=KEY, request_hash=HASH)
        assert second.acquired is False
        assert second.record.id == record_id
        assert second.record.state is IdempotencyState.IN_PROGRESS


def test_begin_same_key_different_hash_conflicts(idempotency, session_factory) -> None:
    with session_scope(session_factory) as session:
        idempotency.begin(session, key=KEY, request_hash=HASH)

    with session_scope(session_factory) as session:
        with pytest.raises(IdempotencyPayloadConflict):
            idempotency.begin(session, key=KEY, request_hash=fingerprint("other"))
        session.rollback()


def test_complete_success_stores_result_and_campaign_link(
    idempotency, campaigns, session_factory
) -> None:
    with session_scope(session_factory) as session:
        idempotency.begin(session, key=KEY, request_hash=HASH)
        campaign = campaigns.create_campaign(
            session,
            keitaro_id=910,
            name="Idem",
            baseline_canonical=canonical(),
            baseline_fingerprint=fingerprint(),
        )
        record = idempotency.complete(
            session,
            key=KEY,
            state=IdempotencyState.SUCCEEDED,
            result={"campaign": {"id": 910}, "status": "created"},
            campaign_id=campaign.id,
        )
        assert record.state is IdempotencyState.SUCCEEDED
        campaign_id = campaign.id

    with session_scope(session_factory) as session:
        record = idempotency.get(session, KEY)
        assert record.state is IdempotencyState.SUCCEEDED
        assert record.result == {"campaign": {"id": 910}, "status": "created"}
        assert record.campaign_id == campaign_id


def test_failed_record_is_replayed_not_reexecuted(idempotency, session_factory) -> None:
    """A failed attempt is terminal: retrying the key replays the failure
    result instead of re-running remote work, so no duplicate can appear."""
    failure = {"status": "failed", "keitaro_campaign_id": 555, "cleanup": "archived"}
    with session_scope(session_factory) as session:
        lease = idempotency.begin(session, key=KEY, request_hash=HASH)
        idempotency.complete(session, key=KEY, state=IdempotencyState.FAILED, result=failure)
        assert lease.acquired is True

    with session_scope(session_factory) as session:
        retry = idempotency.begin(session, key=KEY, request_hash=HASH)
        assert retry.acquired is False
        assert retry.record.state is IdempotencyState.FAILED
        assert retry.record.result == failure


def test_crash_leaves_in_progress_record_without_result(idempotency, session_factory) -> None:
    """After a crash the outcome is unknown; the record blocks naive re-runs."""
    with session_scope(session_factory) as session:
        lease = idempotency.begin(session, key=KEY, request_hash=HASH)
        assert lease.acquired is True
        # Simulate a crash: record committed as in_progress, never completed.

    with session_scope(session_factory) as session:
        retry = idempotency.begin(session, key=KEY, request_hash=HASH)
        assert retry.acquired is False
        assert retry.record.state is IdempotencyState.IN_PROGRESS
        assert retry.record.result is None


def test_completed_record_cannot_be_completed_again(idempotency, session_factory) -> None:
    with session_scope(session_factory) as session:
        idempotency.begin(session, key=KEY, request_hash=HASH)
        idempotency.complete(
            session, key=KEY, state=IdempotencyState.SUCCEEDED, result={"ok": True}
        )

    with session_scope(session_factory) as session:
        with pytest.raises(IdempotencyRecordStateError):
            idempotency.complete(
                session, key=KEY, state=IdempotencyState.FAILED, result={"ok": False}
            )
        session.rollback()


def test_complete_missing_record_raises(idempotency, session) -> None:
    with pytest.raises(IdempotencyRecordStateError):
        idempotency.complete(session, key="never-seen", state=IdempotencyState.SUCCEEDED)
    session.rollback()


def test_complete_into_in_progress_rejected(idempotency, session) -> None:
    idempotency.begin(session, key=KEY, request_hash=HASH)
    with pytest.raises(IdempotencyRecordStateError):
        idempotency.complete(session, key=KEY, state=IdempotencyState.IN_PROGRESS)
    session.rollback()


def test_key_uniqueness_enforced_at_database_level(idempotency, session_factory) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.db.models import IdempotencyRecord

    with session_scope(session_factory) as session:
        session.add(IdempotencyRecord(key=KEY, request_hash=HASH))
        session.flush()
        session.add(IdempotencyRecord(key=KEY, request_hash=HASH))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
