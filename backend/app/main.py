"""FastAPI application factory.

Run with:
    uvicorn --factory app.main:create_app --reload

Startup validates the environment-backed settings and fails with a clear
error when required values are missing or invalid. No Keitaro calls are made
at startup; the Keitaro client is constructed lazily-offline and closed on
shutdown. Startup also resets campaigns left in ``syncing`` by a crash —
single-process deployment guarantees no upload is running then — while
``needs_attention`` persists until a recovery fetch.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import OperationalError

from app.api import campaigns, catalog, health
from app.config import Settings, load_settings
from app.db.engine import build_engine, make_session_factory, session_scope
from app.errors import register_error_handlers
from app.five.client import FiveApiClient
from app.keitaro.adapter import KeitaroAdapter
from app.keitaro.client import KeitaroClient
from app.logging_config import configure_logging
from app.repositories.campaigns import CampaignRepository
from app.services.campaigns import CampaignService
from app.services.catalog import CatalogService, FiveApi
from app.services.drafts import DraftService
from app.services.locks import UploadLockManager
from app.services.uploads import UploadService

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    keitaro: KeitaroAdapter | None = None,
    five: FiveApi | None = None,
) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    configure_logging(settings.log_level, settings.secret_values())

    engine = build_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    keitaro_adapter: KeitaroAdapter = (
        keitaro
        if keitaro is not None
        else KeitaroClient(
            base_url=settings.keitaro_base_url,
            api_key=settings.keitaro_api_key.get_secret_value(),
            timeout_seconds=settings.keitaro_timeout_seconds,
        )
    )
    five_api: FiveApi | None = five
    if five_api is None and settings.five_api_base_url and settings.five_api_api_key:
        five_api = FiveApiClient(
            base_url=settings.five_api_base_url,
            api_key=settings.five_api_api_key.get_secret_value(),
            timeout_seconds=settings.five_api_timeout_seconds,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            with session_scope(session_factory) as session:
                stale_syncing = CampaignRepository().reset_stale_syncing(session)
        except OperationalError as exc:
            raise RuntimeError(
                "The database is not migrated (or not reachable). "
                "Run `alembic upgrade head` before starting the application."
            ) from exc
        if stale_syncing:
            logger.warning(
                "reset %d campaign(s) stuck in 'syncing' after a restart; "
                "their drafts remain dirty and the next upload preflight will "
                "detect any partial remote writes",
                stale_syncing,
            )
        yield
        for client in (keitaro_adapter, five_api):
            close = getattr(client, "close", None)
            if callable(close):
                close()
        engine.dispose()

    app = FastAPI(title="Keitaro Campaign Manager", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.keitaro = keitaro_adapter
    app.state.upload_locks = UploadLockManager()
    app.state.campaign_service = CampaignService(
        session_factory=session_factory, keitaro=keitaro_adapter, settings=settings
    )
    app.state.draft_service = DraftService(session_factory=session_factory, keitaro=keitaro_adapter)
    app.state.upload_service = UploadService(
        session_factory=session_factory, keitaro=keitaro_adapter
    )
    app.state.catalog_service = CatalogService(session_factory=session_factory, five=five_api)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )
    register_error_handlers(app)
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(campaigns.router, prefix=API_PREFIX)
    app.include_router(catalog.router, prefix=API_PREFIX)
    return app
