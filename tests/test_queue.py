import asyncio

import pytest

from agent_bridge.runtime.queue import SessionTaskQueue


@pytest.mark.asyncio
async def test_queue_serializes_same_session_but_parallelizes_different_sessions() -> None:
    queue = SessionTaskQueue(max_concurrency=2)
    active_by_session: dict[str, int] = {}
    maximum_total = 0
    order: list[str] = []
    gate = asyncio.Event()

    async def work(session: str, label: str) -> str:
        nonlocal maximum_total
        active_by_session[session] = active_by_session.get(session, 0) + 1
        assert active_by_session[session] == 1
        maximum_total = max(maximum_total, sum(active_by_session.values()))
        order.append(f"start-{label}")
        await gate.wait()
        order.append(f"end-{label}")
        active_by_session[session] -= 1
        return label

    tasks = [
        asyncio.create_task(queue.submit("a", lambda: work("a", "a1"))),
        asyncio.create_task(queue.submit("a", lambda: work("a", "a2"))),
        asyncio.create_task(queue.submit("b", lambda: work("b", "b1"))),
    ]
    await asyncio.sleep(0.05)
    assert maximum_total == 2
    assert "start-a2" not in order
    gate.set()
    assert await asyncio.gather(*tasks) == ["a1", "a2", "b1"]
    assert order.index("end-a1") < order.index("start-a2")
    await queue.close()


@pytest.mark.asyncio
async def test_close_discards_pending_session_work() -> None:
    queue = SessionTaskQueue(max_concurrency=1)
    started = asyncio.Event()
    release = asyncio.Event()
    executed: list[str] = []

    async def first() -> str:
        executed.append("first")
        started.set()
        await release.wait()
        return "first"

    async def second() -> str:
        executed.append("second")
        return "second"

    first_task = asyncio.create_task(queue.submit("chat", first))
    await started.wait()
    second_task = asyncio.create_task(queue.submit("chat", second))
    await asyncio.sleep(0)

    await queue.close()
    results = await asyncio.gather(first_task, second_task, return_exceptions=True)

    assert executed == ["first"]
    assert all(isinstance(result, asyncio.CancelledError) for result in results)


@pytest.mark.asyncio
async def test_remove_queued_item_does_not_cancel_active_work() -> None:
    queue = SessionTaskQueue(max_concurrency=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def first() -> str:
        started.set()
        await release.wait()
        return "first"

    async def second() -> str:
        raise AssertionError("removed item must not execute")

    first_task = asyncio.create_task(queue.submit("chat", first, item_id="job-1"))
    await started.wait()
    second_task = asyncio.create_task(queue.submit("chat", second, item_id="job-2"))
    await asyncio.sleep(0)

    assert queue.queued_item_ids("chat") == ("job-2",)
    assert queue.remove_queued("chat", "job-1") is False
    assert queue.remove_queued("chat", "job-2") is True
    assert queue.queued_item_ids("chat") == ()

    release.set()
    assert await first_task == "first"
    result = (await asyncio.gather(second_task, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    await queue.close()


@pytest.mark.asyncio
async def test_clear_queued_items_keeps_active_item() -> None:
    queue = SessionTaskQueue(max_concurrency=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        started.set()
        await release.wait()

    async def pending() -> None:
        raise AssertionError("cleared item must not execute")

    active = asyncio.create_task(queue.submit("chat", first, item_id="job-1"))
    await started.wait()
    pending_task = asyncio.create_task(queue.submit("chat", pending, item_id="job-2"))
    await asyncio.sleep(0)

    assert queue.clear_queued("chat") == ("job-2",)
    release.set()
    await active
    result = (await asyncio.gather(pending_task, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    await queue.close()
