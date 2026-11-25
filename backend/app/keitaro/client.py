"""HTTPX client for the Keitaro Admin API.

- Authenticates with the ``Api-Key`` header; the key never leaves the backend
  and is redacted from errors and logs.
- Applies connection/read timeouts to every call.
- Maps timeouts, transport failures, error statuses, malformed JSON, and
  missing required fields to a typed exception hierarchy. Application
  services translate these into API errors in later stages.
- Logs operation name, IDs, duration, and status category only — never
  headers, credentials, or response bodies.

Bounded retries for safe reads are added with the full adapter in stage 3;
write operations are never blindly retried.
"""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

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

logger = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", bound=BaseModel)

_ERROR_SNIPPET_LIMIT = 200


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


class KeitaroClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
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

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        start = time.monotonic()
        status_category = "error"
        try:
            response = self._client.request(method, path, params=params, json=payload)
            status_category = f"{response.status_code // 100}xx"
        except httpx.TimeoutException as exc:
            raise KeitaroTimeoutError(f"Keitaro {operation} timed out") from exc
        except httpx.TransportError as exc:
            raise KeitaroConnectionError(f"Keitaro {operation} connection failed") from exc
        finally:
            duration_ms = round((time.monotonic() - start) * 1000)
            logger.info(
                "keitaro operation=%s %s %s status=%s duration_ms=%d",
                operation,
                method,
                path,
                status_category,
                duration_ms,
            )

        if response.status_code >= 400:
            raise KeitaroResponseError(
                operation, response.status_code, self._sanitize(response.text)
            )
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

    # Safe reads -----------------------------------------------------------

    def get_campaign(self, campaign_id: int) -> KeitaroCampaign:
        data = self._request("GET", f"/campaigns/{campaign_id}", operation="get_campaign")
        return self._parse(KeitaroCampaign, data, "get_campaign")

    def get_campaign_streams(self, campaign_id: int) -> list[KeitaroStream]:
        data = self._request(
            "GET", f"/campaigns/{campaign_id}/streams", operation="get_campaign_streams"
        )
        return self._parse_list(KeitaroStream, data, "get_campaign_streams")

    def get_stream(self, stream_id: int) -> KeitaroStream:
        data = self._request("GET", f"/streams/{stream_id}", operation="get_stream")
        return self._parse(KeitaroStream, data, "get_stream")

    def list_offers(self) -> list[KeitaroOffer]:
        data = self._request("GET", "/offers", operation="list_offers")
        return self._parse_list(KeitaroOffer, data, "list_offers")

    def get_offer(self, offer_id: int) -> KeitaroOffer:
        data = self._request("GET", f"/offers/{offer_id}", operation="get_offer")
        return self._parse(KeitaroOffer, data, "get_offer")

    def list_domains(self) -> list[KeitaroDomain]:
        data = self._request("GET", "/domains", operation="list_domains")
        return self._parse_list(KeitaroDomain, data, "list_domains")

    def list_groups(self, group_type: str = "campaigns") -> list[KeitaroGroup]:
        data = self._request("GET", "/groups", operation="list_groups", params={"type": group_type})
        return self._parse_list(KeitaroGroup, data, "list_groups")

    def list_traffic_sources(self) -> list[KeitaroTrafficSource]:
        data = self._request("GET", "/traffic_sources", operation="list_traffic_sources")
        return self._parse_list(KeitaroTrafficSource, data, "list_traffic_sources")

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
