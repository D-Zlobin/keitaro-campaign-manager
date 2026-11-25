"""HTTPX client for the Keitaro Admin API.

- Authenticates with the ``Api-Key`` header; the key never leaves the backend
  and is redacted from errors and logs.
- Applies connection/read timeouts to every call.
- Retries safe reads only, with bounded exponential backoff for timeouts,
  HTTP 429, and transient 5xx responses. Writes are never retried blindly;
  recovery belongs to the caller's idempotency/confirmation machinery.
- Paginates index endpoints with limit/offset, deduplicating by ID so an
  endpoint that ignores pagination parameters still terminates correctly.
- Maps timeouts, transport failures, error statuses, malformed JSON, and
  missing required fields to a typed exception hierarchy. Application
  services translate these into API errors in later stages.
- Logs operation name, IDs, duration, status category, and attempt count
  only — never headers, credentials, or response bodies.
"""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.keitaro.adapter import KeitaroAdapter
from app.keitaro.models import (
    KeitaroCampaign,
    KeitaroDomain,
    KeitaroGroup,
    KeitaroOffer,
    KeitaroStream,
    KeitaroTrafficSource,
)
from app.keitaro.payloads import (
    CampaignCreatePayload,
    StreamCreatePayload,
    StreamUpdatePayload,
)
from app.upstream_retry import DEFAULT_RETRY_POLICY, RetryPolicy

logger = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", bound=BaseModel)

_ERROR_SNIPPET_LIMIT = 200
_PAGE_LIMIT = 500
_MAX_PAGES = 100


class KeitaroClientError(Exception):
    """Base class for Keitaro integration failures."""


class KeitaroTimeoutError(KeitaroClientError):
    pass


class KeitaroConnectionError(KeitaroClientError):
    pass


class KeitaroResponseError(KeitaroClientError):
    """Keitaro returned an unsuccessful status."""

    def __init__(self, operation: str, status_code: int, snippet: str) -> None:
        super().__init__(f"Keitaro {operation} failed with status {status_code}")
        self.operation = operation
        self.status_code = status_code
        self.snippet = snippet


class KeitaroInvalidResponseError(KeitaroClientError):
    """Keitaro returned a success status with malformed or incomplete data."""


class KeitaroClient(KeitaroAdapter):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        self._api_key = api_key
        self._retry_policy = retry_policy
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Api-Key": api_key},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> KeitaroClient:
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

    def _sleep_before_retry(
        self, operation: str, method: str, path: str, retry_number: int, reason: str
    ) -> None:
        delay = self._retry_policy.delay_for(retry_number)
        logger.info(
            "keitaro operation=%s %s %s retry=%d/%d reason=%s delay_ms=%d",
            operation,
            method,
            path,
            retry_number,
            self._retry_policy.max_retries,
            reason,
            round(delay * 1000),
        )
        self._retry_policy.sleeper(delay)

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        retries = 0
        while True:
            attempt_start = time.monotonic()
            response: httpx.Response | None = None
            error: httpx.TransportError | None = None
            try:
                response = self._client.request(method, path, params=params, json=payload)
                status_category = f"{response.status_code // 100}xx"
            except httpx.TimeoutException as exc:
                error = exc
                status_category = "timeout"
            except httpx.TransportError as exc:
                error = exc
                status_category = "connection_error"
            logger.info(
                "keitaro operation=%s %s %s status=%s duration_ms=%d attempt=%d",
                operation,
                method,
                path,
                status_category,
                round((time.monotonic() - attempt_start) * 1000),
                retries + 1,
            )

            if error is not None:
                if isinstance(error, httpx.TimeoutException) and self._retry_policy.allows_retry(
                    method=method, retry_number=retries + 1
                ):
                    retries += 1
                    self._sleep_before_retry(operation, method, path, retries, "timeout")
                    continue
                if isinstance(error, httpx.TimeoutException):
                    raise KeitaroTimeoutError(f"Keitaro {operation} timed out") from error
                raise KeitaroConnectionError(f"Keitaro {operation} connection failed") from error

            assert response is not None
            if self._retry_policy.is_retryable_status(response.status_code) and (
                self._retry_policy.allows_retry(method=method, retry_number=retries + 1)
            ):
                retries += 1
                self._sleep_before_retry(
                    operation, method, path, retries, f"status_{response.status_code}"
                )
                continue
            if response.status_code >= 400:
                raise KeitaroResponseError(
                    operation, response.status_code, self._sanitize(response.text)
                )
            break

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise KeitaroInvalidResponseError(
                f"Keitaro {operation} returned malformed JSON"
            ) from exc

    @staticmethod
    def _parse(model: type[_ModelT], data: Any, operation: str) -> _ModelT:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise KeitaroInvalidResponseError(
                f"Keitaro {operation} response failed validation"
            ) from exc

    def _parse_list(self, model: type[_ModelT], data: Any, operation: str) -> list[_ModelT]:
        if not isinstance(data, list):
            raise KeitaroInvalidResponseError(f"Keitaro {operation} returned a non-list response")
        return [self._parse(model, item, operation) for item in data]

    def _paged_raw_list(
        self, path: str, *, operation: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """Fetch every page of an index endpoint, deduplicating by item ID.

        If an endpoint ignores ``limit``/``offset`` and always returns the
        full collection, the first page contains everything and iteration
        stops at the first page that adds no new IDs. Bounded by
        ``_MAX_PAGES`` regardless; exceeding the bound means the collection
        is incomplete and is reported as an integration failure.
        """
        collected: list[Any] = []
        seen_ids: set[int] = set()
        offset = 0
        for _ in range(_MAX_PAGES):
            page_params = {"limit": _PAGE_LIMIT, "offset": offset, **(params or {})}
            data = self._request("GET", path, operation=operation, params=page_params)
            if not isinstance(data, list):
                raise KeitaroInvalidResponseError(
                    f"Keitaro {operation} returned a non-list response"
                )
            new_items: list[Any] = []
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("id"), int):
                    if item["id"] in seen_ids:
                        continue
                    seen_ids.add(item["id"])
                new_items.append(item)
            collected.extend(new_items)
            if not data or not new_items or len(data) < _PAGE_LIMIT:
                return collected
            offset += _PAGE_LIMIT
        raise KeitaroInvalidResponseError(
            f"Keitaro {operation} exceeded {_MAX_PAGES} pages; aborting as incomplete"
        )

    # Safe reads -----------------------------------------------------------

    def get_campaign(self, campaign_id: int) -> KeitaroCampaign:
        data = self._request("GET", f"/campaigns/{campaign_id}", operation="get_campaign")
        return self._parse(KeitaroCampaign, data, "get_campaign")

    def get_campaign_streams(self, campaign_id: int) -> list[KeitaroStream]:
        data = self._paged_raw_list(
            f"/campaigns/{campaign_id}/streams", operation="get_campaign_streams"
        )
        return [self._parse(KeitaroStream, item, "get_campaign_streams") for item in data]

    def get_stream(self, stream_id: int) -> KeitaroStream:
        data = self._request("GET", f"/streams/{stream_id}", operation="get_stream")
        return self._parse(KeitaroStream, data, "get_stream")

    def list_offers(self) -> list[KeitaroOffer]:
        data = self._paged_raw_list("/offers", operation="list_offers")
        return [self._parse(KeitaroOffer, item, "list_offers") for item in data]

    def get_offer(self, offer_id: int) -> KeitaroOffer:
        data = self._request("GET", f"/offers/{offer_id}", operation="get_offer")
        return self._parse(KeitaroOffer, data, "get_offer")

    def list_domains(self) -> list[KeitaroDomain]:
        data = self._paged_raw_list("/domains", operation="list_domains")
        return [self._parse(KeitaroDomain, item, "list_domains") for item in data]

    def list_groups(self, group_type: str = "campaigns") -> list[KeitaroGroup]:
        data = self._paged_raw_list("/groups", operation="list_groups", params={"type": group_type})
        return [self._parse(KeitaroGroup, item, "list_groups") for item in data]

    def list_traffic_sources(self) -> list[KeitaroTrafficSource]:
        data = self._paged_raw_list("/traffic_sources", operation="list_traffic_sources")
        return [self._parse(KeitaroTrafficSource, item, "list_traffic_sources") for item in data]

    # Writes (never retried automatically) ---------------------------------

    def create_campaign(self, payload: CampaignCreatePayload) -> KeitaroCampaign:
        data = self._request(
            "POST", "/campaigns", operation="create_campaign", payload=payload.to_api_dict()
        )
        return self._parse(KeitaroCampaign, data, "create_campaign")

    def archive_campaign(self, campaign_id: int) -> None:
        self._request("DELETE", f"/campaigns/{campaign_id}", operation="archive_campaign")

    def create_stream(self, payload: StreamCreatePayload) -> KeitaroStream:
        data = self._request(
            "POST", "/streams", operation="create_stream", payload=payload.to_api_dict()
        )
        return self._parse(KeitaroStream, data, "create_stream")

    def update_stream(self, stream_id: int, payload: StreamUpdatePayload) -> KeitaroStream:
        data = self._request(
            "PUT",
            f"/streams/{stream_id}",
            operation="update_stream",
            payload=payload.to_api_dict(),
        )
        return self._parse(KeitaroStream, data, "update_stream")

    def disable_stream(self, stream_id: int) -> None:
        self._request("POST", f"/streams/{stream_id}/disable", operation="disable_stream")

    def enable_stream(self, stream_id: int) -> None:
        self._request("POST", f"/streams/{stream_id}/enable", operation="enable_stream")
