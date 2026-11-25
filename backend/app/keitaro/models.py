"""Validated internal models for Keitaro Admin API responses.

Field sets verified against the official OpenAPI reference
(admin-api.docs.keitaro.io) and read-only inspection of the target instance
(keitaro-for-tests.etolegko.com) on 2026-11-25. Unknown fields are preserved
(``extra="allow"``) so fingerprints and partial updates can use them later,
but only the typed fields below are treated as managed.

Notable target-instance observations:
- Streams expose ``created_at``; ``updated_at`` may be ``null``.
- Offer associations are ``{id, offer_id, state, share}`` with integer shares.
- Offer ``country`` values are uppercase ISO 3166-1 alpha-2 codes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RemoteModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class KeitaroFilter(RemoteModel):
    id: int | None = None
    name: str
    mode: str
    payload: Any = None


class KeitaroOfferAssociation(RemoteModel):
    id: int
    offer_id: int
    state: str
    share: int | float
    stream_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class KeitaroStream(RemoteModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: int
    campaign_id: int
    name: str = ""
    type: str
    schema_: str = Field(alias="schema")
    state: str
    position: int
    weight: int | float
    action_type: str | None = None
    action_payload: Any = None
    action_options: dict[str, Any] | None = None
    filters: list[KeitaroFilter] = Field(default_factory=list)
    offers: list[KeitaroOfferAssociation] = Field(default_factory=list)
    landings: list[Any] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.state == "active"


class KeitaroCampaign(RemoteModel):
    id: int
    name: str = ""
    alias: str | None = None
    type: str | None = None
    state: str | None = None
    group_id: int | None = None
    traffic_source_id: int | None = None
    domain_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class KeitaroOffer(RemoteModel):
    id: int
    name: str = ""
    state: str | None = None
    country: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def is_deleted(self) -> bool:
        return self.state == "deleted"


class KeitaroDomain(RemoteModel):
    id: int
    name: str = ""
    state: str | None = None


class KeitaroGroup(RemoteModel):
    id: int
    name: str = ""
    type: str | None = None


class KeitaroTrafficSource(RemoteModel):
    id: int
    name: str = ""
    state: str | None = None
