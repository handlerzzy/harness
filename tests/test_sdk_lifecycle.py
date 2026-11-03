from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pi_agent_core import AbortController, AgentTool, InMemorySessionStorage, Session

from .test_sdk import chunk, sdk, sse
from .test_sdk_tools import call, recorder, tool_chunk


@pytest.mark.parametrize("failure", [408, 429, 500, 502, 503, 504, "timeout", "connect", "disconnect"])
async def test_transient_request_failure_retries_only_the_request_after_a_completed_tool(
    tmp_path: Path, failure: int | str
) -> None:
    path = tmp_path / "effects.txt"
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=sse(
                    tool_chunk(call(0, '{"value":"once"}', call_id="write", name="record"), finish="tool_calls")
                ),
            )
        if len(requests) == 2:
            if isinstance(failure, int):
                return httpx.Response(failure, text="temporary failure")
            errors = {
                "timeout": httpx.ReadTimeout,
                "connect": httpx.ConnectError,
                "disconnect": httpx.RemoteProtocolError,
            }
            raise errors[failure]("temporary failure", request=request)
        return httpx.Response(200, content=sse(chunk("Recorded once.", finish="stop")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, tools=[recorder(path)])
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda event: events.append(copy.deepcopy(event)))
        result = await asyncio.wait_for(harness.prompt("record once"), 2)
        await asyncio.wait_for(harness.wait_for_idle(), 1)
        assert not client.is_closed

    assert result["stopReason"] == "stop"
    assert path.read_text() == "write:once\n"
    assert len(requests) == 3
    assert requests[1] == requests[2]
    assert requests[2]["messages"][-1] == {"role": "tool", "tool_call_id": "write", "content": "recorded once"}
    assert len([event for event in events if event["type"] == "tool_execution_end"]) == 1
    assert len([event for event in events if event["type"] == "agent_end"]) == 1
    assert events[-1]["type"] == "settled"


@pytest.mark.parametrize("stage", ["before_start", "before_request", "waiting", "generating", "backoff"])
async def test_cancellation_settles_without_starting_another_request(tmp_path: Path, stage: str) -> None:
    active = asyncio.Event()
    released = asyncio.Event()
    gate = asyncio.Event()
    requests: list[httpx.Request] = []

    class WaitingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(chunk("partial"), done=False)
            active.set()
            await asyncio.Event().wait()

        async def aclose(self):
            released.set()

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if stage == "backoff":
            return httpx.Response(503, text="retry later")
        if stage == "waiting":
            active.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()
        return httpx.Response(200, stream=WaitingBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client)
        events: list[dict[str, Any]] = []

        def observe(event):
            events.append(copy.deepcopy(event))
            if stage == "backoff" and event["type"] == "after_provider_response":
                active.set()

        async def before_request(event):
            active.set()
            await gate.wait()

        harness.subscribe(observe)
        if stage == "before_request":
            harness.on("before_provider_request", before_request)
        if stage == "before_start":
            harness.on("before_agent_start", before_request)
        task = asyncio.create_task(harness.prompt("go"))
        await asyncio.wait_for(active.wait(), 1)
        abort = asyncio.create_task(harness.abort())
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(abort, 1)
        result = await asyncio.wait_for(task, 1)
        await asyncio.wait_for(harness.wait_for_idle(), 1)
        assert not client.is_closed

    assert result["stopReason"] == "aborted"
    assert len(requests) == (0 if stage in {"before_start", "before_request"} else 1)
    if stage in {"waiting", "generating"}:
        assert released.is_set()
    assert len([event for event in events if event["type"] == "agent_end"]) == 1


@pytest.mark.parametrize("immediate", [False, True])
async def test_direct_model_cancellation_settles_events_and_final_result(tmp_path: Path, immediate: bool) -> None:
    controller = AbortController()
    active = asyncio.Event()
    released = asyncio.Event()
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        active.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client)
        stream = harness.models.stream_simple(harness.get_model(), {"messages": []}, {"signal": controller.signal})
        if not immediate:
            await asyncio.wait_for(active.wait(), 1)
        controller.abort()

        async def consume():
            return [event async for event in stream]

        events, result = await asyncio.wait_for(asyncio.gather(consume(), stream.result()), 1)

    assert result["stopReason"] == "aborted"
    assert events == [{"type": "error", "reason": "aborted", "error": result}]
    assert len(requests) == (0 if immediate else 1)
    assert released.is_set() is (not immediate)


async def test_cancellation_in_payload_hook_does_not_start_http_request(tmp_path: Path) -> None:
    controller = AbortController()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=sse(chunk("unexpected", finish="stop")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client)
        result = await asyncio.wait_for(
            harness.models.complete_simple(
                harness.get_model(),
                {"messages": []},
                {"signal": controller.signal, "onPayload": lambda _: controller.abort()},
            ),
            1,
        )

    assert result["stopReason"] == "aborted"
    assert not requests


async def test_cancelling_custom_tool_stops_the_run_and_skips_unstarted_tools(tmp_path: Path) -> None:
    active = asyncio.Event()
    released = asyncio.Event()
    path = tmp_path / "never-written.txt"
    requests: list[dict[str, Any]] = []

    async def wait(call_id, arguments, signal, on_update):
        active.set()
        try:
            await signal.wait()
            signal.throw_if_aborted()
        finally:
            released.set()

    tool = AgentTool(
        name="wait", label="Wait", description="Wait until cancelled", parameters={"type": "object"}, execute=wait
    )
    session = Session(InMemorySessionStorage())

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=sse(
                tool_chunk(
                    call(0, "{}", call_id="waiting", name="wait"),
                    call(1, '{"value":"bad"}', call_id="unstarted", name="record"),
                    finish="tool_calls",
                )
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, tools=[tool, recorder(path)], session=session)
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda event: events.append(copy.deepcopy(event)))
        # Force a sequential batch using the public tool scheduling contract.
        tool.execution_mode = "sequential"
        task = asyncio.create_task(harness.prompt("wait"))
        await asyncio.wait_for(active.wait(), 1)
        await asyncio.wait_for(harness.abort(), 1)
        result = await asyncio.wait_for(task, 1)
        await asyncio.wait_for(harness.wait_for_idle(), 1)
        context = await session.build_context()

    assert released.is_set()
    assert not path.exists()
    assert len(requests) == 1
    assert result["stopReason"] == "aborted"
    results = [message for message in context["messages"] if message["role"] == "toolResult"]
    assert {message["toolCallId"] for message in results} == {"waiting", "unstarted"}
    assert all(message["isError"] for message in results)
    starts = [event["toolCallId"] for event in events if event["type"] == "tool_execution_start"]
    assert starts == ["waiting"]
    assert len([event for event in events if event["type"] == "agent_end"]) == 1


async def test_parallel_preparation_cancellation_never_executes_prepared_tools(tmp_path: Path) -> None:
    active = asyncio.Event()
    gate = asyncio.Event()
    path = tmp_path / "never-written.txt"
    session = Session(InMemorySessionStorage())
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=sse(
                    tool_chunk(
                        *(call(index, '{"value":"bad"}', call_id=str(index), name="record") for index in range(3)),
                        finish="tool_calls",
                    )
                ),
            )
        return httpx.Response(200, content=sse(chunk("Continued.", finish="stop")))

    async def before_tool(event):
        if event["toolCallId"] == "1":
            active.set()
            await gate.wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, tools=[recorder(path)], session=session)
        harness.on("tool_call", before_tool)
        task = asyncio.create_task(harness.prompt("record"))
        await asyncio.wait_for(active.wait(), 1)
        abort = asyncio.create_task(harness.abort())
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(abort, 1)
        result = await asyncio.wait_for(task, 1)
        assert len(requests) == 1
        await asyncio.wait_for(harness.prompt("continue"), 1)

    assert result["stopReason"] == "aborted"
    assert not path.exists()
    results = [message for message in requests[1]["messages"] if message["role"] == "tool"]
    assert {message["tool_call_id"] for message in results} == {"0", "1", "2"}
    assert all("aborted" in message["content"] for message in results)


@pytest.mark.parametrize("options,attempts", [({"maxRetries": 0}, 1), ({"maxRetries": 1, "maxRetryDelayMs": 0}, 2)])
async def test_harness_retry_options_control_the_attempt_limit(
    tmp_path: Path, options: dict[str, Any], attempts: int
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client)
        await harness.set_stream_options(options)
        result = await asyncio.wait_for(harness.prompt("go"), 1)

    assert result["stopReason"] == "error"
    assert len(requests) == attempts


@pytest.mark.parametrize(
    "options",
    [
        {"maxRetries": -1},
        {"maxRetries": 1.5},
        {"maxRetryDelayMs": -1},
        {"maxRetryDelayMs": float("nan")},
        {"timeoutMs": -1},
        {"timeoutMs": float("inf")},
    ],
)
async def test_invalid_request_options_fail_without_http_requests(tmp_path: Path, options: dict[str, Any]) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=sse(chunk("unexpected", finish="stop")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client)
        await harness.set_stream_options(options)
        result = await asyncio.wait_for(harness.prompt("go"), 1)

    assert result["stopReason"] == "error"
    assert not requests


async def test_authentication_failure_is_not_retried_when_error_body_times_out(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    closed: list[bool] = []

    class StalledError(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self):
            closed.append(True)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, stream=StalledError())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, timeout_ms=10)
        result = await asyncio.wait_for(harness.prompt("go"), 1)

    assert result["stopReason"] == "error"
    assert len(requests) == 1
    assert closed == [True]


@pytest.mark.parametrize(
    "failure,attempts",
    [(503, 3), (401, 1), (403, 1), (400, 1), (404, 1), (422, 1), (501, 1), ("timeout", 3), ("stall", 3), ("url", 1)],
)
async def test_retry_exhaustion_and_permanent_errors_settle_stream_and_result(
    tmp_path: Path, failure: int | str, attempts: int
) -> None:
    requests: list[httpx.Request] = []
    released: list[bool] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(failure, int):
            return httpx.Response(failure, text="request failed")
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "url":
            raise httpx.UnsupportedProtocol("invalid URL", request=request)
        try:
            await asyncio.Event().wait()
        finally:
            released.append(True)
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, timeout_ms=10)
        stream = harness.models.stream_simple(harness.get_model(), {"messages": []})

        async def consume():
            return [event async for event in stream]

        events, result = await asyncio.wait_for(asyncio.gather(consume(), stream.result()), 2)
        assert not client.is_closed

    assert result["stopReason"] == "error"
    assert result["errorMessage"]
    assert events == [{"type": "error", "reason": "error", "error": result}]
    assert len(requests) == attempts
    if failure == "stall":
        assert len(released) == attempts


@pytest.mark.parametrize("failure", ["eof", "disconnect", "timeout", "bad_json", "provider_error"])
async def test_published_content_is_preserved_as_failure_without_retry(tmp_path: Path, failure: str) -> None:
    requests: list[httpx.Request] = []
    closed = asyncio.Event()

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(chunk("partial"), done=False)
            if failure == "disconnect":
                raise httpx.RemoteProtocolError("disconnected")
            if failure == "timeout":
                await asyncio.Event().wait()
            if failure == "bad_json":
                yield b"data: {broken}\n\n"
            if failure == "provider_error":
                yield sse({"error": {"message": "failed"}}, done=False)

        async def aclose(self):
            closed.set()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=BrokenBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, timeout_ms=30)
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda event: events.append(copy.deepcopy(event)))
        result = await asyncio.wait_for(harness.prompt("go"), 1)
        await asyncio.wait_for(harness.wait_for_idle(), 1)

    assert result["stopReason"] == "error"
    assert result["content"] == [{"type": "text", "text": "partial"}]
    assert len(requests) == 1
    assert closed.is_set()
    deltas = [
        event["assistantMessageEvent"]["delta"]
        for event in events
        if event.get("assistantMessageEvent", {}).get("type") == "text_delta"
    ]
    assert "".join(deltas) == "partial"
    assert len([event for event in events if event["type"] == "agent_end"]) == 1
    assert events[-1]["type"] == "settled"
