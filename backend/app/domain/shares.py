"""Stream share calculation per specification section 6.7.

Definitions: ``A`` active streams, sorted by position then Keitaro stream ID.
Disabled streams always have share 0 and are excluded from ``A``. After
every mutation active shares total exactly 100, divided equally over ``A``
with deterministic rounding (see :mod:`app.domain.distribution`). Pinning
exists only at the offer level (spec section 6.8); stream shares are always
redistributed equally.

All functions are pure: they return new entry lists and raise
:class:`~app.domain.errors.StreamRuleViolationError` instead of producing an
invalid state, so a rejected operation can never mutate the caller's draft.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from app.domain.distribution import distribute_equal_shares
from app.domain.entries import StreamEntry
from app.domain.errors import StreamNotFoundError, StreamRuleViolationError
from app.domain.states import StreamState

TOTAL_SHARE = 100


def _active_sorted(streams: Sequence[StreamEntry]) -> list[StreamEntry]:
    return sorted(
        (s for s in streams if s.state is StreamState.ACTIVE),
        key=lambda s: (s.position, s.keitaro_id),
    )


def _require_stream(streams: Sequence[StreamEntry], keitaro_id: int) -> StreamEntry:
    for stream in streams:
        if stream.keitaro_id == keitaro_id:
            return stream
    raise StreamNotFoundError(keitaro_id)


def recalculate_shares(streams: Sequence[StreamEntry]) -> list[StreamEntry]:
    """Recompute every stream's share after a mutation.

    Disabled streams get share 0; active streams divide 100 equally with
    deterministic rounding in ``A`` order (position, then Keitaro stream ID).
    """
    active = _active_sorted(streams)
    assigned = {
        stream.keitaro_id: share
        for stream, share in zip(
            active, distribute_equal_shares(TOTAL_SHARE, len(active)), strict=True
        )
    }
    return [
        replace(stream, share=0)
        if stream.state is StreamState.DISABLED
        else replace(stream, share=assigned[stream.keitaro_id])
        for stream in streams
    ]


def validate_share_invariants(streams: Sequence[StreamEntry]) -> None:
    """Validate an arbitrary draft (used before upload).

    Checks spec rules 1 and 6 on the given state without recalculating.
    """
    for stream in streams:
        if stream.state is StreamState.DISABLED and stream.share != 0:
            raise StreamRuleViolationError(f"Disabled stream {stream.keitaro_id} must have share 0")
    total = sum(s.share for s in streams if s.state is StreamState.ACTIVE)
    if total != TOTAL_SHARE:
        raise StreamRuleViolationError(
            f"Active stream shares total {total}, expected {TOTAL_SHARE}"
        )


def delete_stream(streams: Sequence[StreamEntry], keitaro_id: int) -> list[StreamEntry]:
    """Soft-delete a stream: disabled, share 0, then recalculate.

    Rejected (raising, without mutating anything) when the target is already
    disabled.
    """
    target = _require_stream(streams, keitaro_id)
    if target.state is StreamState.DISABLED:
        raise StreamRuleViolationError(f"Stream {keitaro_id} is already disabled")
    updated = [
        replace(stream, state=StreamState.DISABLED, share=0)
        if stream.keitaro_id == keitaro_id
        else stream
        for stream in streams
    ]
    return recalculate_shares(updated)


def restore_stream(streams: Sequence[StreamEntry], keitaro_id: int) -> list[StreamEntry]:
    """Restore a disabled stream: active, share recalculated."""
    target = _require_stream(streams, keitaro_id)
    if target.state is StreamState.ACTIVE:
        raise StreamRuleViolationError(f"Stream {keitaro_id} is already active")
    updated = [
        replace(stream, state=StreamState.ACTIVE) if stream.keitaro_id == keitaro_id else stream
        for stream in streams
    ]
    return recalculate_shares(updated)
