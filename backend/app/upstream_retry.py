"""Bounded retries for safe upstream reads (plan stage 3).

Retries apply only to safe read methods and only for timeouts, HTTP 429,
and transient 5xx responses, with bounded exponential backoff. Writes are
never retried blindly — the caller's idempotency/confirmation machinery owns
write recovery. The sleeper is injectable so tests do not wait in real time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
SAFE_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """``max_retries`` additional attempts after the first; total attempts
    are ``max_retries + 1``. Delays follow ``backoff * 2 ** (n - 1)``."""

    max_retries: int = 2
    backoff_seconds: float = 0.5
    sleeper: Callable[[float], None] = field(default=time.sleep)

    def delay_for(self, retry_number: int) -> float:
        """Delay before retry ``retry_number`` (1-based)."""
        return self.backoff_seconds * (2 ** (retry_number - 1))

    def allows_retry(self, *, method: str, retry_number: int) -> bool:
        """Whether ``retry_number`` (1-based) may be attempted."""
        return method.upper() in SAFE_READ_METHODS and retry_number <= self.max_retries

    def is_retryable_status(self, status_code: int) -> bool:
        return status_code in RETRYABLE_STATUS_CODES


DEFAULT_RETRY_POLICY = RetryPolicy()
NO_RETRY = RetryPolicy(max_retries=0)
