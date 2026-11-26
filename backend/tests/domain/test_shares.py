"""Stream share recalculation per specification section 6.7 (table-driven).

Stream shares are always redistributed equally over the active streams;
pinning exists only at the offer level (see ``test_offers.py``).
"""

from __future__ import annotations

import pytest

from app.domain.entries import StreamEntry
from app.domain.errors import StreamNotFoundError, StreamRuleViolationError
from app.domain.shares import (
    delete_stream,
    recalculate_shares,
    restore_stream,
    validate_share_invariants,
)
from app.domain.states import StreamState


def s(
    keitaro_id: int,
    position: int | None = None,
    *,
    state: StreamState = StreamState.ACTIVE,
    share: int = 0,
    offer_capable: bool = False,
) -> StreamEntry:
    return StreamEntry(
        keitaro_id=keitaro_id,
        position=keitaro_id if position is None else position,
        state=state,
        share=share,
        offer_capable=offer_capable,
    )


def shares_by_id(streams: list[StreamEntry]) -> dict[int, int]:
    return {stream.keitaro_id: stream.share for stream in streams}


# Input shares are irrelevant for active streams (always recomputed).
RECALCULATE_CASES = [
    pytest.param(
        [s(1, share=0), s(2, share=0), s(3, share=0)],
        {1: 34, 2: 33, 3: 33},
        id="three-active",
    ),
    pytest.param(
        [s(1, share=90), s(2, share=5), s(3, share=5)],
        {1: 34, 2: 33, 3: 33},
        id="skewed-inputs-equalized",
    ),
    pytest.param(
        [s(1, share=100), s(2)],
        {1: 50, 2: 50},
        id="single-share-split-in-half",
    ),
    pytest.param(
        [
            s(1, state=StreamState.DISABLED, share=77),
            s(2),
            s(3),
        ],
        {1: 0, 2: 50, 3: 50},
        id="disabled-excluded-share-zeroed",
    ),
    pytest.param(
        [s(10, position=3), s(20, position=1), s(30, position=2)],
        {10: 33, 20: 34, 30: 33},
        id="rounding-order-by-position",
    ),
    pytest.param(
        [s(20, position=1), s(10, position=1), s(30, position=1)],
        {10: 34, 20: 33, 30: 33},
        id="rounding-order-tie-broken-by-id",
    ),
]


@pytest.mark.parametrize(("streams", "expected"), RECALCULATE_CASES)
def test_recalculate_shares(streams: list[StreamEntry], expected: dict[int, int]) -> None:
    result = recalculate_shares(streams)
    assert shares_by_id(result) == expected
    active_total = sum(stream.share for stream in result if stream.state is StreamState.ACTIVE)
    assert active_total == 100


# Operations -----------------------------------------------------------------


def test_delete_redistributes_remaining_streams() -> None:
    result = delete_stream([s(1, share=34), s(2, share=33), s(3, share=33)], 2)
    assert result[1].state is StreamState.DISABLED
    assert shares_by_id(result) == {1: 50, 2: 0, 3: 50}


def test_delete_down_to_one_stream_gives_it_100() -> None:
    result = delete_stream([s(1, share=50), s(2, share=50)], 2)
    assert shares_by_id(result) == {1: 100, 2: 0}


def test_delete_already_disabled_rejected() -> None:
    with pytest.raises(StreamRuleViolationError):
        delete_stream([s(1, state=StreamState.DISABLED)], 1)


def test_delete_unknown_stream_rejected() -> None:
    with pytest.raises(StreamNotFoundError):
        delete_stream([s(1)], 999)


def test_restore_splits_equally_with_other_active() -> None:
    result = restore_stream([s(1, share=100), s(2, state=StreamState.DISABLED)], 2)
    assert result[1].state is StreamState.ACTIVE
    assert shares_by_id(result) == {1: 50, 2: 50}


def test_restore_into_three_way_split() -> None:
    result = restore_stream([s(1, share=50), s(2, share=50), s(3, state=StreamState.DISABLED)], 3)
    assert shares_by_id(result) == {1: 34, 2: 33, 3: 33}


def test_restore_already_active_rejected() -> None:
    with pytest.raises(StreamRuleViolationError):
        restore_stream([s(1)], 1)


# Invariant validation (used before upload) ----------------------------------


def test_validate_accepts_well_formed_draft() -> None:
    validate_share_invariants([s(1, share=70), s(2, share=30), s(3, state=StreamState.DISABLED)])


@pytest.mark.parametrize(
    "streams",
    [
        [s(1, share=60), s(2, share=30)],  # total 90
        [s(1, share=60), s(2, share=50)],  # total 110
        [s(1, state=StreamState.DISABLED, share=5), s(2, share=100)],  # disabled share
    ],
)
def test_validate_rejects_broken_invariants(streams: list[StreamEntry]) -> None:
    with pytest.raises(StreamRuleViolationError):
        validate_share_invariants(streams)
