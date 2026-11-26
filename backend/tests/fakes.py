"""Programmable in-memory Keitaro adapter for service and API tests.

Implements the ``KeitaroAdapter`` protocol against in-memory state, records
every operation (in order) so tests can assert call sequences and the
absence of writes, and supports queued per-operation failures to simulate
every step of the creation sequence. IDs are assigned monotonically and
timestamps advance on every write so fingerprints change like real remote
state.

Also hosts ``FakeFiveApiClient``, the in-memory stand-in for the Five API
catalog integration (Geo list and offer autocomplete).
"""

from __future__ import annotations

import time
from typing import Any

from app.api.schemas import GeoOption, OfferOption
from app.five.errors import FiveApiError
from app.keitaro.client import KeitaroResponseError
from app.keitaro.models import (
    KeitaroCampaign,
    KeitaroDomain,
    KeitaroGroup,
    KeitaroOffer,
    KeitaroStream,
    KeitaroTrafficSource,
)
from app.keitaro.payloads import (
    CampaignCreatePayload,
    StreamCreatePayload,
    StreamUpdatePayload,
)

DEFAULT_DOMAIN_ID = 1813
DEFAULT_GROUP_ID = 10332
DEFAULT_TRAFFIC_SOURCE_ID = 836
DEFAULT_OFFER_ID = 18001


class FakeKeitaroAdapter:
    def __init__(self) -> None:
        self.domains: list[KeitaroDomain] = [
            KeitaroDomain(id=DEFAULT_DOMAIN_ID, name="kt-emulator.test", state="active")
        ]
        self.groups: list[KeitaroGroup] = [
            KeitaroGroup(id=DEFAULT_GROUP_ID, name="FORTESTS", type="campaigns")
        ]
        self.traffic_sources: list[KeitaroTrafficSource] = [
            KeitaroTrafficSource(id=DEFAULT_TRAFFIC_SOURCE_ID, name="FORTESTS", state="active")
        ]
        self.offers: dict[int, KeitaroOffer] = {
            DEFAULT_OFFER_ID: KeitaroOffer(
                id=DEFAULT_OFFER_ID, name="Example Offer", state="active"
            )
        }
        self.campaigns: dict[int, KeitaroCampaign] = {}
        self.streams: dict[int, KeitaroStream] = {}
        self.hidden_stream_ids: set[int] = set()

        self.calls: list[str] = []
        self.campaign_payloads: list[dict[str, Any]] = []
        self.stream_payloads: list[dict[str, Any]] = []
        # Queued outcomes per operation: each call pops one entry; a non-None
        # entry is raised, None lets the call succeed.
        self.fail_queue: dict[str, list[Exception | None]] = {}
        self.create_campaign_delay = 0.0

        self._next_campaign_id = 500
        self._next_stream_id = 900
        self._next_association_id = 7000
        self._next_filter_id = 100
        self._clock = 0

    # Test helpers -----------------------------------------------------------

    def calls_of(self, operation: str) -> int:
        return self.calls.count(operation)

    def writes(self) -> list[str]:
        prefixes = ("create_", "archive_", "update_", "disable_", "enable_")
        return [call for call in self.calls if call.startswith(prefixes)]

    def queue_failure(self, operation: str, *outcomes: Exception | None) -> None:
        self.fail_queue.setdefault(operation, []).extend(outcomes)

    def seed_campaign(
        self,
        *,
        keitaro_id: int | None = None,
        name: str = "Existing Campaign",
        state: str = "active",
    ) -> KeitaroCampaign:
        campaign_id = keitaro_id if keitaro_id is not None else self._next_campaign_id
        self._next_campaign_id = max(self._next_campaign_id, campaign_id + 1)
        now = self._tick()
        campaign = KeitaroCampaign(
            id=campaign_id,
            name=name,
            alias=f"alias{campaign_id}",
            type="weight",
            state=state,
            group_id=DEFAULT_GROUP_ID,
            traffic_source_id=DEFAULT_TRAFFIC_SOURCE_ID,
            domain_id=DEFAULT_DOMAIN_ID,
            created_at=now,
            updated_at=now,
        )
        self.campaigns[campaign_id] = campaign
        return campaign

    def seed_stream(
        self,
        *,
        campaign_id: int,
        name: str = "Stream",
        schema: str = "landings",
        type: str = "regular",
        state: str = "active",
        position: int = 0,
        weight: int | float = 50,
        action_type: str | None = "http",
        action_payload: Any = None,
        filters: list[dict[str, Any]] | None = None,
        offers: list[dict[str, Any]] | None = None,
        keitaro_id: int | None = None,
    ) -> KeitaroStream:
        stream_id = keitaro_id if keitaro_id is not None else self._next_stream_id
        self._next_stream_id = max(self._next_stream_id, stream_id + 1)
        now = self._tick()
        stream = KeitaroStream(
            id=stream_id,
            campaign_id=campaign_id,
            name=name,
            type=type,
            schema=schema,
            state=state,
            position=position,
            weight=weight,
            action_type=action_type,
            action_payload=action_payload,
            filters=[self._filter_payload(f) for f in (filters or [])],
            offers=[self._association_payload(stream_id, o, now) for o in (offers or [])],
            created_at=now,
            updated_at=None,
        )
        self.streams[stream_id] = stream
        return stream

    def touch_stream(self, stream_id: int, **changes: Any) -> KeitaroStream:
        """Apply remote edits and bump ``updated_at`` like a real write."""
        stream = self.streams[stream_id]
        updated = stream.model_copy(update={**changes, "updated_at": self._tick()})
        self.streams[stream_id] = updated
        return updated

    def touch_campaign(self, campaign_id: int, **changes: Any) -> KeitaroCampaign:
        campaign = self.campaigns[campaign_id]
        updated = campaign.model_copy(update={**changes, "updated_at": self._tick()})
        self.campaigns[campaign_id] = updated
        return updated

    def remove_stream(self, stream_id: int) -> None:
        """Simulate a remote stream deletion."""
        del self.streams[stream_id]

    # Internals ----------------------------------------------------------------

    def _tick(self) -> str:
        self._clock += 1
        return f"2026-11-26 12:{self._clock // 60:02d}:{self._clock % 60:02d}"

    def _maybe_fail(self, operation: str) -> None:
        queue = self.fail_queue.get(operation)
        if queue:
            outcome = queue.pop(0)
            if outcome is not None:
                raise outcome

    def _filter_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        self._next_filter_id += 1
        return {"id": self._next_filter_id, **data}

    def _association_payload(
        self, stream_id: int, data: dict[str, Any], now: str
    ) -> dict[str, Any]:
        self._next_association_id += 1
        return {
            "id": self._next_association_id,
            "stream_id": stream_id,
            "created_at": now,
            "updated_at": None,
            **data,
        }

    # Safe reads -----------------------------------------------------------------

    def get_campaign(self, campaign_id: int) -> KeitaroCampaign:
        self.calls.append("get_campaign")
        self._maybe_fail("get_campaign")
        campaign = self.campaigns.get(campaign_id)
        if campaign is None:
            raise KeitaroResponseError("get_campaign", 404, "campaign not found")
        return campaign

    def get_campaign_streams(self, campaign_id: int) -> list[KeitaroStream]:
        self.calls.append("get_campaign_streams")
        self._maybe_fail("get_campaign_streams")
        return [
            stream
            for stream in self.streams.values()
            if stream.campaign_id == campaign_id and stream.id not in self.hidden_stream_ids
        ]

    def get_stream(self, stream_id: int) -> KeitaroStream:
        self.calls.append("get_stream")
        self._maybe_fail("get_stream")
        stream = self.streams.get(stream_id)
        if stream is None:
            raise KeitaroResponseError("get_stream", 404, "stream not found")
        return stream

    def list_offers(self) -> list[KeitaroOffer]:
        self.calls.append("list_offers")
        self._maybe_fail("list_offers")
        return list(self.offers.values())

    def get_offer(self, offer_id: int) -> KeitaroOffer:
        self.calls.append("get_offer")
        self._maybe_fail("get_offer")
        offer = self.offers.get(offer_id)
        if offer is None:
            raise KeitaroResponseError("get_offer", 404, "offer not found")
        return offer

    def list_domains(self) -> list[KeitaroDomain]:
        self.calls.append("list_domains")
        self._maybe_fail("list_domains")
        return list(self.domains)

    def list_groups(self, group_type: str = "campaigns") -> list[KeitaroGroup]:
        self.calls.append("list_groups")
        self._maybe_fail("list_groups")
        return [group for group in self.groups if group.type in (None, group_type)]

    def list_traffic_sources(self) -> list[KeitaroTrafficSource]:
        self.calls.append("list_traffic_sources")
        self._maybe_fail("list_traffic_sources")
        return list(self.traffic_sources)

    # Writes (never retried automatically) ---------------------------------------

    def create_campaign(self, payload: CampaignCreatePayload) -> KeitaroCampaign:
        self.calls.append("create_campaign")
        self.campaign_payloads.append(payload.to_api_dict())
        if self.create_campaign_delay:
            time.sleep(self.create_campaign_delay)
        self._maybe_fail("create_campaign")
        campaign_id = self._next_campaign_id
        self._next_campaign_id += 1
        now = self._tick()
        campaign = KeitaroCampaign(
            id=campaign_id,
            name=payload.name,
            alias=f"alias{campaign_id}",
            type=payload.type,
            state=payload.state,
            group_id=payload.group_id,
            traffic_source_id=payload.traffic_source_id,
            domain_id=payload.domain_id,
            created_at=now,
            updated_at=now,
        )
        self.campaigns[campaign_id] = campaign
        return campaign

    def archive_campaign(self, campaign_id: int) -> None:
        self.calls.append("archive_campaign")
        self._maybe_fail("archive_campaign")
        campaign = self.campaigns.get(campaign_id)
        if campaign is None:
            raise KeitaroResponseError("archive_campaign", 404, "campaign not found")
        self.campaigns[campaign_id] = campaign.model_copy(update={"state": "deleted"})

    def create_stream(self, payload: StreamCreatePayload) -> KeitaroStream:
        self.calls.append("create_stream")
        self.stream_payloads.append(payload.to_api_dict())
        self._maybe_fail("create_stream")
        if payload.campaign_id not in self.campaigns:
            raise KeitaroResponseError("create_stream", 404, "campaign not found")
        stream_id = self._next_stream_id
        self._next_stream_id += 1
        now = self._tick()
        stream = KeitaroStream(
            id=stream_id,
            campaign_id=payload.campaign_id,
            name=payload.name,
            type=payload.type,
            schema=payload.schema_,
            state=payload.state,
            position=payload.position,
            weight=payload.weight,
            action_type=payload.action_type,
            action_payload=payload.action_payload,
            action_options=payload.action_options,
            filters=[self._filter_payload(f.to_api_dict()) for f in payload.filters],
            offers=[
                self._association_payload(stream_id, o.to_api_dict(), now) for o in payload.offers
            ],
            created_at=now,
            updated_at=None,
        )
        self.streams[stream_id] = stream
        return stream

    def update_stream(self, stream_id: int, payload: StreamUpdatePayload) -> KeitaroStream:
        self.calls.append("update_stream")
        self._maybe_fail("update_stream")
        stream = self.streams.get(stream_id)
        if stream is None:
            raise KeitaroResponseError("update_stream", 404, "stream not found")
        changes = payload.to_api_dict()
        changes.pop("filters", None)
        changes.pop("offers", None)
        return self.touch_stream(stream_id, **changes)

    def disable_stream(self, stream_id: int) -> None:
        self.calls.append("disable_stream")
        self._maybe_fail("disable_stream")
        self.touch_stream(stream_id, state="disabled")

    def enable_stream(self, stream_id: int) -> None:
        self.calls.append("enable_stream")
        self._maybe_fail("enable_stream")
        self.touch_stream(stream_id, state="active")


class FakeFiveApiClient:
    """In-memory Five API stand-in for catalog endpoint tests.

    Returns the configured Geo list and offer search results (filtered by a
    case-insensitive substring match on the name, like the real directory)
    and can be primed with a failure to simulate upstream outages.
    """

    def __init__(self) -> None:
        self.geos: list[GeoOption] = [
            GeoOption(code="DE", name="Germany"),
            GeoOption(code="CL", name="Chile"),
        ]
        self.offers: list[OfferOption] = [
            OfferOption(offer_id=DEFAULT_OFFER_ID, name="Example Offer"),
        ]
        self.failure: FiveApiError | None = None
        self.calls: list[str] = []

    def list_geos(self) -> list[GeoOption]:
        self.calls.append("list_geos")
        if self.failure is not None:
            raise self.failure
        return list(self.geos)

    def search_offers(self, search: str) -> list[OfferOption]:
        self.calls.append("search_offers")
        if self.failure is not None:
            raise self.failure
        term = search.strip().lower()
        return [offer for offer in self.offers if term in offer.name.lower()]

    def close(self) -> None:
        pass
