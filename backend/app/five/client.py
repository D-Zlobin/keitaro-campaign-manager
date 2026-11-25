"""HTTPX client for the Five API (server-side only).

Mirrors the Keitaro client guarantees: bearer credential redacted from
errors and logs, timeouts on every call, safe-read-only bounded retries,
strict response validation, and sanitized operation logging. All Five API
calls are safe reads, so they all retry within the bounded policy.

Results are translated into the stable application response models
(``GeoOption`` / ``OfferOption``); upstream wire details never leak past
this package.
"""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.api.schemas import GeoOption, OfferOption
from app.five.errors import (
    FiveApiConnectionError,
    FiveApiInvalidResponseError,
    FiveApiResponseError,
    FiveApiTimeoutError,
)
from app.five.models import FiveGeo, FiveOffer
from app.upstream_retry import DEFAULT_RETRY_POLICY, RetryPolicy

logger = logging.getLogger(__name__)

_ERROR_SNIPPET_LIMIT = 200


class FiveApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        self._api_key = api_key
        self._retry_policy = retry_policy
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FiveApiClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _sanitize(self, text: str) -> str:
        return text.replace(self._api_key, "***")[:_ERROR_SNIPPET_LIMIT]

    def _request(
        self,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        retries = 0
        while True:
            attempt_start = time.monotonic()
            response: httpx.Response | None = None
            error: httpx.TransportError | None = None
            try:
                response = self._client.get(path, params=params)
                status_category = f"{response.status_code // 100}xx"
            except httpx.TimeoutException as exc:
                error = exc
                status_category = "timeout"
            except httpx.TransportError as exc:
                error = exc
                status_category = "connection_error"
            logger.info(
                "five_api operation=%s GET %s status=%s duration_ms=%d attempt=%d",
                operation,
                path,
                status_category,
                round((time.monotonic() - attempt_start) * 1000),
                retries + 1,
            )

            if error is not None:
                if isinstance(error, httpx.TimeoutException) and self._retry_policy.allows_retry(
                    method="GET", retry_number=retries + 1
                ):
                    retries += 1
                    delay = self._retry_policy.delay_for(retries)
                    logger.info(
                        "five_api operation=%s GET %s retry=%d/%d reason=timeout delay_ms=%d",
                        operation,
                        path,
                        retries,
                        self._retry_policy.max_retries,
                        round(delay * 1000),
                    )
                    self._retry_policy.sleeper(delay)
                    continue
                if isinstance(error, httpx.TimeoutException):
                    raise FiveApiTimeoutError(f"Five API {operation} timed out") from error
                raise FiveApiConnectionError(f"Five API {operation} connection failed") from error

            assert response is not None
            if self._retry_policy.is_retryable_status(response.status_code) and (
                self._retry_policy.allows_retry(method="GET", retry_number=retries + 1)
            ):
                retries += 1
                delay = self._retry_policy.delay_for(retries)
                logger.info(
                    "five_api operation=%s GET %s retry=%d/%d reason=status_%d delay_ms=%d",
                    operation,
                    path,
                    retries,
                    self._retry_policy.max_retries,
                    response.status_code,
                    round(delay * 1000),
                )
                self._retry_policy.sleeper(delay)
                continue
            if response.status_code >= 400:
                raise FiveApiResponseError(
                    operation, response.status_code, self._sanitize(response.text)
                )
            break

        if not response.content:
            raise FiveApiInvalidResponseError(f"Five API {operation} returned an empty body")
        try:
            data = response.json()
        except ValueError as exc:
            raise FiveApiInvalidResponseError(
                f"Five API {operation} returned malformed JSON"
            ) from exc
        if not isinstance(data, list):
            raise FiveApiInvalidResponseError(f"Five API {operation} returned a non-list response")
        return data

    @staticmethod
    def _parse_list(model: type[BaseModel], data: Any, operation: str) -> list[Any]:
        parsed = []
        for item in data:
            try:
                parsed.append(model.model_validate(item))
            except ValidationError as exc:
                raise FiveApiInvalidResponseError(
                    f"Five API {operation} response failed validation"
                ) from exc
        return parsed

    def list_geos(self) -> list[GeoOption]:
        """All selectable countries for the New Campaign Geo field."""
        data = self._request("/geos", operation="list_geos")
        geos = self._parse_list(FiveGeo, data, "list_geos")
        return [
            GeoOption(code=geo.code.upper(), name=geo.name)
            for geo in sorted(geos, key=lambda g: g.code.upper())
        ]

    def search_offers(self, search: str) -> list[OfferOption]:
        """Autocomplete selectable offers by free-text search term."""
        term = search.strip()
        if not term:
            raise ValueError("search term must be non-empty")
        data = self._request("/offers", operation="search_offers", params={"search": term})
        offers = self._parse_list(FiveOffer, data, "search_offers")
        return [
            OfferOption(offer_id=offer.keitaro_offer_id, name=offer.name)
            for offer in sorted(offers, key=lambda o: o.keitaro_offer_id)
        ]
