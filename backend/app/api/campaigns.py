"""Campaign list, creation, import, and fetch endpoints (plan stage 4).

The service layer is synchronous (sync HTTP client and SQLAlchemy sessions),
so endpoints run it in the threadpool. Fetch additionally holds the
per-campaign asyncio lock so it cannot interleave with an upload of the same
campaign; creation coordinates concurrent duplicates on the idempotency key
inside the service.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.schemas import (
    CampaignCreatedResponse,
    CampaignCreateRequest,
    CampaignFetchRequest,
    CampaignFetchResponse,
    CampaignImportRequest,
    CampaignListResponse,
)
from app.errors import BadRequestError
from app.services.campaigns import CampaignService, CreateCampaignCommand
from app.services.locks import UploadLockManager

router = APIRouter(tags=["campaigns"])


def _service(request: Request) -> CampaignService:
    return request.app.state.campaign_service


@router.get("/campaigns", response_model=CampaignListResponse)
async def list_campaigns(request: Request) -> CampaignListResponse:
    return await run_in_threadpool(_service(request).list_campaigns)


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreateRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=255)],
) -> JSONResponse:
    key = idempotency_key.strip()
    if not key:
        raise BadRequestError("The Idempotency-Key header must not be empty.")
    command = CreateCampaignCommand(
        name=payload.name, country_code=payload.country_code, offer_id=payload.offer_id
    )
    outcome = await run_in_threadpool(
        _service(request).create_campaign, command, idempotency_key=key
    )
    return JSONResponse(status_code=outcome.status_code, content=outcome.body)


@router.post("/campaigns/import", status_code=status.HTTP_201_CREATED)
async def import_campaign(
    payload: CampaignImportRequest, request: Request
) -> CampaignCreatedResponse:
    return await run_in_threadpool(_service(request).import_campaign, payload.keitaro_campaign_id)


@router.post("/campaigns/{campaign_id}/fetch", response_model=CampaignFetchResponse)
async def fetch_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    payload: CampaignFetchRequest | None = None,
) -> CampaignFetchResponse:
    confirm_discard = payload.confirm_discard if payload is not None else False
    locks: UploadLockManager = request.app.state.upload_locks
    async with locks.acquire(campaign_id):
        return await run_in_threadpool(
            _service(request).fetch_campaign, campaign_id, confirm_discard=confirm_discard
        )
