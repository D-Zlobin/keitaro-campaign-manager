"""Per-campaign upload lock: serialization and isolation."""

from __future__ import annotations

import asyncio
import uuid

from app.services.locks import UploadLockManager


def _run(coro) -> None:
    asyncio.run(coro)


def test_same_campaign_serializes() -> None:
    async def main() -> None:
        manager = UploadLockManager()
        campaign_id = uuid.uuid4()
        order: list[str] = []

        async def worker(name: str) -> None:
            async with manager.acquire(campaign_id):
                order.append(f"{name}:in")
                await asyncio.sleep(0.01)
                order.append(f"{name}:out")

        await asyncio.gather(worker("a"), worker("b"))
        assert order == ["a:in", "a:out", "b:in", "b:out"] or order == [
            "b:in",
            "b:out",
            "a:in",
            "a:out",
        ]

    _run(main())


def test_different_campaigns_do_not_block() -> None:
    async def main() -> None:
        manager = UploadLockManager()
        first = uuid.uuid4()
        second = uuid.uuid4()
        order: list[str] = []

        async def worker(campaign_id: uuid.UUID, name: str) -> None:
            async with manager.acquire(campaign_id):
                order.append(f"{name}:in")
                await asyncio.sleep(0.02)
                order.append(f"{name}:out")

        await asyncio.gather(worker(first, "a"), worker(second, "b"))
        # Both workers held their locks concurrently: each entered before the
        # other left.
        assert order.index("a:in") < order.index("b:out")
        assert order.index("b:in") < order.index("a:out")

    _run(main())


def test_lock_released_on_exception() -> None:
    async def main() -> None:
        manager = UploadLockManager()
        campaign_id = uuid.uuid4()

        async def failing() -> None:
            async with manager.acquire(campaign_id):
                raise RuntimeError("boom")

        try:
            await failing()
        except RuntimeError:
            pass
        assert not manager.is_locked(campaign_id)

        # Re-acquisition succeeds immediately.
        async with manager.acquire(campaign_id):
            assert manager.is_locked(campaign_id)

    _run(main())


def test_is_locked_reflects_state() -> None:
    async def main() -> None:
        manager = UploadLockManager()
        campaign_id = uuid.uuid4()
        assert not manager.is_locked(campaign_id)
        async with manager.acquire(campaign_id):
            assert manager.is_locked(campaign_id)
        assert not manager.is_locked(campaign_id)

    _run(main())
