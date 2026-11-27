"""Keitaro Admin API request payload builders.

All endpoint and payload knowledge lives in this adapter package; services
never assemble Keitaro payloads themselves.

Verification status (plan stage 0):
- Campaign/stream create and partial-update shapes follow the official
  OpenAPI reference (admin-api.docs.keitaro.io), ``POST /campaigns``
  requiring ``alias`` + ``name`` and ``POST /streams`` requiring
  ``campaign_id``, ``schema``, ``type``, ``name`` and ``action_type``.
  The target instance rejects an empty alias despite the public reference,
  so campaign creation generates a URL-safe 12-character hexadecimal alias.
- Offer-capable streams use the public schema name ``landings`` with nested
  ``offers: [{offer_id, share, state}]``; association shares are integers.
- On the target instance every UI-created ``landings`` stream carries
  ``action_type="http"`` with null ``action_payload`` (observed 2026-11-25).
- Version-dependent, pending write-verification on a disposable campaign:
  the exact redirect representation. The documented response field is
  ``action_payload`` (string URL); some versions use ``action_options``.
  ``build_google_stream_payload`` supports both via ``redirect_via``.
- Country filter payloads use uppercase ISO 3166-1 alpha-2 codes, matching
  the offer ``country`` format observed on the target instance.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CAMPAIGN_ROTATION_WEIGHT = "weight"
CAMPAIGN_STATE_ACTIVE = "active"

STREAM_TYPE_REGULAR = "regular"
STREAM_SCHEMA_REDIRECT = "redirect"
STREAM_SCHEMA_OFFER_CAPABLE = "landings"
STREAM_STATE_ACTIVE = "active"
STREAM_STATE_DISABLED = "disabled"

# Observed on the target instance for UI-created landings streams.
LANDINGS_STREAM_ACTION_TYPE = "http"
# Documented action for redirect-schema streams; confirm on the target
# version before the first live creation.
REDIRECT_ACTION_TYPE = "redirect"

COUNTRY_FILTER_NAME = "country"
FILTER_MODE_ACCEPT = "accept"
FILTER_MODE_REJECT = "reject"

GOOGLE_STREAM_NAME_TEMPLATE = "Google — {geo}"
OFFER_STREAM_NAME_TEMPLATE = "Offer — {offer_id}"

RedirectVia = Literal["action_payload", "action_options"]


class KeitaroPayload(BaseModel):
    def to_api_dict(self) -> dict[str, Any]:
        """Full create payload: defaults included, unset optional fields dropped."""
        return self.model_dump(by_alias=True, exclude_none=True)


def _generate_campaign_alias() -> str:
    return uuid.uuid4().hex[:12]


class CampaignCreatePayload(KeitaroPayload):
    name: str
    alias: str = Field(default_factory=_generate_campaign_alias, min_length=1)
    type: str = CAMPAIGN_ROTATION_WEIGHT
    state: str = CAMPAIGN_STATE_ACTIVE
    group_id: int
    traffic_source_id: int
    domain_id: int


def build_campaign_create_payload(
    *,
    name: str,
    group_id: int,
    traffic_source_id: int,
    domain_id: int,
) -> CampaignCreatePayload:
    return CampaignCreatePayload(
        name=name,
        group_id=group_id,
        traffic_source_id=traffic_source_id,
        domain_id=domain_id,
    )


class StreamFilterPayload(KeitaroPayload):
    name: str
    mode: Literal["accept", "reject"]
    payload: list[str]


class OfferAssociationPayload(KeitaroPayload):
    offer_id: int = Field(gt=0)
    share: int = Field(ge=0, le=100)
    state: Literal["active", "disabled"] = "active"


class StreamCreatePayload(KeitaroPayload):
    model_config = ConfigDict(populate_by_name=True)

    campaign_id: int
    schema_: str = Field(alias="schema")
    type: str = STREAM_TYPE_REGULAR
    name: str
    position: int = Field(ge=0)
    weight: int = Field(ge=0, le=100)
    state: str = STREAM_STATE_ACTIVE
    action_type: str | None = None
    action_payload: str | None = None
    action_options: dict[str, Any] | None = None
    filters: list[StreamFilterPayload] = Field(default_factory=list)
    offers: list[OfferAssociationPayload] = Field(default_factory=list)


def build_google_stream_payload(
    *,
    campaign_id: int,
    country_code: str,
    redirect_url: str,
    position: int = 0,
    weight: int = 50,
    redirect_via: RedirectVia = "action_payload",
) -> StreamCreatePayload:
    geo = country_code.strip().upper()
    redirect_fields: dict[str, Any] = (
        {"action_payload": redirect_url}
        if redirect_via == "action_payload"
        else {"action_options": {"url": redirect_url}}
    )
    return StreamCreatePayload(
        campaign_id=campaign_id,
        schema_=STREAM_SCHEMA_REDIRECT,
        name=GOOGLE_STREAM_NAME_TEMPLATE.format(geo=geo),
        position=position,
        weight=weight,
        action_type=REDIRECT_ACTION_TYPE,
        filters=[
            StreamFilterPayload(name=COUNTRY_FILTER_NAME, mode=FILTER_MODE_ACCEPT, payload=[geo])
        ],
        **redirect_fields,
    )


def build_offer_stream_payload(
    *,
    campaign_id: int,
    offer_id: int,
    country_code: str,
    position: int = 1,
    weight: int = 50,
    action_type: str = LANDINGS_STREAM_ACTION_TYPE,
) -> StreamCreatePayload:
    geo = country_code.strip().upper()
    return StreamCreatePayload(
        campaign_id=campaign_id,
        schema_=STREAM_SCHEMA_OFFER_CAPABLE,
        name=OFFER_STREAM_NAME_TEMPLATE.format(offer_id=offer_id),
        position=position,
        weight=weight,
        action_type=action_type,
        filters=[
            StreamFilterPayload(name=COUNTRY_FILTER_NAME, mode=FILTER_MODE_REJECT, payload=[geo])
        ],
        offers=[OfferAssociationPayload(offer_id=offer_id, share=100, state="active")],
    )


class StreamUpdatePayload(KeitaroPayload):
    """Partial stream update: only explicitly set fields are sent.

    ``to_api_dict`` uses ``exclude_unset`` so unchanged managed fields never
    reach Keitaro (spec section 7.2). Offer updates contain the complete
    desired association collection; omitted associations are deleted from the
    stream. Disabled streams are sent with ``weight=0``.
    """

    name: str | None = None
    position: int | None = None
    weight: int | None = None
    state: str | None = None
    action_type: str | None = None
    action_payload: str | None = None
    action_options: dict[str, Any] | None = None
    filters: list[StreamFilterPayload] | None = None
    offers: list[OfferAssociationPayload] | None = None

    def to_api_dict(self) -> dict[str, Any]:
        """Partial update payload: only explicitly set fields are sent.

        Set fields are serialized completely (including nested filters and
        offer associations); everything else stays out of the request.
        """
        data = self.model_dump()
        return {key: value for key, value in data.items() if key in self.model_fields_set}
