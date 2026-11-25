"""Deterministic integer distribution (spec section 6.7, rule 3)."""

from __future__ import annotations

import pytest

from app.domain.distribution import distribute_equal_shares


@pytest.mark.parametrize(
    ("total", "count", "expected"),
    [
        (100, 1, [100]),
        (100, 2, [50, 50]),
        (100, 3, [34, 33, 33]),
        (100, 4, [25, 25, 25, 25]),
        (100, 7, [15, 15, 14, 14, 14, 14, 14]),
        (30, 2, [15, 15]),
        (70, 2, [35, 35]),
        (3, 3, [1, 1, 1]),
        (2, 4, [1, 1, 0, 0]),
        (0, 3, [0, 0, 0]),
        (0, 1, [0]),
    ],
)
def test_distribution_splits_total_with_remainder_first(
    total: int, count: int, expected: list[int]
) -> None:
    assert distribute_equal_shares(total, count) == expected
    assert sum(expected) == total
    assert all(share >= 0 for share in expected)


def test_distribution_zero_count_yields_empty() -> None:
    assert distribute_equal_shares(0, 0) == []


@pytest.mark.parametrize(("total", "count"), [(-1, 2), (100, -1)])
def test_distribution_rejects_negatives(total: int, count: int) -> None:
    with pytest.raises(ValueError):
        distribute_equal_shares(total, count)
