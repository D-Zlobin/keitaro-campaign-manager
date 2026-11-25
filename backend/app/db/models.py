"""SQLAlchemy models for local campaign state (spec section 10, plan stage 1).

Draft state (``streams`` / ``stream_offers``) is mutable and replaced
transactionally. Baseline state is stored separately: campaigns carry the
current canonical baseline snapshot and fingerprint, while
``baseline_snapshots`` is an append-only, immutable history of every baseline
ever installed. Creation idempotency lives in ``idempotency_records`` and in
the campaign's own key/request-hash columns.

All tables use local UUID primary keys. Keitaro IDs are plain integers with
uniqueness scoped as required: campaign Keitaro IDs are globally unique,
stream Keitaro IDs are unique per campaign, and a stream holds at most one
local association per offer ID so re-adding reactivates instead of
duplicating.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

SHA256_HEX_LENGTH = 64


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    """Persist str-enum values (e.g. ``active``), not member names."""
    return [member.value for member in enum_class]


def _enum_sql_values(enum_class: type[enum.Enum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_class)


def utcnow() -> datetime:
    return datetime.now(UTC)


class StreamState(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class OfferAssociationState(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class SyncState(enum.StrEnum):
    """Synchronization state of one campaign; ``needs_attention`` persists
    across restarts and blocks further uploads until a recovery fetch."""

    CLEAN = "clean"
    DIRTY = "dirty"
    SYNCING = "syncing"
    NEEDS_ATTENTION = "needs_attention"


class BaselineSource(enum.StrEnum):
    CREATE = "create"
    FETCH = "fetch"
    UPLOAD = "upload"


class IdempotencyState(enum.StrEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Base(DeclarativeBase):
    pass


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(
            f"sync_state IN ({_enum_sql_values(SyncState)})", name="ck_campaigns_sync_state"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    keitaro_id: Mapped[int] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(255))
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)

    revision: Mapped[int] = mapped_column(Integer, default=1)
    sync_state: Mapped[SyncState] = mapped_column(
        SAEnum(
            SyncState,
            native_enum=False,
            validate_strings=True,
            length=20,
            values_callable=_enum_values,
        ),
        default=SyncState.CLEAN,
    )
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_upload_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    baseline_canonical: Mapped[dict[str, Any]] = mapped_column(JSON)
    baseline_fingerprint: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH))

    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    idempotency_request_hash: Mapped[str | None] = mapped_column(
        String(SHA256_HEX_LENGTH), nullable=True
    )

    streams: Mapped[list[Stream]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        order_by="Stream.position, Stream.keitaro_id",
    )
    baseline_snapshots: Mapped[list[BaselineSnapshot]] = relationship(
        cascade="all, delete-orphan",
        order_by="BaselineSnapshot.created_at",
    )


class Stream(TimestampMixin, Base):
    __tablename__ = "streams"
    __table_args__ = (
        UniqueConstraint("campaign_id", "keitaro_id", name="uq_streams_campaign_keitaro_id"),
        CheckConstraint("share >= 0 AND share <= 100", name="ck_streams_share_range"),
        CheckConstraint("position >= 0", name="ck_streams_position_non_negative"),
        CheckConstraint("keitaro_id > 0", name="ck_streams_keitaro_id_positive"),
        CheckConstraint(f"state IN ({_enum_sql_values(StreamState)})", name="ck_streams_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    keitaro_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255), default="")
    type: Mapped[str] = mapped_column(String(32))
    # Keitaro names the field "schema"; the trailing underscore avoids the
    # SQLAlchemy declarative attribute of the same name.
    schema_: Mapped[str] = mapped_column("schema", String(32))
    position: Mapped[int] = mapped_column(Integer)
    state: Mapped[StreamState] = mapped_column(
        SAEnum(
            StreamState,
            native_enum=False,
            validate_strings=True,
            length=16,
            values_callable=_enum_values,
        )
    )
    share: Mapped[int] = mapped_column(Integer)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    baseline_payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    campaign: Mapped[Campaign] = relationship(back_populates="streams")
    offers: Mapped[list[StreamOffer]] = relationship(
        back_populates="stream",
        cascade="all, delete-orphan",
        order_by="StreamOffer.keitaro_association_id, StreamOffer.offer_id",
    )


class StreamOffer(TimestampMixin, Base):
    __tablename__ = "stream_offers"
    __table_args__ = (
        UniqueConstraint("stream_id", "offer_id", name="uq_stream_offers_stream_offer"),
        CheckConstraint("share >= 0 AND share <= 100", name="ck_stream_offers_share_range"),
        CheckConstraint("offer_id > 0", name="ck_stream_offers_offer_id_positive"),
        CheckConstraint(
            f"state IN ({_enum_sql_values(OfferAssociationState)})",
            name="ck_stream_offers_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stream_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("streams.id", ondelete="CASCADE"), index=True
    )
    # Nullable: a locally added association has no Keitaro ID until upload.
    keitaro_association_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    offer_id: Mapped[int] = mapped_column(Integer)
    offer_name: Mapped[str] = mapped_column(String(255))
    state: Mapped[OfferAssociationState] = mapped_column(
        SAEnum(
            OfferAssociationState,
            native_enum=False,
            validate_strings=True,
            length=16,
            values_callable=_enum_values,
        )
    )
    share: Mapped[int] = mapped_column(Integer)
    baseline_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    stream: Mapped[Stream] = relationship(back_populates="offers")


class BaselineSnapshot(TimestampMixin, Base):
    """Append-only, immutable history of campaign baselines.

    Rows are inserted by :class:`app.repositories.campaigns.CampaignRepository`
    and are never updated or deleted; the current baseline is mirrored on the
    campaign row (``baseline_canonical`` / ``baseline_fingerprint``).
    """

    __tablename__ = "baseline_snapshots"
    __table_args__ = (
        CheckConstraint(
            f"source IN ({_enum_sql_values(BaselineSource)})",
            name="ck_baseline_snapshots_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    canonical: Mapped[dict[str, Any]] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH))
    source: Mapped[BaselineSource] = mapped_column(
        SAEnum(
            BaselineSource,
            native_enum=False,
            validate_strings=True,
            length=16,
            values_callable=_enum_values,
        )
    )


class IdempotencyRecord(TimestampMixin, Base):
    """Durable record of one creation attempt keyed by ``Idempotency-Key``.

    A record is created ``in_progress`` before any remote call and completed
    exactly once with the original success or failure result. Replaying a
    completed record — including a failed one — never re-executes the remote
    work, so a retry cannot create an unintended duplicate. A record stuck
    ``in_progress`` (e.g. after a crash) marks the outcome as unknown and must
    not be silently re-run.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint(
            f"state IN ({_enum_sql_values(IdempotencyState)})",
            name="ck_idempotency_records_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(255), unique=True)
    request_hash: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH))
    state: Mapped[IdempotencyState] = mapped_column(
        SAEnum(
            IdempotencyState,
            native_enum=False,
            validate_strings=True,
            length=16,
            values_callable=_enum_values,
        ),
        default=IdempotencyState.IN_PROGRESS,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
