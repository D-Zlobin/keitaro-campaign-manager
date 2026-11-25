"""Durable creation-idempotency records (spec section 6.4, plan stage 1).

A record is inserted ``in_progress`` before any remote work starts and is
completed exactly once with the original success or failure result. Replaying
a completed record — including a failed one — returns the recorded result and
never re-executes remote work, so a retry cannot create an unintended
duplicate. A record left ``in_progress`` (crash) signals an unknown outcome;
callers must not silently re-run it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db.models import IdempotencyRecord, IdempotencyState


class IdempotencyPayloadConflict(Exception):
    """The key was already used with a different request hash."""

    def __init__(self, key: str) -> None:
        super().__init__(f"Idempotency key {key!r} was used with a different payload")
        self.key = key


class IdempotencyRecordStateError(Exception):
    """An illegal idempotency-record transition was attempted."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"Idempotency record {key!r}: {message}")
        self.key = key


@dataclass(frozen=True, slots=True)
class IdempotencyLease:
    """Outcome of claiming an idempotency key.

    ``acquired`` is True when this call created the in-progress record and
    owns the attempt. When False, ``record`` is the pre-existing record: the
    caller must replay its result (completed) or refuse an ambiguous re-run
    (still in progress, e.g. after a crash).
    """

    record: IdempotencyRecord
    acquired: bool


class IdempotencyRepository:
    def get(self, session: Session, key: str) -> IdempotencyRecord | None:
        return session.scalars(
            sa.select(IdempotencyRecord).where(IdempotencyRecord.key == key)
        ).first()

    def begin(self, session: Session, *, key: str, request_hash: str) -> IdempotencyLease:
        """Claim ``key`` for a new attempt, or return the existing record.

        The existence check keeps the common replay path free of failed
        flushes; a concurrent same-key insert still fails on the unique
        constraint and surfaces as ``IntegrityError``.
        """
        existing = self.get(session, key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyPayloadConflict(key)
            return IdempotencyLease(record=existing, acquired=False)
        record = IdempotencyRecord(key=key, request_hash=request_hash)
        session.add(record)
        session.flush()
        return IdempotencyLease(record=record, acquired=True)

    def complete(
        self,
        session: Session,
        *,
        key: str,
        state: IdempotencyState,
        result: dict[str, Any] | None = None,
        campaign_id: uuid.UUID | None = None,
    ) -> IdempotencyRecord:
        """Record the original success/failure result exactly once."""
        if state is IdempotencyState.IN_PROGRESS:
            raise IdempotencyRecordStateError(key, "cannot complete into in_progress")
        record = self.get(session, key)
        if record is None:
            raise IdempotencyRecordStateError(key, "no record to complete")
        if record.state is not IdempotencyState.IN_PROGRESS:
            raise IdempotencyRecordStateError(
                key, f"record already completed as {record.state.value}"
            )
        record.state = state
        record.result = result
        record.campaign_id = campaign_id
        session.flush()
        return record
