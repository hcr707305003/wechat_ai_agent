from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _WorkItem:
    factory: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]
    item_id: str | None = None


_STOP = object()


class SessionTaskQueue:
    def __init__(self, max_concurrency: int = 3) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._queues: dict[str, asyncio.Queue[_WorkItem | object]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._closed = False

    async def submit(
        self,
        session_id: str,
        factory: Callable[[], Awaitable[Any]],
        *,
        item_id: str | None = None,
    ) -> Any:
        if self._closed:
            raise RuntimeError("Session task queue is closed")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        queue = self._queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[session_id] = queue
            self._workers[session_id] = asyncio.create_task(
                self._worker(session_id, queue), name=f"session-worker-{session_id}"
            )
        await queue.put(_WorkItem(factory, future, item_id))
        return await future

    def queued_item_ids(self, session_id: str) -> tuple[str, ...]:
        """Return IDs still waiting in a session queue (never the active item)."""
        queue = self._queues.get(session_id)
        if queue is None:
            return ()
        return tuple(
            item.item_id
            for item in tuple(queue._queue)
            if isinstance(item, _WorkItem) and item.item_id
        )

    def remove_queued(self, session_id: str, item_id: str) -> bool:
        """Cancel one queued item; active/in-flight work is intentionally untouched."""
        queue = self._queues.get(session_id)
        if queue is None:
            return False
        retained: list[_WorkItem | object] = []
        removed = False
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if (
                not removed
                and isinstance(item, _WorkItem)
                and item.item_id == item_id
            ):
                removed = True
                if not item.future.done():
                    item.future.cancel()
            else:
                retained.append(item)
        for item in retained:
            queue.put_nowait(item)
        return removed

    def clear_queued(self, session_id: str) -> tuple[str, ...]:
        """Cancel all waiting items and return their IDs; active work remains running."""
        queue = self._queues.get(session_id)
        if queue is None:
            return ()
        removed: list[str] = []
        retained: list[_WorkItem | object] = []
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, _WorkItem):
                if item.item_id:
                    removed.append(item.item_id)
                if not item.future.done():
                    item.future.cancel()
            else:
                retained.append(item)
        for item in retained:
            queue.put_nowait(item)
        return tuple(removed)

    async def cancel(self, session_id: str) -> bool:
        active = self._active.get(session_id)
        if active is None or active.done():
            return False
        active.cancel()
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for active in self._active.values():
            if not active.done():
                active.cancel()
        for queue in self._queues.values():
            # Pending work only lives in memory.  Drain it before waking the
            # worker so a shutdown/restart cannot execute an old reply later.
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if isinstance(item, _WorkItem) and not item.future.done():
                    item.future.cancel()
            await queue.put(_STOP)
        if self._workers:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)

    async def _worker(
        self, session_id: str, queue: asyncio.Queue[_WorkItem | object]
    ) -> None:
        while True:
            item = await queue.get()
            if item is _STOP:
                return
            assert isinstance(item, _WorkItem)
            if item.future.cancelled():
                continue
            try:
                async with self._semaphore:
                    # A queued item may have been taken by this worker while
                    # close() was waiting for the semaphore.  It is still a
                    # pending in-memory item and must be discarded as well.
                    if self._closed:
                        if not item.future.done():
                            item.future.cancel()
                        continue
                    task = asyncio.create_task(item.factory())
                    self._active[session_id] = task
                    result = await task
            except asyncio.CancelledError:
                if not item.future.done():
                    item.future.cancel()
            except Exception as exc:  # noqa: BLE001 - propagate arbitrary job failures
                if not item.future.done():
                    item.future.set_exception(exc)
            else:
                if not item.future.done():
                    item.future.set_result(result)
            finally:
                self._active.pop(session_id, None)
