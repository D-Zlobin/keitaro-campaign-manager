"""FastAPI application factory.

Run with:
    uvicorn --factory app.main:create_app --reload

Startup validates the environment-backed settings and fails with a clear
error when required values are missing or invalid. No Keitaro calls are made
at startup.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.config import Settings, load_settings
from app.errors import register_error_handlers
from app.logging_config import configure_logging

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    configure_logging(settings.log_level, settings.secret_values())

    app = FastAPI(title="Keitaro Campaign Manager", version="0.1.0")
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )
    register_error_handlers(app)
    app.include_router(health.router, prefix=API_PREFIX)
    return app
