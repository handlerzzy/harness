from __future__ import annotations

import asyncio

import pytest

from pi_agent_core import AbortController, EventStream


@pytest.mark.asyncio
async def test_abort_signal_native_aliases_reason_and_listener_removal() -> None:
    controller = AbortController()
    events: list[object] = []

    def removed(_event: object) -> None:
        events.append("removed")

    controller.signal.addEventListener("abort", removed)
    controller.signal.removeEventListener("abort", removed)
    controller.signal.addEventListener("abort", lambda event: events.append(event["type"]))
    controller.signal.onabort = lambda: events.append("onabort")
    reason = RuntimeError("stop")
    controller.abort(reason)
    controller.abort(RuntimeError("ignored"))

    assert controller.signal.aborted
    assert controller.signal.reason is reason
    assert events == ["abort", "onabort"]
    with pytest.raises(RuntimeError, match="stop"):
        controller.signal.throwIfAborted()
    controller.signal.addEventListener("abort", lambda: events.append("late"))
    assert "late" not in events
    await controller.signal.wait()


@pytest.mark.asyncio
async def test_terminal_event_is_delivered_and_late_iterators_finish() -> None:
    stream: EventStream[dict[str, object], object] = EventStream(
        lambda event: event.get("type") == "done",
        lambda event: event["value"],
    )
    stream.push({"type": "data", "value": 1})
    stream.push({"type": "done", "value": 2})
    stream.push({"type": "ignored", "value": 3})

    assert [event["type"] async for event in stream] == ["data", "done"]
    assert await stream.result() == 2
    assert [event async for event in stream] == []


@pytest.mark.asyncio
async def test_end_can_resolve_an_explicit_none_result() -> None:
    stream: EventStream[str, None] = EventStream(lambda _event: False, lambda _event: None)
    stream.end(None)
    assert await asyncio.wait_for(stream.result(), 0.1) is None


@pytest.mark.asyncio
async def test_end_releases_all_concurrent_waiters() -> None:
    stream: EventStream[str, str] = EventStream(lambda _event: False, lambda event: event)

    async def consume() -> list[str]:
        return [event async for event in stream]

    first = asyncio.create_task(consume())
    second = asyncio.create_task(consume())
    await asyncio.sleep(0)
    stream.end("result")

    assert await asyncio.wait_for(first, 0.1) == []
    assert await asyncio.wait_for(second, 0.1) == []
    assert await stream.result() == "result"


@pytest.mark.asyncio
async def test_set_exception_releases_iteration_and_result_raises() -> None:
    stream: EventStream[str, str] = EventStream(lambda _event: False, lambda event: event)
    stream.set_exception(RuntimeError("boom"))
    assert [event async for event in stream] == []
    with pytest.raises(RuntimeError, match="boom"):
        await stream.result()
