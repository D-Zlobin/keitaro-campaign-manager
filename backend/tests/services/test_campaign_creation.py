"""Campaign creation: ordering, validation, idempotency, and compensation.

Covers plan stage 4: success, every failure step (before and after the
Keitaro campaign ID exists), archive success/failure, same-key replay,
same-key/different-payload conflict, concurrent duplicate prevention, and
proof that invalid input performs no remote writes.
"""

from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.api.schemas import CampaignCreateRequest
from app.db.models import (
    BaselineSnapshot,
    Campaign,
    IdempotencyRecord,
    IdempotencyState,
    Stream,
    StreamOffer,
    SyncState,
)
from app.domain.canonical import canonical_json, fingerprint_remote
from app.errors import ConflictError, RuleViolationError, UpstreamError
from app.keitaro.client import KeitaroResponseError, KeitaroTimeoutError
from app.keitaro.models import KeitaroOffer
from app.repositories.campaigns import CampaignRepository
from app.repositories.idempotency import IdempotencyPayloadConflict, IdempotencyRepository
from app.services.campaigns import CampaignService, CreateCampaignCommand
from tests.fakes import DEFAULT_OFFER_ID, FakeKeitaroAdapter

COMMAND = CreateCampaignCommand(name="Example DE", country_code="DE", offer_id=DEFAULT_OFFER_ID)

EXPECTED_CALL_ORDER = [
    "list_domains",
    "list_groups",
    "list_traffic_sources",
    "get_offer",
    "create_campaign",
    "create_stream",
    "create_stream",
    "get_campaign",
    "get_campaign_streams",
]


def test_create_success_persists_baseline_and_draft(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    outcome = service.create_campaign(COMMAND, idempotency_key="key-1")

    assert outcome.status_code == 201
    assert outcome.body["notices"] == []
    summary = outcome.body["campaign"]
    assert summary["keitaro_id"] == 500
    assert summary["name"] == "Example DE"
    assert summary["country_code"] == "DE"
    assert summary["sync_state"] == "clean"
    assert summary["dirty"] is False
    assert summary["revision"] == 1
    assert summary["last_fetch_at"] is not None

    # Remote work happened in the required order, reads before writes.
    assert fake_keitaro.calls == EXPECTED_CALL_ORDER

    # Campaign payload uses the configured domain/group/traffic source.
    (campaign_payload,) = fake_keitaro.campaign_payloads
    assert campaign_payload == {
        "name": "Example DE",
        "alias": "",
        "type": "weight",
        "state": "active",
        "group_id": 10332,
        "traffic_source_id": 836,
        "domain_id": 1813,
    }

    # Streams: Google first (accept filter + redirect), then the offer stream
    # (reject filter + nested offer at 100%), both at weight 50.
    google, offer = fake_keitaro.stream_payloads
    assert google["campaign_id"] == 500
    assert google["name"] == "Google — DE"
    assert google["schema"] == "redirect"
    assert google["type"] == "regular"
    assert google["position"] == 0
    assert google["weight"] == 50
    assert google["state"] == "active"
    assert google["action_type"] == "redirect"
    assert google["action_payload"] == "https://www.google.com/"
    assert google["filters"] == [{"name": "country", "mode": "accept", "payload": ["DE"]}]
    assert google["offers"] == []

    assert offer["campaign_id"] == 500
    assert offer["name"] == f"Offer — {DEFAULT_OFFER_ID}"
    assert offer["schema"] == "landings"
    assert offer["position"] == 1
    assert offer["weight"] == 50
    assert offer["filters"] == [{"name": "country", "mode": "reject", "payload": ["DE"]}]
    assert offer["offers"] == [{"offer_id": DEFAULT_OFFER_ID, "share": 100, "state": "active"}]

    with session_factory() as session:
        campaign = session.scalars(select(Campaign)).one()
        assert campaign.keitaro_id == 500
        assert campaign.revision == 1
        assert campaign.sync_state is SyncState.CLEAN
        assert campaign.idempotency_key == "key-1"
        expected_hash = hashlib.sha256(
            canonical_json(
                {"country_code": "DE", "name": "Example DE", "offer_id": DEFAULT_OFFER_ID}
            ).encode()
        ).hexdigest()
        assert campaign.idempotency_request_hash == expected_hash

        # The stored baseline fingerprint matches the actual remote state.
        remote_streams = fake_keitaro.get_campaign_streams(500)
        assert campaign.baseline_fingerprint == fingerprint_remote(
            fake_keitaro.campaigns[500], remote_streams
        )

        streams = session.scalars(select(Stream).order_by(Stream.position)).all()
        assert [(s.keitaro_id, s.share, s.pinned) for s in streams] == [
            (900, 50, False),
            (901, 50, False),
        ]
        assert streams[0].baseline_payload["schema"] == "redirect"
        offers = session.scalars(select(StreamOffer)).all()
        assert len(offers) == 1
        assert offers[0].offer_id == DEFAULT_OFFER_ID
        assert offers[0].offer_name == "Example Offer"
        assert offers[0].share == 100
        assert offers[0].keitaro_association_id is not None
        assert offers[0].baseline_payload["offer_id"] == DEFAULT_OFFER_ID

        snapshot = session.scalars(select(BaselineSnapshot)).one()
        assert snapshot.source.value == "create"
        assert snapshot.fingerprint == campaign.baseline_fingerprint

        record = session.scalars(select(IdempotencyRecord)).one()
        assert record.state is IdempotencyState.SUCCEEDED
        assert record.campaign_id == campaign.id
        assert record.result["status_code"] == 201


def test_replay_same_key_returns_original_result_without_new_remote_work(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    first = service.create_campaign(COMMAND, idempotency_key="key-1")
    calls_after_first = list(fake_keitaro.calls)

    second = service.create_campaign(COMMAND, idempotency_key="key-1")

    assert second.status_code == first.status_code == 201
    assert second.body == first.body
    assert fake_keitaro.calls == calls_after_first
    assert fake_keitaro.calls_of("create_campaign") == 1
    with session_factory() as session:
        assert len(session.scalars(select(Campaign)).all()) == 1


def test_same_key_with_different_payload_is_rejected(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    service.create_campaign(COMMAND, idempotency_key="key-1")
    other = CreateCampaignCommand(name="Other", country_code="DE", offer_id=DEFAULT_OFFER_ID)

    with pytest.raises(IdempotencyPayloadConflict):
        service.create_campaign(other, idempotency_key="key-1")

    assert fake_keitaro.calls_of("create_campaign") == 1


def test_failed_creation_replay_returns_recorded_failure(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("create_stream", KeitaroResponseError("create_stream", 500, "boom"))
    first = service.create_campaign(COMMAND, idempotency_key="key-1")
    calls_after_first = list(fake_keitaro.calls)

    second = service.create_campaign(COMMAND, idempotency_key="key-1")

    assert first.status_code == 502
    assert second.status_code == first.status_code
    assert second.body == first.body
    assert fake_keitaro.calls == calls_after_first  # nothing re-executed


def test_concurrent_duplicate_runs_creation_once(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.create_campaign_delay = 0.2

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(service.create_campaign, COMMAND, idempotency_key="race") for _ in range(2)
        ]
        outcomes = [future.result() for future in futures]

    assert fake_keitaro.calls_of("create_campaign") == 1
    assert outcomes[0].status_code == outcomes[1].status_code == 201
    assert outcomes[0].body == outcomes[1].body


def test_stuck_in_progress_record_is_not_silently_rerun(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    with session_factory() as session:
        IdempotencyRepository().begin(
            session,
            key="crashed",
            request_hash=hashlib.sha256(
                canonical_json(
                    {"country_code": "DE", "name": "Example DE", "offer_id": DEFAULT_OFFER_ID}
                ).encode()
            ).hexdigest(),
        )
        session.commit()

    with pytest.raises(ConflictError) as exc_info:
        service.create_campaign(COMMAND, idempotency_key="crashed")
    assert exc_info.value.code == "idempotency_in_progress"
    assert fake_keitaro.writes() == []


# Failure steps ---------------------------------------------------------------


def test_invalid_country_fails_before_any_record_or_remote_call(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    command = CreateCampaignCommand(name="Bad", country_code="ZZ", offer_id=DEFAULT_OFFER_ID)

    with pytest.raises(RuleViolationError) as exc_info:
        service.create_campaign(command, idempotency_key="key-zz")

    assert exc_info.value.code == "invalid_country_code"
    assert fake_keitaro.calls == []
    with session_factory() as session:
        assert session.scalars(select(IdempotencyRecord)).all() == []


@pytest.mark.parametrize(
    ("break_config", "problem"),
    [
        (lambda fake: fake.domains.clear(), "domain 1813 does not exist"),
        (
            lambda fake: setattr(fake.domains[0], "state", "disabled"),
            "domain 1813 is not active",
        ),
        (lambda fake: fake.groups.clear(), "group 10332 does not exist"),
        (lambda fake: fake.traffic_sources.clear(), "traffic source 836 does not exist"),
        (
            lambda fake: setattr(fake.traffic_sources[0], "state", "disabled"),
            "traffic source 836 is not active",
        ),
    ],
)
def test_invalid_configuration_causes_no_remote_writes(
    service: CampaignService,
    fake_keitaro: FakeKeitaroAdapter,
    session_factory,
    break_config,
    problem: str,
) -> None:
    break_config(fake_keitaro)

    outcome = service.create_campaign(COMMAND, idempotency_key="key-config")

    assert outcome.status_code == 502
    assert outcome.body["error"]["code"] == "configuration_invalid"
    assert problem in outcome.body["error"]["message"]
    assert outcome.body["error"]["details"]["cleanup_status"] == "not_required"
    assert outcome.body["error"]["details"]["keitaro_campaign_id"] is None
    assert fake_keitaro.writes() == []
    with session_factory() as session:
        assert session.scalars(select(Campaign)).all() == []


def test_missing_offer_causes_no_remote_writes(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.offers.clear()

    outcome = service.create_campaign(COMMAND, idempotency_key="key-offer")

    assert outcome.status_code == 422
    assert outcome.body["error"]["code"] == "offer_unavailable"
    assert outcome.body["error"]["details"]["cleanup_status"] == "not_required"
    assert fake_keitaro.writes() == []


def test_deleted_offer_causes_no_remote_writes(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.offers[DEFAULT_OFFER_ID].state = "deleted"

    outcome = service.create_campaign(COMMAND, idempotency_key="key-deleted-offer")

    assert outcome.status_code == 422
    assert outcome.body["error"]["code"] == "offer_unavailable"
    assert fake_keitaro.writes() == []


def test_create_campaign_timeout_reports_unknown_outcome(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("create_campaign", KeitaroTimeoutError("timed out"))

    outcome = service.create_campaign(COMMAND, idempotency_key="key-timeout")

    assert outcome.status_code == 504
    assert outcome.body["error"]["code"] == "upstream_timeout"
    assert outcome.body["error"]["details"]["keitaro_campaign_id"] is None
    assert outcome.body["error"]["details"]["cleanup_status"] == "unknown"
    assert "archive_campaign" not in fake_keitaro.calls


def test_google_stream_failure_archives_created_campaign(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("create_stream", KeitaroResponseError("create_stream", 500, "x"))

    outcome = service.create_campaign(COMMAND, idempotency_key="key-google")

    assert outcome.status_code == 502
    details = outcome.body["error"]["details"]
    assert details["keitaro_campaign_id"] == 500
    assert details["cleanup_status"] == "archived"
    assert fake_keitaro.campaigns[500].state == "deleted"
    # Sequence stopped immediately: no second stream, no final fetch.
    assert fake_keitaro.calls_of("create_stream") == 1
    assert "get_campaign_streams" not in fake_keitaro.calls


def test_offer_stream_failure_archives_created_campaign(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure(
        "create_stream", None, KeitaroResponseError("create_stream", 500, "x")
    )

    outcome = service.create_campaign(COMMAND, idempotency_key="key-offer-stream")

    assert outcome.status_code == 502
    assert outcome.body["error"]["details"]["cleanup_status"] == "archived"
    assert fake_keitaro.calls_of("create_stream") == 2
    assert "get_campaign_streams" not in fake_keitaro.calls


def test_archive_failure_is_reported(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("create_stream", KeitaroResponseError("create_stream", 500, "x"))
    fake_keitaro.queue_failure("archive_campaign", KeitaroTimeoutError("timed out"))

    outcome = service.create_campaign(COMMAND, idempotency_key="key-archive-fail")

    assert outcome.status_code == 502
    details = outcome.body["error"]["details"]
    assert details["keitaro_campaign_id"] == 500
    assert details["cleanup_status"] == "archive_failed"
    assert fake_keitaro.campaigns[500].state == "active"  # archive did not happen


def test_final_fetch_failure_archives_created_campaign(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("get_campaign_streams", KeitaroTimeoutError("timed out"))

    outcome = service.create_campaign(COMMAND, idempotency_key="key-fetch")

    assert outcome.status_code == 504
    assert outcome.body["error"]["details"]["cleanup_status"] == "archived"
    assert fake_keitaro.calls_of("create_stream") == 2


def test_missing_created_stream_in_final_fetch_is_integration_failure(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.hidden_stream_ids.add(901)  # offer stream vanishes from the listing

    outcome = service.create_campaign(COMMAND, idempotency_key="key-incomplete")

    assert outcome.status_code == 502
    assert outcome.body["error"]["code"] == "upstream_error"
    assert outcome.body["error"]["details"]["cleanup_status"] == "archived"


def test_local_persistence_failure_compensates_and_records(
    session_factory, fake_keitaro: FakeKeitaroAdapter, settings
) -> None:
    class BrokenRepository(CampaignRepository):
        def replace_draft(self, session, **kwargs):
            raise RuntimeError("disk full")

    service = CampaignService(
        session_factory=session_factory,
        keitaro=fake_keitaro,
        settings=settings,
        campaigns=BrokenRepository(),
    )

    outcome = service.create_campaign(COMMAND, idempotency_key="key-persist")

    assert outcome.status_code == 500
    assert outcome.body["error"]["code"] == "internal_error"
    assert outcome.body["error"]["details"]["cleanup_status"] == "archived"
    assert fake_keitaro.campaigns[500].state == "deleted"
    with session_factory() as session:
        record = session.scalars(select(IdempotencyRecord)).one()
        assert record.state is IdempotencyState.FAILED

    # The recorded failure replays instead of re-running remote work.
    replay = service.create_campaign(COMMAND, idempotency_key="key-persist")
    assert replay.status_code == 500
    assert fake_keitaro.calls_of("create_campaign") == 1


def test_validation_failure_is_recorded_and_replayed(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.offers.clear()
    first = service.create_campaign(COMMAND, idempotency_key="key-422")
    assert first.status_code == 422

    # Even after the offer appears, the same key replays the recorded result.
    fake_keitaro.offers[DEFAULT_OFFER_ID] = KeitaroOffer(
        id=DEFAULT_OFFER_ID, name="Example Offer", state="active"
    )
    second = service.create_campaign(COMMAND, idempotency_key="key-422")

    assert second.status_code == 422
    assert second.body == first.body
    assert fake_keitaro.calls_of("get_offer") == 1


def test_created_campaign_can_be_fetched_back(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter, session_factory
) -> None:
    outcome = service.create_campaign(COMMAND, idempotency_key="key-1")
    campaign_id = outcome.body["campaign"]["id"]

    fetched = service.fetch_campaign(uuid.UUID(campaign_id), confirm_discard=False)

    assert fetched.campaign.revision == 2
    assert fetched.campaign.sync_state == "clean"
    assert fetched.notices == []


def test_offer_validation_failure_maps_keitaro_rejection(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("get_offer", KeitaroResponseError("get_offer", 500, "x"))

    outcome = service.create_campaign(COMMAND, idempotency_key="key-offer-500")

    assert outcome.status_code == 502
    assert outcome.body["error"]["code"] == "upstream_error"


def test_create_request_schema_normalizes_input() -> None:
    request = CampaignCreateRequest(name="  Example DE  ", country_code="de", offer_id=7)
    assert request.name == "Example DE"
    assert request.country_code == "DE"


def test_unexpected_creation_error_is_generic(
    service: CampaignService, fake_keitaro: FakeKeitaroAdapter
) -> None:
    fake_keitaro.queue_failure("list_domains", RuntimeError("secret-internal-detail"))

    outcome = service.create_campaign(COMMAND, idempotency_key="key-bug")

    assert outcome.status_code == 500
    assert outcome.body["error"]["code"] == "internal_error"
    assert "secret-internal-detail" not in str(outcome.body)


def test_configuration_error_is_upstream_categorized() -> None:
    error = UpstreamError("bad config", code="configuration_invalid")
    assert error.status_code == 502
    assert error.code == "configuration_invalid"
