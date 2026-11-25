"""Canonical remote projections and SHA-256 fingerprints (spec section 7.1).

The canonical document contains exactly the Keitaro-managed fields:

- campaign ID and remote campaign update timestamp;
- every managed stream's ID, remote update timestamp, state, position,
  weight, schema, type, filters, and action data;
- every offer association's ID, offer ID, state, and share.

Object keys and collections are sorted deterministically (streams by ID,
filters by ID then name, associations by ID) and serialization never depends
on JSON formatting or dict insertion order, so a fingerprint changes only
when managed remote state changes. Fields without a bearing on campaign
configuration are excluded because any change to them also updates one of
the included timestamps on Keitaro's side.

Numeric weights and shares are normalized to integers when integral, so
``50`` and ``50.0`` fingerprint identically.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.keitaro.models import (
    KeitaroCampaign,
    KeitaroFilter,
    KeitaroOfferAssociation,
    KeitaroStream,
)

SHA256_HEX_LENGTH = 64


def _number(value: int | float) -> int | float:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _canonical_filter(stream_filter: KeitaroFilter) -> dict[str, Any]:
    return {
        "id": stream_filter.id,
        "mode": stream_filter.mode,
        "name": stream_filter.name,
        "payload": stream_filter.payload,
    }


def _canonical_association(association: KeitaroOfferAssociation) -> dict[str, Any]:
    return {
        "id": association.id,
        "offer_id": association.offer_id,
        "share": _number(association.share),
        "state": association.state,
    }


def _canonical_stream(stream: KeitaroStream) -> dict[str, Any]:
    return {
        "action": {
            "options": stream.action_options,
            "payload": stream.action_payload,
            "type": stream.action_type,
        },
        "filters": [
            _canonical_filter(stream_filter)
            for stream_filter in sorted(
                stream.filters,
                key=lambda f: (f.id if f.id is not None else -1, f.name),
            )
        ],
        "id": stream.id,
        "offers": [
            _canonical_association(association)
            for association in sorted(stream.offers, key=lambda a: a.id)
        ],
        "position": stream.position,
        "schema": stream.schema_,
        "state": stream.state,
        "type": stream.type,
        "updated_at": stream.updated_at,
        "weight": _number(stream.weight),
    }


def build_canonical_document(
    campaign: KeitaroCampaign, streams: Sequence[KeitaroStream]
) -> dict[str, Any]:
    """Project managed remote state into the canonical document."""
    return {
        "campaign": {"id": campaign.id, "updated_at": campaign.updated_at},
        "streams": [_canonical_stream(stream) for stream in sorted(streams, key=lambda s: s.id)],
    }


def canonical_json(document: Mapping[str, Any]) -> str:
    """Deterministic JSON: sorted keys, fixed separators, stable encoding."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint_canonical(document: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of the canonical JSON representation."""
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def fingerprint_remote(campaign: KeitaroCampaign, streams: Sequence[KeitaroStream]) -> str:
    """Fingerprint freshly fetched remote state in one call."""
    return fingerprint_canonical(build_canonical_document(campaign, streams))
