"""In-process per-campaign upload locks (plan stage 1).

Deployment is a single backend process, so one ``asyncio.Lock`` per campaign
coordinates active upload work. The lock only covers live operations; the
durable ``sync_state`` on the campaign row (including ``needs_attention``) is
what survives failures and restarts and blocks unsafe uploads afterwards.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class UploadLockManager:
    """Serializes uploads per campaign; different campaigns never block each
    other. Locks are bound to the event loop they are first used on."""

    def __init__(self) -> None:
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, campaign_id: uuid.UUID) -> AsyncIterator[None]:
        lock = await self._lock_for(campaign_id)
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    async def _lock_for(self, campaign_id: uuid.UUID) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(campaign_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[campaign_id] = lock
            return lock

    def is_locked(self, campaign_id: uuid.UUID) -> bool:
        lock = self._locks.get(campaign_id)
        return lock is not None and lock.locked()
