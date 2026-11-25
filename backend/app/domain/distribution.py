"""Deterministic integer distribution (spec section 6.7, rule 3).

The remainder is assigned one extra unit to the first items in the caller's
deterministic order (stream position then Keitaro stream ID; association ID
then offer ID). This module is the single implementation shared by stream
and offer recalculation.
"""

from __future__ import annotations


def distribute_equal_shares(total: int, count: int) -> list[int]:
    """Split ``total`` into ``count`` integer shares totaling exactly ``total``.

    ``base = total // count``, ``remainder = total % count``; the first
    ``remainder`` shares receive ``base + 1``, the rest ``base``.

    Raises ``ValueError`` for negative inputs. A zero count yields an empty
    list; callers that require a non-empty result (spec rule 5) validate
    before calling.
    """
    if total < 0:
        raise ValueError(f"total must be non-negative, got {total}")
    if count < 0:
        raise ValueError(f"count must be non-negative, got {count}")
    if count == 0:
        return []
    base, remainder = divmod(total, count)
    return [base + 1 if index < remainder else base for index in range(count)]
