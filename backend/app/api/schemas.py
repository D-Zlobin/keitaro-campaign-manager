"""Application API contracts (spec section 8, plan stages 0, 4, and 5).

These models define the stable request/response shapes for campaign
creation, listing, import, and fetch, plus the stage-5 editor contracts:
campaign detail, the draft command payload, the Geo selector, and the Five
API-backed offer autocomplete. The browser only ever talks to this API;
upstream credentials stay server-side.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.states import OfferAssociationState, StreamState


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


# Campaign detail and draft editing (plan stage 5) -----------------------------


class DraftOffer(BaseModel):
    """One offer association of the editable draft."""

    offer_id: int
    offer_name: str
    keitaro_association_id: int | None
    state: OfferAssociationState
    share: int


class DraftStream(BaseModel):
    """One editable draft stream with its offer associations.

    ``schema`` and ``type`` are exposed so the UI can tell offer-capable
    streams apart; names and positions are read-only display data.
    """

    model_config = ConfigDict(populate_by_name=True)

    keitaro_id: int
    name: str
    type: str
    schema_: str = Field(alias="schema")
    position: int
    state: StreamState
    share: int
    pinned: bool
    offer_capable: bool
    offers: list[DraftOffer]


class CampaignDetailResponse(BaseModel):
    """Editor load state: baseline metadata, draft, pins, revision, dirty
    state, notices, and whether synchronization currently blocks uploads."""

    campaign: CampaignSummary
    streams: list[DraftStream]
    notices: list[str] = Field(default_factory=list)
    upload_blocked: bool


class DeleteStreamCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["delete_stream"]
    stream_id: int = Field(gt=0)


class RestoreStreamCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["restore_stream"]
    stream_id: int = Field(gt=0)


class PinStreamCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["pin_stream"]
    stream_id: int = Field(gt=0)


class UnpinStreamCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["unpin_stream"]
    stream_id: int = Field(gt=0)


class AddOfferCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["add_offer"]
    stream_id: int = Field(gt=0)
    offer_id: int = Field(gt=0)


class RemoveOfferCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["remove_offer"]
    stream_id: int = Field(gt=0)
    offer_id: int = Field(gt=0)


# The only editable draft operations (spec sections 6.6-6.8): soft delete and
# restore, pin and unpin, add and remove/reactivate offer. Stream creation,
# renames, and manual percentages are deliberately not expressible.
DraftCommand = Annotated[
    DeleteStreamCommand
    | RestoreStreamCommand
    | PinStreamCommand
    | UnpinStreamCommand
    | AddOfferCommand
    | RemoveOfferCommand,
    Field(discriminator="type"),
]


class DraftUpdateRequest(BaseModel):
    """One transactional draft mutation against a known local revision."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    command: DraftCommand


# Catalog endpoints: Geo selector and offer autocomplete (plan stage 5) --------


class GeoListResponse(BaseModel):
    geos: list[GeoOption]


class OfferListResponse(BaseModel):
    offers: list[OfferOption]
