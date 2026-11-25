"""Application API contracts (spec section 8, plan stages 0 and 4).

These models define the stable request/response shapes for campaign
creation, listing, import, and fetch, plus the Geo selector and the Five
API-backed offer autocomplete. The browser only ever talks to this API;
upstream credentials stay server-side. The Geo/offer endpoints themselves
are wired in stage 5.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GeoOption(BaseModel):
    """One selectable country for the New Campaign dialog."""

    code: str = Field(pattern=r"^[A-Z]{2}$", description="ISO 3166-1 alpha-2, uppercase")
    name: str = Field(min_length=1)


class OfferOption(BaseModel):
    """One selectable offer returned by offer autocomplete.

    ``offer_id`` is the resolved Keitaro offer ID that creation submits; it is
    re-validated against Keitaro before any campaign is created.
    """

    offer_id: int = Field(gt=0)
    name: str = Field(min_length=1)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


# Campaign creation, import, listing, and fetch (plan stage 4) -----------------


class CampaignCreateRequest(BaseModel):
    """New Campaign dialog submission (spec section 6.2).

    Values are normalized here (trimmed name, uppercase country code); ISO
    membership and offer availability are validated by the service.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    country_code: str = Field(pattern=r"^[A-Za-z]{2}$")
    offer_id: int = Field(gt=0)

    @field_validator("country_code")
    @classmethod
    def _normalize_country_code(cls, value: str) -> str:
        return value.upper()


class CampaignImportRequest(BaseModel):
    keitaro_campaign_id: int = Field(gt=0)


class CampaignFetchRequest(BaseModel):
    """Operator-initiated fetch; discarding a dirty draft needs the flag."""

    confirm_discard: bool = False


class CampaignSummary(BaseModel):
    """Locally known campaign state for lists and mutation responses."""

    id: uuid.UUID
    keitaro_id: int
    name: str
    country_code: str | None
    sync_state: str
    dirty: bool
    revision: int
    last_fetch_at: datetime | None
    last_upload_at: datetime | None
    created_at: datetime


class CampaignCreatedResponse(BaseModel):
    """201 result of creation and import; also the recorded idempotent replay."""

    campaign: CampaignSummary
    notices: list[str] = Field(default_factory=list)


class CampaignListResponse(BaseModel):
    campaigns: list[CampaignSummary]


class CampaignFetchResponse(BaseModel):
    campaign: CampaignSummary
    notices: list[str] = Field(default_factory=list)
