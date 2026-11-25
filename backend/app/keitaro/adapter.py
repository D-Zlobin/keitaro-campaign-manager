"""Typed Keitaro adapter interface (plan stage 3).

Application services depend on this protocol, not on the HTTP client, so
they can run against any conforming implementation. Endpoint and payload
knowledge stays in the ``app.keitaro`` package.
"""

from __future__ import annotations

from typing import Protocol

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


class KeitaroAdapter(Protocol):
    # Safe reads -----------------------------------------------------------
    def get_campaign(self, campaign_id: int) -> KeitaroCampaign: ...

    def get_campaign_streams(self, campaign_id: int) -> list[KeitaroStream]: ...

    def get_stream(self, stream_id: int) -> KeitaroStream: ...

    def list_offers(self) -> list[KeitaroOffer]: ...

    def get_offer(self, offer_id: int) -> KeitaroOffer: ...

    def list_domains(self) -> list[KeitaroDomain]: ...

    def list_groups(self, group_type: str = "campaigns") -> list[KeitaroGroup]: ...

    def list_traffic_sources(self) -> list[KeitaroTrafficSource]: ...

    # Writes (never retried automatically) --------------------------------
    def create_campaign(self, payload: CampaignCreatePayload) -> KeitaroCampaign: ...

    def archive_campaign(self, campaign_id: int) -> None: ...

    def create_stream(self, payload: StreamCreatePayload) -> KeitaroStream: ...

    def update_stream(self, stream_id: int, payload: StreamUpdatePayload) -> KeitaroStream: ...

    def disable_stream(self, stream_id: int) -> None: ...

    def enable_stream(self, stream_id: int) -> None: ...
