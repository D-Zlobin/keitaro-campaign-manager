"""Response models for the assumed Five API contract (see package docstring).

Validation is strict: any malformed entry fails the whole response, because
silently dropping Geo/autocomplete entries would make the UI selectors
unreliable. Codes are normalized to uppercase ISO 3166-1 alpha-2 on the way
in; ``keitaro_offer_id`` is the resolved Keitaro identity that creation
re-validates against Keitaro before any campaign is created.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FiveGeo(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    code: str = Field(pattern=r"^[A-Za-z]{2}$")
    name: str = Field(min_length=1, max_length=255)


class FiveOffer(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    keitaro_offer_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
