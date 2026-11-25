"""Stream share recalculation per specification section 6.7 (table-driven)."""

from __future__ import annotations

import pytest

from app.domain.entries import StreamEntry
from app.domain.errors import StreamNotFoundError, StreamRuleViolationError
from app.domain.shares import (
    delete_stream,
    pin_stream,
    recalculate_shares,
    restore_stream,
    unpin_stream,
    validate_share_invariants,
)
from app.domain.states import StreamState


def s(
    keitaro_id: int,
    position: int | None = None,
    *,
    state: StreamState = StreamState.ACTIVE,
    share: int = 0,
    pinned: bool = False,
    offer_capable: bool = False,
) -> StreamEntry:
    return StreamEntry(
        keitaro_id=keitaro_id,
        position=keitaro_id if position is None else position,
        state=state,
        share=share,
        pinned=pinned,
        offer_capable=offer_capable,
    )


def shares_by_id(streams: list[StreamEntry]) -> dict[int, int]:
    return {stream.keitaro_id: stream.share for stream in streams}


def flags_by_id(streams: list[StreamEntry]) -> dict[int, tuple[str, bool]]:
    return {stream.keitaro_id: (stream.state.value, stream.pinned) for stream in streams}


# Every example from specification section 6.7, plus zero-share and
# disabled-stream cases. Input shares are irrelevant for unpinned streams
# (always recomputed); pinned shares are taken as recorded.
RECALCULATE_CASES = [
    pytest.param(
        [s(1, share=0), s(2, share=0), s(3, share=0)],
        {1: 34, 2: 33, 3: 33},
        id="three-unpinned",
    ),
    pytest.param(
        [s(1, share=40, pinned=True), s(2, share=11), s(3, share=11)],
        {1: 40, 2: 30, 3: 30},
        id="first-pinned-keeps-share",
    ),
    pytest.param(
        [s(1, share=40, pinned=True), s(2, share=30, pinned=True), s(3, share=99)],
        {1: 40, 2: 30, 3: 30},
        id="two-pinned-remainder-to-one",
    ),
    pytest.param(
        [s(1, share=40, pinned=True), s(2, share=30, pinned=True), s(3), s(4)],
        {1: 40, 2: 30, 3: 15, 4: 15},
        id="pinned-70-two-unpinned-get-15",
    ),
    pytest.param(
        [s(1, share=100, pinned=True), s(2)],
        {1: 100, 2: 0},
        id="zero-share-for-exhausted-remainder",
    ),
    pytest.param(
        [
            s(1, state=StreamState.DISABLED, share=77, pinned=True),
            s(2),
            s(3),
        ],
        {1: 0, 2: 50, 3: 50},
        id="disabled-excluded-share-zeroed-pin-cleared",
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


def test_disabled_stream_pin_is_cleared() -> None:
    result = recalculate_shares(
        [s(1, state=StreamState.DISABLED, share=5, pinned=True), s(2, share=100)]
    )
    assert flags_by_id(result)[1] == ("disabled", False)
    assert shares_by_id(result)[1] == 0


def test_recalculate_rejects_pinned_total_over_100() -> None:
    with pytest.raises(StreamRuleViolationError):
        recalculate_shares([s(1, share=60, pinned=True), s(2, share=60, pinned=True)])


# Operations -----------------------------------------------------------------


def test_delete_redistributes_remaining_unpinned() -> None:
    result = delete_stream([s(1, share=34), s(2, share=33), s(3, share=33)], 2)
    assert flags_by_id(result)[2] == ("disabled", False)
    assert shares_by_id(result) == {1: 50, 2: 0, 3: 50}


def test_delete_pinned_stream_frees_its_share() -> None:
    result = delete_stream([s(1, share=100, pinned=True), s(2, share=0)], 1)
    assert shares_by_id(result) == {1: 0, 2: 100}
    assert flags_by_id(result)[1] == ("disabled", False)


@pytest.mark.parametrize(
    "streams",
    [
        [s(1, share=50, pinned=True), s(2, share=50)],  # P=50, deleting last unpinned
        [s(1, share=50)],  # deleting the only stream
    ],
)
def test_delete_last_unpinned_stream_with_pinned_below_100_rejected(
    streams: list[StreamEntry],
) -> None:
    target = streams[-1].keitaro_id
    with pytest.raises(StreamRuleViolationError):
        delete_stream(streams, target)
    assert flags_by_id(streams)[target][0] == "active"  # input untouched


def test_delete_last_unpinned_with_pinned_at_100_allowed() -> None:
    result = delete_stream([s(1, share=100, pinned=True), s(2, share=0)], 2)
    assert shares_by_id(result)[1] == 100


def test_delete_already_disabled_rejected() -> None:
    with pytest.raises(StreamRuleViolationError):
        delete_stream([s(1, state=StreamState.DISABLED)], 1)


def test_delete_unknown_stream_rejected() -> None:
    with pytest.raises(StreamNotFoundError):
        delete_stream([s(1)], 999)


def test_restore_recalculates_unpinned() -> None:
    result = restore_stream([s(1, share=100, pinned=True), s(2, state=StreamState.DISABLED)], 2)
    assert flags_by_id(result)[2] == ("active", False)
    assert shares_by_id(result) == {1: 100, 2: 0}


def test_restore_splits_with_other_unpinned() -> None:
    result = restore_stream([s(1, share=100), s(2, state=StreamState.DISABLED)], 2)
    assert shares_by_id(result) == {1: 50, 2: 50}


def test_restore_already_active_rejected() -> None:
    with pytest.raises(StreamRuleViolationError):
        restore_stream([s(1)], 1)


def test_pin_keeps_current_share_and_redistributes() -> None:
    result = pin_stream([s(1, share=34), s(2, share=33), s(3, share=33)], 1)
    assert flags_by_id(result)[1] == ("active", True)
    assert shares_by_id(result) == {1: 34, 2: 33, 3: 33}


def test_pin_second_stream_leaves_final_unpinned_at_30() -> None:
    streams = [s(1, share=40, pinned=True), s(2, share=30), s(3, share=30)]
    result = pin_stream(streams, 2)
    assert shares_by_id(result) == {1: 40, 2: 30, 3: 30}


@pytest.mark.parametrize(
    "streams",
    [
        [s(1, share=50, pinned=True), s(2, state=StreamState.DISABLED)],
        [s(1, share=50, pinned=True)],
    ],
)
def test_pin_disabled_stream_rejected(streams: list[StreamEntry]) -> None:
    with pytest.raises(StreamRuleViolationError):
        pin_stream(streams, streams[-1].keitaro_id)


def test_pin_already_pinned_rejected() -> None:
    with pytest.raises(StreamRuleViolationError):
        pin_stream([s(1, share=100, pinned=True)], 1)


def test_unpin_makes_stream_eligible_for_split() -> None:
    result = unpin_stream([s(1, share=40, pinned=True), s(2, share=30), s(3, share=30)], 1)
    assert shares_by_id(result) == {1: 34, 2: 33, 3: 33}


def test_unpin_redistributes_the_freed_share() -> None:
    result = unpin_stream([s(1, share=60, pinned=True), s(2, share=40, pinned=True)], 1)
    assert flags_by_id(result)[1] == ("active", False)
    assert shares_by_id(result) == {1: 60, 2: 40}


def test_unpin_only_gets_zero_when_pinned_still_total_100() -> None:
    result = unpin_stream([s(1, share=0, pinned=True), s(2, share=100, pinned=True)], 1)
    assert shares_by_id(result) == {1: 0, 2: 100}


def test_unpin_unpinned_stream_rejected() -> None:
    with pytest.raises(StreamRuleViolationError):
        unpin_stream([s(1)], 1)


# Invariant validation (used before upload) ----------------------------------


def test_validate_accepts_well_formed_draft() -> None:
    validate_share_invariants(
        [s(1, share=70, pinned=True), s(2, share=30), s(3, state=StreamState.DISABLED)]
    )


@pytest.mark.parametrize(
    "streams",
    [
        [s(1, share=60), s(2, share=30)],  # total 90
        [s(1, share=60), s(2, share=50)],  # total 110
        [s(1, share=70, pinned=True), s(2, share=20)],  # total 90
        [s(1, share=60, pinned=True), s(2, share=60)],  # pinned alone exceeds
        [s(1, share=70, pinned=True)],  # no unpinned, P < 100
        [s(1, state=StreamState.DISABLED, share=5), s(2, share=100)],  # disabled share
        [s(1, state=StreamState.DISABLED, pinned=True), s(2, share=100)],  # disabled pin
    ],
)
def test_validate_rejects_broken_invariants(streams: list[StreamEntry]) -> None:
    with pytest.raises(StreamRuleViolationError):
        validate_share_invariants(streams)
