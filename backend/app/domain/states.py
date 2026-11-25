"""Shared state enums for the domain layer.

Both the persistence models (``app.db.models``) and the pure domain
functions (``app.domain``) use these enums. String values (not member names)
are persisted — CHECK constraints and JSON payloads both use the lowercase
values, matching Keitaro's own ``state`` vocabulary.
"""

from __future__ import annotations

import enum


class StreamState(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class OfferAssociationState(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class SyncState(enum.StrEnum):
    """Synchronization state of one campaign; ``needs_attention`` persists
    across restarts and blocks further uploads until a recovery fetch."""

    CLEAN = "clean"
    DIRTY = "dirty"
    SYNCING = "syncing"
    NEEDS_ATTENTION = "needs_attention"


class BaselineSource(enum.StrEnum):
    CREATE = "create"
    FETCH = "fetch"
    UPLOAD = "upload"


class IdempotencyState(enum.StrEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def enum_values(enum_class: type[enum.Enum]) -> list[str]:
    """Values persisted for a str-enum (used by SAEnum values_callable)."""
    return [member.value for member in enum_class]


def enum_sql_values(enum_class: type[enum.Enum]) -> str:
    """Comma-separated quoted values for SQL CHECK constraints."""
    return ", ".join(f"'{member.value}'" for member in enum_class)
