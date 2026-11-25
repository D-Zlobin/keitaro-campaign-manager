"""Stream share calculation per specification section 6.7.

Definitions: ``A`` active streams; ``P`` sum of shares of active pinned
streams; ``U`` active unpinned streams sorted by position then Keitaro
stream ID; ``R = 100 - P``. Disabled streams always have share 0 and are
excluded from ``A``, ``P``, and ``U``. After every mutation active shares
total exactly 100; the remainder of ``R`` is distributed equally over ``U``
with deterministic rounding (see :mod:`app.domain.distribution`).

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


def _active(streams: Sequence[StreamEntry]) -> list[StreamEntry]:
    return [s for s in streams if s.state is StreamState.ACTIVE]


def _unpinned_sorted(streams: Sequence[StreamEntry]) -> list[StreamEntry]:
    return sorted(
        (s for s in streams if s.state is StreamState.ACTIVE and not s.pinned),
        key=lambda s: (s.position, s.keitaro_id),
    )


def _pinned_total(streams: Sequence[StreamEntry]) -> int:
    return sum(s.share for s in streams if s.state is StreamState.ACTIVE and s.pinned)


def _require_stream(streams: Sequence[StreamEntry], keitaro_id: int) -> StreamEntry:
    for stream in streams:
        if stream.keitaro_id == keitaro_id:
            return stream
    raise StreamNotFoundError(keitaro_id)


def recalculate_shares(streams: Sequence[StreamEntry]) -> list[StreamEntry]:
    """Recompute every stream's share after a mutation.

    - Disabled streams: share 0, pin cleared.
    - Active pinned streams: share unchanged.
    - Active unpinned streams: ``R`` divided equally with deterministic
      rounding in ``U`` order (position, then Keitaro stream ID).

    Raises :class:`StreamRuleViolationError` when pinned shares exceed 100 or
    when no unpinned active stream remains while pinned shares total less
    than 100 (spec rule 5).
    """
    pinned_total = _pinned_total(streams)
    if pinned_total > TOTAL_SHARE:
        raise StreamRuleViolationError(
            f"Pinned shares total {pinned_total}, exceeding {TOTAL_SHARE}"
        )
    unpinned = _unpinned_sorted(streams)
    available = TOTAL_SHARE - pinned_total
    if not unpinned and pinned_total != TOTAL_SHARE:
        raise StreamRuleViolationError(
            "No unpinned active stream remains while pinned shares total "
            f"{pinned_total} (< {TOTAL_SHARE})"
        )
    assigned = {
        stream.keitaro_id: share
        for stream, share in zip(
            unpinned, distribute_equal_shares(available, len(unpinned)), strict=True
        )
    }
    result: list[StreamEntry] = []
    for stream in streams:
        if stream.state is StreamState.DISABLED:
            result.append(replace(stream, share=0, pinned=False))
        elif stream.pinned:
            result.append(stream)
        else:
            result.append(replace(stream, share=assigned[stream.keitaro_id]))
    return result


def validate_share_invariants(streams: Sequence[StreamEntry]) -> None:
    """Validate an arbitrary draft (used before upload).

    Checks spec rules 1, 2, 5 and 6 on the given state without recalculating.
    """
    active = _active(streams)
    for stream in streams:
        if stream.state is StreamState.DISABLED and (stream.share != 0 or stream.pinned):
            raise StreamRuleViolationError(
                f"Disabled stream {stream.keitaro_id} must have share 0 and no pin"
            )
    total = sum(s.share for s in active)
    if total != TOTAL_SHARE:
        raise StreamRuleViolationError(
            f"Active stream shares total {total}, expected {TOTAL_SHARE}"
        )
    pinned_total = _pinned_total(streams)
    if pinned_total > TOTAL_SHARE:
        raise StreamRuleViolationError(
            f"Pinned shares total {pinned_total}, exceeding {TOTAL_SHARE}"
        )
    if not _unpinned_sorted(streams) and pinned_total != TOTAL_SHARE:
        raise StreamRuleViolationError(
            f"No unpinned active stream while pinned shares total {pinned_total} (< {TOTAL_SHARE})"
        )


def delete_stream(streams: Sequence[StreamEntry], keitaro_id: int) -> list[StreamEntry]:
    """Soft-delete a stream: disabled, share 0, pin cleared, then recalculate.

    Rejected (raising, without mutating anything) when the target is already
    disabled or when disabling it would leave no unpinned active stream while
    pinned shares total less than 100.
    """
    target = _require_stream(streams, keitaro_id)
    if target.state is StreamState.DISABLED:
        raise StreamRuleViolationError(f"Stream {keitaro_id} is already disabled")
    updated = [
        replace(stream, state=StreamState.DISABLED, share=0, pinned=False)
        if stream.keitaro_id == keitaro_id
        else stream
        for stream in streams
    ]
    return recalculate_shares(updated)


def restore_stream(streams: Sequence[StreamEntry], keitaro_id: int) -> list[StreamEntry]:
    """Restore a disabled stream: active, unpinned, share recalculated."""
    target = _require_stream(streams, keitaro_id)
    if target.state is StreamState.ACTIVE:
        raise StreamRuleViolationError(f"Stream {keitaro_id} is already active")
    updated = [
        replace(stream, state=StreamState.ACTIVE, pinned=False)
        if stream.keitaro_id == keitaro_id
        else stream
        for stream in streams
    ]
    return recalculate_shares(updated)


def pin_stream(streams: Sequence[StreamEntry], keitaro_id: int) -> list[StreamEntry]:
    """Pin an active stream at its current share; redistribute the remainder."""
    target = _require_stream(streams, keitaro_id)
    if target.state is StreamState.DISABLED:
        raise StreamRuleViolationError(f"Disabled stream {keitaro_id} cannot be pinned")
    if target.pinned:
        raise StreamRuleViolationError(f"Stream {keitaro_id} is already pinned")
    updated = [
        replace(stream, pinned=True) if stream.keitaro_id == keitaro_id else stream
        for stream in streams
    ]
    return recalculate_shares(updated)


def unpin_stream(streams: Sequence[StreamEntry], keitaro_id: int) -> list[StreamEntry]:
    """Unpin a stream; it becomes eligible for equal redistribution."""
    target = _require_stream(streams, keitaro_id)
    if not target.pinned:
        raise StreamRuleViolationError(f"Stream {keitaro_id} is not pinned")
    updated = [
        replace(stream, pinned=False) if stream.keitaro_id == keitaro_id else stream
        for stream in streams
    ]
    return recalculate_shares(updated)
