"""Campaign creation, listing, import, and operator-initiated fetch (stage 4).

Creation (spec section 6.4) runs as an idempotent, compensating sequence:

1. Local request validation (ISO country, payload construction) happens
   before any record or remote call, so malformed input never creates state.
2. The idempotency record is inserted ``in_progress`` and committed before
   the first remote call; a crash afterwards leaves the outcome explicitly
   unknown (HTTP 409 on retry) instead of silently re-running.
3. Configured domain/group/traffic source and offer availability are
   verified with safe reads before any write.
4. Writes run in the required order — campaign, Google stream, offer
   stream, final fetch — and any failure stops the sequence, attempts a
   compensating campaign archive, and records the failure result. Replaying
   the key returns the recorded result (success or failure) without new
   remote work; a different payload with the same key is a 409.

Import and fetch share the fetch normalization pipeline: remote state is
normalized into draft input, the baseline stores the raw canonical document
and fingerprint, and pins are cleared. A dirty, ``syncing``, or
``needs_attention`` draft requires an explicit confirmation flag before fetch
discards it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.schemas import (
    CampaignCreatedResponse,
    CampaignFetchResponse,
    CampaignListResponse,
    CampaignSummary,
)
from app.config import Settings
from app.db.engine import session_scope
from app.db.models import BaselineSource, Campaign, IdempotencyState, SyncState
from app.domain.canonical import (
    build_canonical_document,
    canonical_json,
    fingerprint_canonical,
)
from app.domain.geos import is_valid_country_code
from app.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    RuleViolationError,
    UpstreamError,
    UpstreamTimeoutError,
    error_body,
)
from app.keitaro.adapter import KeitaroAdapter
from app.keitaro.client import (
    KeitaroClientError,
    KeitaroInvalidResponseError,
    KeitaroResponseError,
    KeitaroTimeoutError,
)
from app.keitaro.models import KeitaroOffer
from app.keitaro.normalization import NormalizedFetch, normalize_fetch
from app.keitaro.payloads import (
    CampaignCreatePayload,
    StreamCreatePayload,
    build_campaign_create_payload,
    build_google_stream_payload,
    build_offer_stream_payload,
)
from app.repositories.campaigns import (
    CampaignRepository,
    DraftOfferInput,
    DraftStreamInput,
)
from app.repositories.idempotency import IdempotencyRepository
from app.services.locks import KeyedLock

logger = logging.getLogger(__name__)

CAMPAIGN_STATE_DELETED = "deleted"
DOMAIN_STATE_ACTIVE = "active"
TRAFFIC_SOURCE_STATE_ACTIVE = "active"

# Placeholder campaign_id used to validate stream payloads before the remote
# campaign exists; replaced with the real ID (a plain int field) afterwards.
_PLACEHOLDER_CAMPAIGN_ID = 1

_CLEANUP_NOT_REQUIRED = "not_required"
_CLEANUP_ARCHIVED = "archived"
_CLEANUP_ARCHIVE_FAILED = "archive_failed"
_CLEANUP_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CreateCampaignCommand:
    """Normalized, locally validated creation input."""

    name: str
    country_code: str
    offer_id: int


@dataclass(frozen=True, slots=True)
class StoredResponse:
    """An HTTP result that is replayable from the idempotency record."""

    status_code: int
    body: dict[str, Any]


def _campaign_summary(campaign: Campaign) -> CampaignSummary:
    return CampaignSummary(
        id=campaign.id,
        keitaro_id=campaign.keitaro_id,
        name=campaign.name,
        country_code=campaign.country_code,
        sync_state=campaign.sync_state.value,
        dirty=campaign.sync_state is SyncState.DIRTY,
        revision=campaign.revision,
        last_fetch_at=campaign.last_fetch_at,
        last_upload_at=campaign.last_upload_at,
        created_at=campaign.created_at,
    )


def _draft_inputs(normalized: NormalizedFetch) -> list[DraftStreamInput]:
    """Map normalized remote state to replacement draft rows (pins cleared)."""
    return [
        DraftStreamInput(
            keitaro_id=stream.keitaro_id,
            name=stream.name,
            type=stream.type,
            schema=stream.schema,
            position=stream.position,
            state=stream.state,
            share=stream.share,
            baseline_payload=stream.baseline_payload,
            offers=tuple(
                DraftOfferInput(
                    offer_id=offer.offer_id,
                    offer_name=offer.offer_name,
                    state=offer.state,
                    share=offer.share,
                    keitaro_association_id=offer.keitaro_association_id,
                    pinned=False,
                    baseline_payload=offer.baseline_payload,
                )
                for offer in stream.offers
            ),
        )
        for stream in normalized.streams
    ]


def _request_hash(command: CreateCampaignCommand) -> str:
    payload = {
        "country_code": command.country_code,
        "name": command.name,
        "offer_id": command.offer_id,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _upstream_response_message(exc: KeitaroResponseError) -> str | None:
    try:
        body = json.loads(exc.snippet)
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    for key in ("error", "message"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _upstream_error(exc: KeitaroClientError) -> AppError:
    if isinstance(exc, KeitaroTimeoutError):
        return UpstreamTimeoutError("Keitaro did not respond in time. Try again.")
    if isinstance(exc, KeitaroResponseError):
        message = f"Keitaro rejected the request (HTTP {exc.status_code})."
        response_message = _upstream_response_message(exc)
        if response_message:
            return UpstreamError(
                f"{message} {response_message}",
                details={"upstream_message": response_message},
            )
        return UpstreamError(message)
    if isinstance(exc, KeitaroInvalidResponseError):
        return UpstreamError("Keitaro returned an unexpected response.")
    return UpstreamError("Keitaro could not be reached.")


def _read_error(exc: KeitaroClientError, *, not_found_message: str) -> AppError:
    if isinstance(exc, KeitaroResponseError) and exc.status_code == 404:
        return NotFoundError(not_found_message)
    return _upstream_error(exc)


def validate_offer_available(keitaro: KeitaroAdapter, offer_id: int) -> KeitaroOffer:
    """Require an existing, non-deleted Keitaro offer (spec sections 6.2, 6.8).

    Shared by campaign creation and by draft offer additions so a resolved
    autocomplete ID is always re-validated against Keitaro before it becomes
    part of persisted state.
    """
    try:
        offer = keitaro.get_offer(offer_id)
    except KeitaroResponseError as exc:
        if exc.status_code == 404:
            raise RuleViolationError(
                f"Offer {offer_id} does not exist in Keitaro.",
                code="offer_unavailable",
                details={"offer_id": offer_id},
            ) from exc
        raise
    if offer.is_deleted:
        raise RuleViolationError(
            f"Offer {offer_id} is deleted in Keitaro.",
            code="offer_unavailable",
            details={"offer_id": offer_id},
        )
    return offer


class CampaignService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        keitaro: KeitaroAdapter,
        settings: Settings,
        campaigns: CampaignRepository | None = None,
        idempotency: IdempotencyRepository | None = None,
        creation_locks: KeyedLock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._keitaro = keitaro
        self._settings = settings
        self._campaigns = campaigns or CampaignRepository()
        self._idempotency = idempotency or IdempotencyRepository()
        self._creation_locks = creation_locks or KeyedLock()

    # Listing ----------------------------------------------------------------

    def list_campaigns(self) -> CampaignListResponse:
        with session_scope(self._session_factory) as session:
            campaigns = self._campaigns.list_campaigns(session)
            return CampaignListResponse(campaigns=[_campaign_summary(c) for c in campaigns])

    # Creation ---------------------------------------------------------------

    def create_campaign(
        self, command: CreateCampaignCommand, *, idempotency_key: str
    ) -> StoredResponse:
        """Create a campaign remotely and locally, idempotently.

        Returns a :class:`StoredResponse` for every outcome that is recorded
        in the idempotency record (fresh success/failure and replays alike);
        raises :class:`AppError` for pre-record validation and key conflicts.
        """
        if not is_valid_country_code(command.country_code):
            raise RuleViolationError(
                f"Country code {command.country_code!r} is not an assigned "
                "ISO 3166-1 alpha-2 code.",
                code="invalid_country_code",
                details={"country_code": command.country_code},
            )
        settings = self._settings
        campaign_payload = build_campaign_create_payload(
            name=command.name,
            group_id=settings.keitaro_group_id,
            traffic_source_id=settings.keitaro_traffic_source_id,
            domain_id=settings.keitaro_domain_id,
        )
        google_template = build_google_stream_payload(
            campaign_id=_PLACEHOLDER_CAMPAIGN_ID,
            country_code=command.country_code,
            redirect_url=settings.google_redirect_url,
        )
        offer_template = build_offer_stream_payload(
            campaign_id=_PLACEHOLDER_CAMPAIGN_ID,
            offer_id=command.offer_id,
            country_code=command.country_code,
        )
        request_hash = _request_hash(command)

        with self._creation_locks.acquire(idempotency_key):
            replay = self._begin_idempotency(idempotency_key, request_hash)
            if replay is not None:
                return replay
            return self._execute_creation(
                command=command,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                campaign_payload=campaign_payload,
                google_template=google_template,
                offer_template=offer_template,
            )

    def _begin_idempotency(self, key: str, request_hash: str) -> StoredResponse | None:
        """Commit the in-progress record, or resolve why no work may run.

        Returns the recorded response for a completed record (replay) and
        ``None`` when this call owns the attempt. A record stuck in progress
        means a previous attempt crashed; its outcome is unknown, so it is
        refused instead of silently re-run.
        """
        try:
            with session_scope(self._session_factory) as session:
                lease = self._idempotency.begin(session, key=key, request_hash=request_hash)
                if lease.acquired:
                    return None
                record = lease.record
                if record.state is IdempotencyState.IN_PROGRESS:
                    raise ConflictError(
                        "A previous creation attempt with this Idempotency-Key has an "
                        "unknown outcome. Check Keitaro for a created campaign before "
                        "retrying with a new key.",
                        code="idempotency_in_progress",
                    )
                result = record.result or {}
                return StoredResponse(
                    status_code=int(result["status_code"]), body=dict(result["body"])
                )
        except IntegrityError as exc:
            # Backstop for a same-key insert racing past the in-process lock.
            raise ConflictError(
                "Another creation attempt with this Idempotency-Key is in progress.",
                code="idempotency_in_progress",
            ) from exc

    def _execute_creation(
        self,
        *,
        command: CreateCampaignCommand,
        idempotency_key: str,
        request_hash: str,
        campaign_payload: CampaignCreatePayload,
        google_template: StreamCreatePayload,
        offer_template: StreamCreatePayload,
    ) -> StoredResponse:
        keitaro_campaign_id: int | None = None
        step = "validate"
        try:
            self._validate_remote_configuration()
            offer = self._validate_offer(command.offer_id)
            step = "create_campaign"
            created = self._keitaro.create_campaign(campaign_payload)
            keitaro_campaign_id = created.id
            step = "create_streams"
            google_stream = self._keitaro.create_stream(
                google_template.model_copy(update={"campaign_id": keitaro_campaign_id})
            )
            offer_stream = self._keitaro.create_stream(
                offer_template.model_copy(update={"campaign_id": keitaro_campaign_id})
            )
            step = "fetch_created"
            final_campaign = self._keitaro.get_campaign(keitaro_campaign_id)
            final_streams = self._keitaro.get_campaign_streams(keitaro_campaign_id)
            missing = {google_stream.id, offer_stream.id} - {s.id for s in final_streams}
            if missing:
                raise KeitaroInvalidResponseError(
                    f"Created streams {sorted(missing)} are missing from the final fetch"
                )
            normalized = normalize_fetch(final_streams, offer_names={offer.id: offer.name})
            canonical = build_canonical_document(final_campaign, final_streams)
            fingerprint = fingerprint_canonical(canonical)
        except Exception as exc:
            return self._fail_creation(
                key=idempotency_key,
                keitaro_campaign_id=keitaro_campaign_id,
                step=step,
                exc=exc,
            )
        step = "persist"
        try:
            body = self._persist_creation(
                command=command,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                keitaro_campaign_id=keitaro_campaign_id,
                normalized=normalized,
                canonical=canonical,
                fingerprint=fingerprint,
            )
        except Exception as exc:
            return self._fail_creation(
                key=idempotency_key,
                keitaro_campaign_id=keitaro_campaign_id,
                step=step,
                exc=exc,
            )
        return StoredResponse(status_code=201, body=body)

    def _validate_remote_configuration(self) -> None:
        """Verify the configured domain/group/traffic source are usable."""
        problems: list[str] = []
        domain_id = self._settings.keitaro_domain_id
        domain = next((d for d in self._keitaro.list_domains() if d.id == domain_id), None)
        if domain is None:
            problems.append(f"configured domain {domain_id} does not exist in Keitaro")
        elif domain.state != DOMAIN_STATE_ACTIVE:
            problems.append(f"configured domain {domain_id} is not active")
        group_id = self._settings.keitaro_group_id
        if all(group.id != group_id for group in self._keitaro.list_groups()):
            problems.append(f"configured group {group_id} does not exist in Keitaro")
        source_id = self._settings.keitaro_traffic_source_id
        source = next((s for s in self._keitaro.list_traffic_sources() if s.id == source_id), None)
        if source is None:
            problems.append(f"configured traffic source {source_id} does not exist in Keitaro")
        elif source.state != TRAFFIC_SOURCE_STATE_ACTIVE:
            problems.append(f"configured traffic source {source_id} is not active")
        if problems:
            raise UpstreamError(
                "Backend Keitaro configuration is invalid: " + "; ".join(problems) + ".",
                code="configuration_invalid",
                details={"problems": problems},
            )

    def _validate_offer(self, offer_id: int) -> KeitaroOffer:
        try:
            return validate_offer_available(self._keitaro, offer_id)
        except KeitaroClientError as exc:
            raise _upstream_error(exc) from exc

    def _persist_creation(
        self,
        *,
        command: CreateCampaignCommand,
        idempotency_key: str,
        request_hash: str,
        keitaro_campaign_id: int,
        normalized: NormalizedFetch,
        canonical: dict[str, Any],
        fingerprint: str,
    ) -> dict[str, Any]:
        """Persist campaign, baseline, and draft plus the success record atomically."""
        sync_state = SyncState.DIRTY if normalized.dirty else SyncState.CLEAN
        with session_scope(self._session_factory) as session:
            campaign = self._campaigns.create_campaign(
                session,
                keitaro_id=keitaro_campaign_id,
                name=command.name,
                country_code=command.country_code,
                baseline_canonical=canonical,
                baseline_fingerprint=fingerprint,
                baseline_source=BaselineSource.CREATE,
                idempotency_key=idempotency_key,
                idempotency_request_hash=request_hash,
                notices=normalized.notices,
                revision=0,
                sync_state=sync_state,
            )
            self._campaigns.replace_draft(
                session,
                campaign_id=campaign.id,
                expected_revision=0,
                streams=_draft_inputs(normalized),
            )
            self._campaigns.mark_fetched(session, campaign.id)
            body = CampaignCreatedResponse(
                campaign=_campaign_summary(campaign), notices=list(normalized.notices)
            ).model_dump(mode="json")
            self._idempotency.complete(
                session,
                key=idempotency_key,
                state=IdempotencyState.SUCCEEDED,
                result={"status_code": 201, "body": body},
                campaign_id=campaign.id,
            )
        return body

    def _fail_creation(
        self, *, key: str, keitaro_campaign_id: int | None, step: str, exc: Exception
    ) -> StoredResponse:
        """Stop the sequence, compensate, and record the failure result.

        Compensation archives the remotely created campaign when its ID is
        known. When the create call itself failed without a response the
        outcome is ambiguous (the campaign may exist), which is reported as
        ``unknown`` rather than claimed as cleaned up.
        """
        if keitaro_campaign_id is not None:
            cleanup_status = self._archive_compensation(keitaro_campaign_id)
        elif step == "create_campaign":
            cleanup_status = _CLEANUP_UNKNOWN
        else:
            cleanup_status = _CLEANUP_NOT_REQUIRED
        if isinstance(exc, AppError):
            error = exc
        elif isinstance(exc, KeitaroClientError):
            error = _upstream_error(exc)
        else:
            logger.exception("campaign creation failed unexpectedly at step %s", step)
            error = AppError("Campaign creation failed unexpectedly.")
        details = {
            **error.details,
            "keitaro_campaign_id": keitaro_campaign_id,
            "cleanup_status": cleanup_status,
        }
        body = error_body(error.code, error.message, details)
        # A fresh transaction: the creation transaction (if any) has rolled
        # back. If this also fails the record stays in_progress, which
        # correctly blocks a silent retry with an unknown-outcome 409.
        with session_scope(self._session_factory) as session:
            self._idempotency.complete(
                session,
                key=key,
                state=IdempotencyState.FAILED,
                result={"status_code": error.status_code, "body": body},
            )
        return StoredResponse(status_code=error.status_code, body=body)

    def _archive_compensation(self, keitaro_campaign_id: int) -> str:
        try:
            self._keitaro.archive_campaign(keitaro_campaign_id)
        except Exception:
            logger.exception(
                "compensating archive of Keitaro campaign %s failed", keitaro_campaign_id
            )
            return _CLEANUP_ARCHIVE_FAILED
        return _CLEANUP_ARCHIVED

    # Import -----------------------------------------------------------------

    def import_campaign(self, keitaro_campaign_id: int) -> CampaignCreatedResponse:
        """Import an existing Keitaro campaign as a clean local baseline."""
        with session_scope(self._session_factory) as session:
            existing = self._campaigns.get_by_keitaro_id(session, keitaro_campaign_id)
            if existing is not None:
                raise ConflictError(
                    "This Keitaro campaign is already imported.",
                    code="campaign_exists",
                    details={"campaign_id": str(existing.id), "keitaro_id": keitaro_campaign_id},
                )
        try:
            remote_campaign = self._keitaro.get_campaign(keitaro_campaign_id)
            remote_streams = self._keitaro.get_campaign_streams(keitaro_campaign_id)
            offer_names = self._resolve_offer_names(
                {offer.offer_id for stream in remote_streams for offer in stream.offers}
            )
        except KeitaroClientError as exc:
            raise _read_error(exc, not_found_message="Campaign was not found in Keitaro.") from exc
        if remote_campaign.state == CAMPAIGN_STATE_DELETED:
            raise NotFoundError(
                "Campaign was not found in Keitaro; it is archived.",
                details={"keitaro_id": keitaro_campaign_id},
            )
        normalized = normalize_fetch(remote_streams, offer_names=offer_names)
        canonical = build_canonical_document(remote_campaign, remote_streams)
        fingerprint = fingerprint_canonical(canonical)
        sync_state = SyncState.DIRTY if normalized.dirty else SyncState.CLEAN
        try:
            with session_scope(self._session_factory) as session:
                campaign = self._campaigns.create_campaign(
                    session,
                    keitaro_id=keitaro_campaign_id,
                    name=remote_campaign.name or f"Campaign {keitaro_campaign_id}",
                    country_code=None,
                    baseline_canonical=canonical,
                    baseline_fingerprint=fingerprint,
                    baseline_source=BaselineSource.FETCH,
                    notices=normalized.notices,
                    revision=0,
                    sync_state=sync_state,
                )
                self._campaigns.replace_draft(
                    session,
                    campaign_id=campaign.id,
                    expected_revision=0,
                    streams=_draft_inputs(normalized),
                )
                self._campaigns.mark_fetched(session, campaign.id)
                return CampaignCreatedResponse(
                    campaign=_campaign_summary(campaign), notices=list(normalized.notices)
                )
        except IntegrityError as exc:
            # A concurrent import of the same Keitaro campaign lost the race.
            raise ConflictError(
                "This Keitaro campaign is already imported.",
                code="campaign_exists",
                details={"keitaro_id": keitaro_campaign_id},
            ) from exc

    # Fetch ------------------------------------------------------------------

    def fetch_campaign(
        self, campaign_id: uuid.UUID, *, confirm_discard: bool
    ) -> CampaignFetchResponse:
        """Replace baseline and draft with the current remote state.

        Allowed for a clean draft; a dirty, ``syncing``, or
        ``needs_attention`` draft is discarded only with ``confirm_discard``.
        Pins are cleared and the local revision increments. Callers serialize
        per campaign (the API holds the campaign lock), so confirmed recovery
        from ``syncing`` cannot overlap a live upload. A stale revision here
        means another writer bypassed that lock and surfaces as a 409.
        """
        with session_scope(self._session_factory) as session:
            campaign = self._campaigns.get_with_draft(session, campaign_id)
            if campaign is None:
                raise NotFoundError(
                    "Campaign not found.", details={"campaign_id": str(campaign_id)}
                )
            if campaign.sync_state is SyncState.SYNCING and not confirm_discard:
                raise ConflictError(
                    "A synchronization operation is already in progress for this campaign.",
                    code="operation_in_progress",
                )
            if (
                campaign.sync_state in (SyncState.DIRTY, SyncState.NEEDS_ATTENTION)
                and not confirm_discard
            ):
                raise ConflictError(
                    "The draft has unsynchronized changes. Confirm discard to reload from Keitaro.",
                    code="dirty_draft",
                    details={
                        "revision": campaign.revision,
                        "sync_state": campaign.sync_state.value,
                    },
                )
            keitaro_id = campaign.keitaro_id
            known_stream_ids = {stream.keitaro_id for stream in campaign.streams}
            expected_revision = campaign.revision
        try:
            remote_campaign = self._keitaro.get_campaign(keitaro_id)
            remote_streams = self._keitaro.get_campaign_streams(keitaro_id)
            offer_names = self._resolve_offer_names(
                {offer.offer_id for stream in remote_streams for offer in stream.offers}
            )
        except KeitaroClientError as exc:
            raise _read_error(
                exc,
                not_found_message=(
                    "Campaign was not found in Keitaro; it may have been archived or deleted."
                ),
            ) from exc
        if remote_campaign.state == CAMPAIGN_STATE_DELETED:
            raise NotFoundError(
                "Campaign was not found in Keitaro; it may have been archived or deleted.",
                details={"keitaro_id": keitaro_id},
            )
        normalized = normalize_fetch(
            remote_streams, known_stream_ids=known_stream_ids, offer_names=offer_names
        )
        canonical = build_canonical_document(remote_campaign, remote_streams)
        fingerprint = fingerprint_canonical(canonical)
        sync_state = SyncState.DIRTY if normalized.dirty else SyncState.CLEAN
        with session_scope(self._session_factory) as session:
            self._campaigns.require(session, campaign_id)
            self._campaigns.replace_baseline(
                session,
                campaign_id=campaign_id,
                canonical=canonical,
                fingerprint=fingerprint,
                source=BaselineSource.FETCH,
            )
            campaign = self._campaigns.replace_draft(
                session,
                campaign_id=campaign_id,
                expected_revision=expected_revision,
                streams=_draft_inputs(normalized),
                sync_state=sync_state,
            )
            self._campaigns.set_notices(session, campaign_id, normalized.notices)
            self._campaigns.mark_fetched(session, campaign_id)
            return CampaignFetchResponse(
                campaign=_campaign_summary(campaign), notices=list(normalized.notices)
            )

    # Shared helpers ---------------------------------------------------------

    def _resolve_offer_names(self, offer_ids: Collection[int]) -> dict[int, str]:
        """Resolve display names for referenced offers via safe reads.

        Offers deleted or purged remotely keep the normalization fallback
        name (``Offer {id}``); other upstream failures abort the operation.
        """
        resolved: dict[int, str] = {}
        for offer_id in sorted(offer_ids):
            try:
                offer = self._keitaro.get_offer(offer_id)
            except KeitaroResponseError as exc:
                if exc.status_code == 404:
                    continue
                raise
            if offer.name:
                resolved[offer_id] = offer.name
        return resolved
