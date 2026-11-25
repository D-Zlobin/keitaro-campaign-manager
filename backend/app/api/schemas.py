"""Application API contracts (spec section 8, plan stage 0).

These models define the stable response shapes for the Geo selector and the
Five API-backed offer autocomplete. The browser only ever talks to this API;
upstream credentials stay server-side. The endpoints themselves are wired in
later stages (plan stages 3 and 5).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
