"""Campaign, draft, baseline, and synchronization-state persistence.

Repository methods take a ``Session`` and never commit: callers compose steps
into one transaction (``app.db.engine.session_scope``) so draft replacement,
baseline replacement, revision updates, and idempotency records stay atomic.

Draft replacement uses PUT semantics — the full set of draft streams and
offers is deleted and re-inserted inside the same transaction, guarded by an
optimistic ``expected_revision`` check. Baselines are replaced by appending
an immutable :class:`~app.db.models.BaselineSnapshot` row and mirroring the
current canonical/fingerprint onto the campaign row; snapshot rows are never
updated or deleted.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    BaselineSnapshot,
    BaselineSource,
    Campaign,
    OfferAssociationState,
    Stream,
    StreamOffer,
    StreamState,
    SyncState,
    utcnow,
)
from app.repositories.exceptions import (
    CampaignNotFoundError,
    InvalidSyncTransitionError,
    StaleRevisionError,
)


@dataclass(frozen=True, slots=True)
class DraftOfferInput:
    """One offer association row of a replacement draft."""

    offer_id: int
    offer_name: str
    state: OfferAssociationState
    share: int
    keitaro_association_id: int | None = None
    baseline_payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DraftStreamInput:
    """One stream row of a replacement draft, with its offer associations."""

    keitaro_id: int
    name: str
    type: str
    schema: str
    position: int
    state: StreamState
    share: int
    pinned: bool = False
    baseline_payload: dict[str, Any] = field(default_factory=dict)
    offers: tuple[DraftOfferInput, ...] = ()


# Synchronization-state transitions. ``needs_attention`` may only be left via
# a recovery fetch (needs_attention -> clean); entering it happens from
# ``syncing`` when compensation after a partial upload cannot be confirmed.
_ALLOWED_SYNC_TRANSITIONS: dict[SyncState, frozenset[SyncState]] = {
    SyncState.CLEAN: frozenset({SyncState.DIRTY, SyncState.SYNCING}),
    SyncState.DIRTY: frozenset({SyncState.CLEAN, SyncState.SYNCING}),
    SyncState.SYNCING: frozenset({SyncState.CLEAN, SyncState.DIRTY, SyncState.NEEDS_ATTENTION}),
    SyncState.NEEDS_ATTENTION: frozenset({SyncState.CLEAN}),
}


class CampaignRepository:
    def get(self, session: Session, campaign_id: uuid.UUID) -> Campaign | None:
        return session.get(Campaign, campaign_id)

    def get_with_draft(self, session: Session, campaign_id: uuid.UUID) -> Campaign | None:
        """Load a campaign with its draft streams and offers in two queries."""
        stmt = (
            sa.select(Campaign)
            .where(Campaign.id == campaign_id)
            .options(
                selectinload(Campaign.streams).selectinload(Stream.offers),
            )
        )
        return session.scalars(stmt).first()

    def list_campaigns(self, session: Session) -> list[Campaign]:
        stmt = sa.select(Campaign).order_by(Campaign.name, Campaign.keitaro_id)
        return list(session.scalars(stmt))

    def require(self, session: Session, campaign_id: uuid.UUID) -> Campaign:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise CampaignNotFoundError(campaign_id)
        return campaign

    def create_campaign(
        self,
        session: Session,
        *,
        keitaro_id: int,
        name: str,
        baseline_canonical: dict[str, Any],
        baseline_fingerprint: str,
        country_code: str | None = None,
        baseline_source: BaselineSource = BaselineSource.CREATE,
        idempotency_key: str | None = None,
        idempotency_request_hash: str | None = None,
        revision: int = 1,
        sync_state: SyncState = SyncState.CLEAN,
    ) -> Campaign:
        """Create the campaign row together with its initial baseline.

        Both rows are added pending and flush with the caller's transaction;
        the NOT NULL baseline columns make a baseline-less campaign
        unrepresentable. ``id`` is generated eagerly so dependent rows can
        reference the campaign before the first flush.
        """
        campaign = Campaign(
            id=uuid.uuid4(),
            keitaro_id=keitaro_id,
            name=name,
            country_code=country_code,
            baseline_canonical=baseline_canonical,
            baseline_fingerprint=baseline_fingerprint,
            idempotency_key=idempotency_key,
            idempotency_request_hash=idempotency_request_hash,
            revision=revision,
            sync_state=sync_state,
        )
        session.add(campaign)
        session.add(
            BaselineSnapshot(
                campaign_id=campaign.id,
                canonical=baseline_canonical,
                fingerprint=baseline_fingerprint,
                source=baseline_source,
            )
        )
        return campaign

    # Draft -----------------------------------------------------------------

    def replace_draft(
        self,
        session: Session,
        *,
        campaign_id: uuid.UUID,
        expected_revision: int,
        streams: Sequence[DraftStreamInput],
        sync_state: SyncState | None = None,
    ) -> Campaign:
        """Atomically replace the draft and bump the local revision.

        Raises :class:`StaleRevisionError` before writing anything when the
        stored revision does not match ``expected_revision``.
        """
        campaign = self.require(session, campaign_id)
        if campaign.revision != expected_revision:
            raise StaleRevisionError(campaign_id, expected_revision, campaign.revision)

        stream_ids = sa.select(Stream.id).where(Stream.campaign_id == campaign_id)
        session.execute(sa.delete(StreamOffer).where(StreamOffer.stream_id.in_(stream_ids)))
        session.execute(sa.delete(Stream).where(Stream.campaign_id == campaign_id))

        for stream_input in streams:
            stream = Stream(
                campaign_id=campaign_id,
                keitaro_id=stream_input.keitaro_id,
                name=stream_input.name,
                type=stream_input.type,
                schema_=stream_input.schema,
                position=stream_input.position,
                state=stream_input.state,
                share=stream_input.share,
                pinned=stream_input.pinned,
                baseline_payload=stream_input.baseline_payload,
            )
            session.add(stream)
            session.flush()
            for offer_input in stream_input.offers:
                session.add(
                    StreamOffer(
                        stream_id=stream.id,
                        keitaro_association_id=offer_input.keitaro_association_id,
                        offer_id=offer_input.offer_id,
                        offer_name=offer_input.offer_name,
                        state=offer_input.state,
                        share=offer_input.share,
                        baseline_payload=offer_input.baseline_payload,
                    )
                )

        campaign.revision = campaign.revision + 1
        if sync_state is not None:
            campaign.sync_state = sync_state
        session.flush()
        return campaign

    # Baseline --------------------------------------------------------------

    def replace_baseline(
        self,
        session: Session,
        *,
        campaign_id: uuid.UUID,
        canonical: dict[str, Any],
        fingerprint: str,
        source: BaselineSource,
    ) -> BaselineSnapshot:
        """Install a new baseline: append an immutable snapshot row and point
        the campaign's current baseline columns at it."""
        campaign = self.require(session, campaign_id)
        snapshot = BaselineSnapshot(
            campaign_id=campaign.id,
            canonical=canonical,
            fingerprint=fingerprint,
            source=source,
        )
        session.add(snapshot)
        campaign.baseline_canonical = canonical
        campaign.baseline_fingerprint = fingerprint
        session.flush()
        return snapshot

    # Synchronization metadata ----------------------------------------------

    def transition_sync_state(
        self, session: Session, *, campaign_id: uuid.UUID, to_state: SyncState
    ) -> Campaign:
        campaign = self.require(session, campaign_id)
        allowed = _ALLOWED_SYNC_TRANSITIONS[campaign.sync_state]
        if to_state not in allowed:
            raise InvalidSyncTransitionError(campaign_id, campaign.sync_state, to_state)
        campaign.sync_state = to_state
        session.flush()
        return campaign

    def mark_fetched(
        self, session: Session, campaign_id: uuid.UUID, when: datetime | None = None
    ) -> Campaign:
        campaign = self.require(session, campaign_id)
        campaign.last_fetch_at = when or utcnow()
        session.flush()
        return campaign

    def mark_uploaded(
        self, session: Session, campaign_id: uuid.UUID, when: datetime | None = None
    ) -> Campaign:
        campaign = self.require(session, campaign_id)
        campaign.last_upload_at = when or utcnow()
        session.flush()
        return campaign
