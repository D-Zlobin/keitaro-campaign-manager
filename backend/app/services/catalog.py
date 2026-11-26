"""Geo list and offer autocomplete through the server-side Five API (stage 5).

The browser never calls the upstream directory directly: this service holds
the only path to it, translates its failures into the standard error
envelope, and applies the editor's exclusion rule — offers already actively
associated with the stream being edited are not selectable (spec section
6.8). While the integration is unconfigured the endpoints fail with 503, as
documented in ``app.config``.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.api.schemas import GeoListResponse, GeoOption, OfferListResponse, OfferOption
from app.db.engine import session_scope
from app.domain.states import OfferAssociationState
from app.errors import (
    AppError,
    BadRequestError,
    NotFoundError,
    ServiceUnavailableError,
    UpstreamError,
    UpstreamTimeoutError,
)
from app.five.errors import FiveApiError, FiveApiTimeoutError
from app.repositories.campaigns import CampaignRepository


class FiveApi(Protocol):
    """The catalog operations the application needs from the Five API."""

    def list_geos(self) -> list[GeoOption]: ...

    def search_offers(self, search: str) -> list[OfferOption]: ...


def _five_error(exc: FiveApiError, *, service: str) -> AppError:
    if isinstance(exc, FiveApiTimeoutError):
        return UpstreamTimeoutError(f"The {service} did not respond in time. Try again.")
    return UpstreamError(f"The {service} returned an error.")


class CatalogService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        five: FiveApi | None,
        campaigns: CampaignRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._five = five
        self._campaigns = campaigns or CampaignRepository()

    def _require_five(self) -> FiveApi:
        if self._five is None:
            raise ServiceUnavailableError(
                "The offer and Geo catalog integration is not configured on this backend."
            )
        return self._five

    def list_geos(self) -> GeoListResponse:
        """Stable country codes and display names for the New Campaign dialog."""
        five = self._require_five()
        try:
            return GeoListResponse(geos=five.list_geos())
        except FiveApiError as exc:
            raise _five_error(exc, service="Geo list service") from exc

    def search_offers(
        self,
        *,
        search: str,
        campaign_id: uuid.UUID | None = None,
        stream_id: int | None = None,
    ) -> OfferListResponse:
        """Autocomplete existing offers, resolving Keitaro offer IDs.

        When ``campaign_id`` and ``stream_id`` identify the stream being
        edited, offers already actively associated with it are excluded;
        disabled associations stay selectable so re-adding reactivates them.
        """
        five = self._require_five()
        term = search.strip()
        if not term:
            raise BadRequestError("The search term must not be empty.")
        if (campaign_id is None) != (stream_id is None):
            raise BadRequestError(
                "campaign_id and stream_id must be provided together to exclude "
                "offers already active in the stream being edited."
            )
        excluded: set[int] = set()
        if campaign_id is not None:
            with session_scope(self._session_factory) as session:
                campaign = self._campaigns.get_with_draft(session, campaign_id)
                if campaign is None:
                    raise NotFoundError(
                        "Campaign not found.", details={"campaign_id": str(campaign_id)}
                    )
                stream = next((s for s in campaign.streams if s.keitaro_id == stream_id), None)
                if stream is None:
                    raise NotFoundError(
                        f"Stream {stream_id} is not part of this campaign's draft.",
                        code="stream_not_found",
                        details={"stream_id": stream_id},
                    )
                excluded = {
                    offer.offer_id
                    for offer in stream.offers
                    if offer.state is OfferAssociationState.ACTIVE
                }
        try:
            offers = five.search_offers(term)
        except FiveApiError as exc:
            raise _five_error(exc, service="offer autocomplete service") from exc
        return OfferListResponse(
            offers=[offer for offer in offers if offer.offer_id not in excluded]
        )
