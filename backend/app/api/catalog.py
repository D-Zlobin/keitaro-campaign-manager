"""Geo selector and offer autocomplete endpoints (plan stage 5).

The Geo list is served from the backend's static ISO 3166-1 list and offer
search goes through the Keitaro adapter; the browser never holds upstream
credentials and never calls Keitaro directly. Offer search optionally takes
the stream being edited so offers already active there are excluded from
the selector.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from app.api.schemas import GeoListResponse, OfferListResponse
from app.services.catalog import CatalogService

router = APIRouter(tags=["catalog"])


def _catalog(request: Request) -> CatalogService:
    return request.app.state.catalog_service


@router.get("/geos", response_model=GeoListResponse)
async def list_geos(request: Request) -> GeoListResponse:
    return await run_in_threadpool(_catalog(request).list_geos)


@router.get("/offers", response_model=OfferListResponse)
async def search_offers(
    request: Request,
    search: str,
    campaign_id: uuid.UUID | None = None,
    stream_id: int | None = Query(default=None, gt=0),
) -> OfferListResponse:
    return await run_in_threadpool(
        _catalog(request).search_offers,
        search=search,
        campaign_id=campaign_id,
        stream_id=stream_id,
    )
