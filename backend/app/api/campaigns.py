"""Campaign list, creation, import, fetch, detail, and draft endpoints.

The service layer is synchronous (sync HTTP client and SQLAlchemy sessions),
so endpoints run it in the threadpool. Fetch and draft mutation additionally
hold the per-campaign asyncio lock so they cannot interleave with an upload
of the same campaign; creation coordinates concurrent duplicates on the
idempotency key inside the service.
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
    CampaignDetailResponse,
    CampaignFetchRequest,
    CampaignFetchResponse,
    CampaignImportRequest,
    CampaignListResponse,
    DraftUpdateRequest,
)
from app.errors import BadRequestError
from app.services.campaigns import CampaignService, CreateCampaignCommand
from app.services.drafts import DraftService
from app.services.locks import UploadLockManager

router = APIRouter(tags=["campaigns"])


def _service(request: Request) -> CampaignService:
    return request.app.state.campaign_service


def _draft_service(request: Request) -> DraftService:
    return request.app.state.draft_service


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


@router.get("/campaigns/{campaign_id}", response_model=CampaignDetailResponse)
async def get_campaign(campaign_id: uuid.UUID, request: Request) -> CampaignDetailResponse:
    return await run_in_threadpool(_draft_service(request).get_campaign_detail, campaign_id)


@router.put("/campaigns/{campaign_id}/draft", response_model=CampaignDetailResponse)
async def update_campaign_draft(
    campaign_id: uuid.UUID, payload: DraftUpdateRequest, request: Request
) -> CampaignDetailResponse:
    locks: UploadLockManager = request.app.state.upload_locks
    async with locks.acquire(campaign_id):
        return await run_in_threadpool(
            _draft_service(request).apply_draft_command,
            campaign_id,
            expected_revision=payload.expected_revision,
            command=payload.command,
        )
