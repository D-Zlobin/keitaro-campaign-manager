"""Geo list and offer autocomplete catalogs (plan stage 5).

The Geo selector is served from the backend's static ISO 3166-1 alpha-2
list (``app.domain.geos``) — Keitaro has no country-list endpoint, and the
browser still receives an API-provided list rather than a hardcoded
frontend one.

Offer autocomplete searches Keitaro's own offers through the adapter
(``GET /offers``), so the IDs the browser submits are Keitaro offer IDs by
construction; creation and draft additions still re-validate them against
Keitaro. The service applies the editor's exclusion rule — offers already
actively associated with the stream being edited are not selectable (spec
section 6.8) — and translates Keitaro failures into the standard error
envelope.
"""

from __future__ import annotations

import unicodedata
import uuid

from sqlalchemy.orm import Session, sessionmaker

from app.api.schemas import GeoListResponse, GeoOption, OfferListResponse, OfferOption
from app.db.engine import session_scope
from app.domain.geos import COUNTRY_NAMES
from app.domain.states import OfferAssociationState
from app.errors import BadRequestError, NotFoundError
from app.keitaro.adapter import KeitaroAdapter
from app.keitaro.client import KeitaroClientError
from app.repositories.campaigns import CampaignRepository
from app.services.campaigns import _upstream_error


def _geo_sort_key(name: str) -> str:
    """Accent-insensitive alphabetical order for the selector."""
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()


class CatalogService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        keitaro: KeitaroAdapter,
        campaigns: CampaignRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._keitaro = keitaro
        self._campaigns = campaigns or CampaignRepository()

    def list_geos(self) -> GeoListResponse:
        """Stable country codes and display names for the New Campaign dialog."""
        options = [GeoOption(code=code, name=name) for code, name in COUNTRY_NAMES.items()]
        options.sort(key=lambda option: _geo_sort_key(option.name))
        return GeoListResponse(geos=options)

    def search_offers(
        self,
        *,
        search: str,
        campaign_id: uuid.UUID | None = None,
        stream_id: int | None = None,
    ) -> OfferListResponse:
        """Autocomplete existing, non-deleted Keitaro offers.

        When ``campaign_id`` and ``stream_id`` identify the stream being
        edited, offers already actively associated with it are excluded;
        disabled associations stay selectable so re-adding reactivates them.
        """
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
            offers = self._keitaro.list_offers()
        except KeitaroClientError as exc:
            raise _upstream_error(exc) from exc
        needle = term.casefold()
        return OfferListResponse(
            offers=[
                OfferOption(offer_id=offer.id, name=offer.name)
                for offer in offers
                if not offer.is_deleted
                and offer.id not in excluded
                and needle in offer.name.casefold()
            ]
        )
