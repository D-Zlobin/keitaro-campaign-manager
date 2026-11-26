"""Consistent API error responses (spec section 8).

Every error leaves the API as::

    {"error": {"code": ..., "message": ..., "details": {...}}}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.domain.errors import (
    OfferNotFoundError,
    OfferRuleViolationError,
    StreamNotFoundError,
    StreamRuleViolationError,
    UnsupportedStreamEditError,
)
from app.repositories.exceptions import CampaignNotFoundError, StaleRevisionError
from app.repositories.idempotency import IdempotencyPayloadConflict

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class RuleViolationError(AppError):
    """JSON was valid but violates campaign rules (HTTP 422)."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "rule_violation"


class UpstreamError(AppError):
    """Keitaro or another upstream returned an invalid/unsuccessful response."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_error"


class UpstreamTimeoutError(AppError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = "upstream_timeout"


class ServiceUnavailableError(AppError):
    """A required upstream integration is not configured on this backend."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = {
            "errors": [
                {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
                for err in exc.errors()
            ]
        }
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=error_body("bad_request", "Malformed request.", details),
        )

    # Persistence-layer exceptions translate to the same envelope so services
    # can let them propagate instead of re-wrapping them at every call site.
    @app.exception_handler(CampaignNotFoundError)
    async def campaign_not_found_handler(_: Request, exc: CampaignNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=error_body(
                "not_found", "Campaign not found.", {"campaign_id": str(exc.campaign_id)}
            ),
        )

    @app.exception_handler(StaleRevisionError)
    async def stale_revision_handler(_: Request, exc: StaleRevisionError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=error_body(
                "stale_revision",
                "The local draft changed in another request. Reload the latest draft and retry.",
                {"expected_revision": exc.expected, "actual_revision": exc.actual},
            ),
        )

    @app.exception_handler(IdempotencyPayloadConflict)
    async def idempotency_conflict_handler(
        _: Request, exc: IdempotencyPayloadConflict
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=error_body(
                "idempotency_payload_conflict",
                "This Idempotency-Key was already used with a different request payload.",
            ),
        )

    # Domain rule failures from draft commands (spec section 8): unknown draft
    # resources are 404, campaign-rule violations are 422.
    @app.exception_handler(StreamNotFoundError)
    async def stream_not_found_handler(_: Request, exc: StreamNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=error_body("stream_not_found", str(exc), {"stream_id": exc.keitaro_id}),
        )

    @app.exception_handler(OfferNotFoundError)
    async def offer_not_found_handler(_: Request, exc: OfferNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=error_body("offer_not_found", str(exc), {"offer_id": exc.offer_id}),
        )

    @app.exception_handler(StreamRuleViolationError)
    async def stream_rule_handler(_: Request, exc: StreamRuleViolationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_body("stream_rule_violation", str(exc)),
        )

    @app.exception_handler(OfferRuleViolationError)
    async def offer_rule_handler(_: Request, exc: OfferRuleViolationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_body("offer_rule_violation", str(exc)),
        )

    @app.exception_handler(UnsupportedStreamEditError)
    async def unsupported_edit_handler(_: Request, exc: UnsupportedStreamEditError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=error_body("unsupported_stream_edit", str(exc)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            status.HTTP_404_NOT_FOUND: "not_found",
            status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
        }.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error."
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, message),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled application error: %s", type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("internal_error", "Internal server error."),
        )
