from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pi_agent_core import AgentTool

from .test_sdk import ChunkedBody, chunk, sdk, sse


def tool_chunk(*calls: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"tool_calls": list(calls)}, "finish_reason": finish}]}


def call(index: int, arguments: str, *, call_id: str | None = None, name: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"index": index, "function": {"arguments": arguments}}
    if call_id is not None:
        value["id"] = call_id
    if name is not None:
        value["function"]["name"] = name
    return value


def recorder(path: Path) -> AgentTool:
    async def execute(call_id, arguments, signal, on_update):
        with path.open("a") as output:
            output.write(f"{call_id}:{arguments['value']}\n")
        result = {"content": [{"type": "text", "text": f"recorded {arguments['value']}"}], "details": {"id": call_id}}
        if on_update:
            on_update(result)
        return result

    return AgentTool(
        name="record",
        label="Record",
        description="Record a value",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string", "minLength": 1}},
            "required": ["value"],
            "additionalProperties": False,
        },
        execute=execute,
    )


async def test_sdk_executes_custom_tool_and_returns_its_result_to_model(tmp_path: Path) -> None:
    path = tmp_path / "recorded.txt"
    tool = recorder(path)
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            body = sse(
                chunk("I'll record it."),
                tool_chunk(call(0, '{"value":', call_id="call-1", name="record")),
                tool_chunk(call(0, '"世界"}'), finish="tool_calls"),
            )
        else:
            body = sse(chunk("Recorded.", finish="stop"))
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, tools=[tool])
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda event: events.append(copy.deepcopy(event)))
        result = await asyncio.wait_for(harness.prompt("Record 世界"), 2)
        await asyncio.wait_for(harness.prompt("What was recorded?"), 2)

    assert result["stopReason"] == "stop"
    assert result["content"] == [{"type": "text", "text": "Recorded."}]
    assert path.read_text() == "call-1:世界\n"
    assert requests[0]["tools"] == [
        {
            "type": "function",
            "function": {"name": "record", "description": tool.description, "parameters": tool.parameters},
        }
    ]
    assert requests[1]["messages"][-2:] == [
        {
            "role": "assistant",
            "content": "I'll record it.",
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "record", "arguments": '{"value": "世界"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "recorded 世界"},
    ]
    generated = [e["assistantMessageEvent"] for e in events if e["type"] == "message_update"]
    expected_call = {"type": "toolCall", "id": "call-1", "name": "record", "arguments": {"value": "世界"}}
    assert next(e for e in generated if e["type"] == "toolcall_end")["toolCall"] == expected_call
    assert "".join(e["delta"] for e in generated if e["type"] == "toolcall_delta") == '{"value":"世界"}'
    generated_end = next(
        i for i, e in enumerate(events) if e.get("assistantMessageEvent", {}).get("type") == "toolcall_end"
    )
    execution_start = next(i for i, e in enumerate(events) if e["type"] == "tool_execution_start")
    assert generated_end < execution_start
    assert next(e for e in events if e["type"] == "tool_execution_update")["toolCallId"] == "call-1"
    assert next(e for e in events if e["type"] == "tool_execution_end")["isError"] is False
    assert [e["message"]["stopReason"] for e in events if e["type"] == "turn_end"] == ["toolUse", "stop", "stop"]
    assert requests[2]["messages"][:-2] == requests[1]["messages"]
    assert requests[2]["messages"][-1] == {"role": "user", "content": "What was recorded?"}
    assert (
        next(e for e in events if e["type"] == "message_end" and e["message"].get("stopReason") == "toolUse")[
            "message"
        ]["content"][-1]
        == expected_call
    )


@pytest.mark.parametrize("size", [1, 7, 10000])
async def test_interleaved_tool_calls_keep_their_names_arguments_and_results(tmp_path: Path, size: int) -> None:
    path = tmp_path / "recorded.txt"
    tool = recorder(path)
    second_tool = {
        "name": "note",
        "description": "Note a value",
        "parameters": tool.parameters,
        "execute": tool.execute,
    }
    requests: list[dict[str, Any]] = []
    body = sse(
        tool_chunk(call(0, '{"value":"世', call_id="first", name="rec")),
        tool_chunk(call(1, '{"value":', name="note")),
        tool_chunk(call(0, '界"}', name="ord")),
        tool_chunk(call(1, '"second"}', call_id="second")),
        # Some compatible services identify later deltas by ID alone.
        tool_chunk({"id": "first", "function": {"arguments": " "}}),
        tool_chunk(finish="tool_calls"),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        response = body if len(requests) == 1 else sse(chunk("Both recorded.", finish="stop"))
        return httpx.Response(200, stream=ChunkedBody(response, size))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, tools=[tool, second_tool])
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda e: events.append(copy.deepcopy(e)))
        result = await asyncio.wait_for(harness.prompt("Record both"), 2)

    assert result["stopReason"] == "stop"
    assert sorted(path.read_text().splitlines()) == ["first:世界", "second:second"]
    history = requests[1]["messages"]
    calls = history[-3]["tool_calls"]
    assert [(c["id"], c["function"]["name"], json.loads(c["function"]["arguments"])) for c in calls] == [
        ("first", "record", {"value": "世界"}),
        ("second", "note", {"value": "second"}),
    ]
    assert history[-2:] == [
        {"role": "tool", "tool_call_id": "first", "content": "recorded 世界"},
        {"role": "tool", "tool_call_id": "second", "content": "recorded second"},
    ]
    ends = [
        e["assistantMessageEvent"] for e in events if e.get("assistantMessageEvent", {}).get("type") == "toolcall_end"
    ]
    assert [e["toolCall"] for e in ends] == [
        {"type": "toolCall", "id": "first", "name": "record", "arguments": {"value": "世界"}},
        {"type": "toolCall", "id": "second", "name": "note", "arguments": {"value": "second"}},
    ]
    starts = [
        e["assistantMessageEvent"] for e in events if e.get("assistantMessageEvent", {}).get("type") == "toolcall_start"
    ]
    assert starts[0]["partial"]["content"][0]["arguments"] == {}


@pytest.mark.parametrize("arguments", ['{"value":"truncated"}', '{"value":"trunc'])
async def test_length_truncation_returns_failure_to_model_without_executing_tools(
    tmp_path: Path, arguments: str
) -> None:
    path = tmp_path / "recorded.txt"
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = (
            sse(tool_chunk(call(0, arguments, call_id="truncated", name="record"), finish="length"))
            if len(requests) == 1
            else sse(chunk("The call was truncated.", finish="stop"))
        )
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, tools=[recorder(path)])
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda e: events.append(copy.deepcopy(e)))
        result = await asyncio.wait_for(harness.prompt("Record it"), 2)

    assert not path.exists()
    assert result["stopReason"] == "stop"
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert requests[1]["messages"][-1]["tool_call_id"] == "truncated"
    assert "output token limit" in requests[1]["messages"][-1]["content"]
    assert next(e for e in events if e["type"] == "turn_end")["message"]["stopReason"] == "length"
    assert next(e for e in events if e["type"] == "tool_execution_end")["isError"] is True


@pytest.mark.parametrize(
    "deltas,finish,error_text",
    [
        ([call(0, '{"value":', call_id="bad", name="record")], "tool_calls", "Invalid JSON"),
        ([call(0, '{"value":"ok"} trailing', call_id="bad", name="record")], "tool_calls", "Invalid JSON"),
        ([call(0, '{"value":NaN}', call_id="bad", name="record")], "tool_calls", "Invalid JSON"),
        ([call(0, '{"value":Infinity}', call_id="bad", name="record")], "tool_calls", "Invalid JSON"),
        ([call(0, '{"value":-Infinity}', call_id="bad", name="record")], "tool_calls", "Invalid JSON"),
        ([call(0, '{"value":"ok"}', call_id="bad", name="record")], "stop", "finish_reason"),
        ([call(0, '{"value":"ok"}', call_id="bad", name="record")], None, "finish_reason"),
        ([call(0, '{"value":"ok"}', call_id="bad", name="record")], "content_filter", "finish_reason"),
        ([call(0, '{"value":"ok"}', name="record")], "tool_calls", "ID"),
        ([call(0, '{"value":"ok"}', call_id="bad")], "tool_calls", "name"),
        (
            [call(0, '{"value":', call_id="duplicate", name="record"), call(1, '"mixed"}', call_id="duplicate")],
            "tool_calls",
            "Conflicting",
        ),
        ([call(-1, '{"value":"ok"}', call_id="bad", name="record")], "tool_calls", "index"),
        ([{"function": {"name": "record", "arguments": '{"value":"ok"}'}}], "tool_calls", "index"),
        ([], "tool_calls", "no tool calls"),
    ],
)
async def test_invalid_tool_responses_never_execute_even_valid_sibling_calls(
    tmp_path: Path, deltas: list[dict[str, Any]], finish: str | None, error_text: str
) -> None:
    path = tmp_path / "recorded.txt"
    body = sse(
        tool_chunk(call(9, '{"value":"valid sibling"}', call_id="valid", name="record")) if deltas else chunk(),
        tool_chunk(*deltas, finish=finish),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
        harness = sdk(tmp_path, client, tools=[recorder(path)])
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda e: events.append(copy.deepcopy(e)))
        result = await asyncio.wait_for(harness.prompt("Record it"), 2)

    assert not path.exists()
    assert result["stopReason"] == "error"
    assert error_text in result["errorMessage"]
    assert not any(e["type"].startswith("tool_execution_") for e in events)
    assert (
        next(e["message"] for e in events if e["type"] == "message_end" and e["message"]["role"] == "assistant")
        == result
    )
    assert events[-1]["type"] == "settled"


@pytest.mark.parametrize(
    "name,arguments,error_text",
    [
        ("record", '{"value":""}', "must not have fewer than 1 characters"),
        ("record", "{}", "required properties value"),
        ("record", '{"value":{}}', "must be string"),
        ("record", '{"value":"ok","unexpected":1}', "must not have additional properties"),
        ("missing", '{"value":"ok"}', "Tool missing not found"),
        ("record", '{"value":"explode"}', "recording unavailable"),
    ],
)
async def test_model_can_correct_schema_unknown_tool_and_execution_errors(
    tmp_path: Path, name: str, arguments: str, error_text: str
) -> None:
    path = tmp_path / "recorded.txt"
    tool = recorder(path)
    record = tool.execute

    async def execute(call_id, args, signal, on_update):
        if args["value"] == "explode":
            raise RuntimeError("recording unavailable")
        return await record(call_id, args, signal, on_update)

    tool.execute = execute
    requests: list[dict[str, Any]] = []
    had_side_effect_before_correction: list[bool] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            body = sse(tool_chunk(call(0, arguments, call_id="failed", name=name), finish="tool_calls"))
        elif len(requests) == 2:
            had_side_effect_before_correction.append(path.exists())
            body = sse(
                tool_chunk(call(0, '{"value":"corrected"}', call_id="fixed", name="record"), finish="tool_calls")
            )
        else:
            body = sse(chunk("Corrected and recorded.", finish="stop"))
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, tools=[tool])
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda e: events.append(copy.deepcopy(e)))
        result = await asyncio.wait_for(harness.prompt("Record a value"), 2)

    assert had_side_effect_before_correction == [False]
    assert path.read_text() == "fixed:corrected\n"
    assert result["stopReason"] == "stop"
    assert requests[1]["messages"][-1]["role"] == "tool"
    assert requests[1]["messages"][-1]["tool_call_id"] == "failed"
    assert error_text in requests[1]["messages"][-1]["content"]
    assert requests[2]["messages"][-1] == {"role": "tool", "tool_call_id": "fixed", "content": "recorded corrected"}
    ends = [e for e in events if e["type"] == "tool_execution_end"]
    assert [(e["toolCallId"], e["isError"]) for e in ends] == [("failed", True), ("fixed", False)]
    failures = [e["message"] for e in events if e["type"] == "message_end" and e["message"].get("isError")]
    assert len(failures) == 1
    assert failures[0]["toolCallId"] == "failed"


async def test_follow_up_after_invalid_json_does_not_replay_unfinished_tool_calls(tmp_path: Path) -> None:
    path = tmp_path / "recorded.txt"
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = (
            sse(tool_chunk(call(0, '{"value":', call_id="unfinished", name="record"), finish="tool_calls"))
            if len(requests) == 1
            else sse(chunk("Ready to continue.", finish="stop"))
        )
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, tools=[recorder(path)])
        failed = await asyncio.wait_for(harness.prompt("Record something"), 2)
        result = await asyncio.wait_for(harness.prompt("Continue"), 2)

    assert failed["stopReason"] == "error"
    assert result["stopReason"] == "stop"
    assert not path.exists()
    assert requests[1]["messages"] == [
        {"role": "system", "content": "Be helpful."},
        {"role": "user", "content": "Record something"},
        {"role": "user", "content": "Continue"},
    ]


async def test_tool_hooks_receive_validated_arguments_and_patch_model_visible_results(tmp_path: Path) -> None:
    path = tmp_path / "recorded.txt"
    requests: list[dict[str, Any]] = []
    hook_inputs: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = (
            sse(tool_chunk(call(0, '{"value":42}', call_id="hooked", name="record"), finish="tool_calls"))
            if len(requests) == 1
            else sse(chunk("Recorded with hook.", finish="stop"))
        )
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, tools=[recorder(path)])
        harness.on("tool_call", lambda e: hook_inputs.append(copy.deepcopy(e["input"])))
        harness.on("tool_result", lambda e: {"content": [*e["content"], {"type": "text", "text": "Verified by hook."}]})
        result = await asyncio.wait_for(harness.prompt("Record 42"), 2)

    assert result["stopReason"] == "stop"
    assert hook_inputs == [{"value": "42"}]
    assert path.read_text() == "hooked:42\n"
    assert requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "hooked",
        "content": "recorded 42\nVerified by hook.",
    }


async def test_complete_tool_arguments_wait_for_model_terminal_before_execution(tmp_path: Path) -> None:
    path = tmp_path / "recorded.txt"
    generated = asyncio.Event()
    release = asyncio.Event()
    requests: list[httpx.Request] = []

    class WaitingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(tool_chunk(call(0, '{"value":"ready"}', call_id="ready", name="record")), done=False)
            await release.wait()
            yield sse(tool_chunk(finish="tool_calls"))

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, stream=WaitingBody())
        return httpx.Response(200, content=sse(chunk("Done.", finish="stop")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, tools=[recorder(path)])
        events: list[dict[str, Any]] = []

        def observe(event):
            events.append(copy.deepcopy(event))
            if event.get("assistantMessageEvent", {}).get("type") == "toolcall_delta":
                generated.set()

        harness.subscribe(observe)
        task = asyncio.create_task(harness.prompt("Record when ready"))
        try:
            await asyncio.wait_for(generated.wait(), 1)
            assert not path.exists()
            assert not any(e["type"].startswith("tool_execution_") for e in events)
        finally:
            release.set()
            result = await asyncio.wait_for(task, 2)

    assert result["stopReason"] == "stop"
    assert path.read_text() == "ready:ready\n"


async def test_model_tool_generation_events_match_result_without_executing_tool(tmp_path: Path) -> None:
    path = tmp_path / "recorded.txt"
    body = sse(
        tool_chunk(call(0, '{"value":', call_id="generated", name="record")),
        tool_chunk(call(0, '"preview"}'), finish="tool_calls"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
        harness = sdk(tmp_path, client)
        stream = harness.models.stream_simple(harness.get_model(), {"messages": [], "tools": [recorder(path)]})

        async def consume():
            return [event async for event in stream]

        events, result = await asyncio.wait_for(asyncio.gather(consume(), stream.result()), 1)

    expected = {"type": "toolCall", "id": "generated", "name": "record", "arguments": {"value": "preview"}}
    assert not path.exists()
    assert result["content"] == [expected]
    assert result["stopReason"] == "toolUse"
    assert events[-1] == {"type": "done", "reason": "toolUse", "message": result}
    end = next(event for event in events if event["type"] == "toolcall_end")
    assert end["toolCall"] == expected
    assert end["partial"] == result
    deltas = [event for event in events if event["type"] == "toolcall_delta"]
    assert "".join(event["delta"] for event in deltas) == '{"value":"preview"}'
    assert deltas[0]["partial"]["content"][0]["arguments"] == {}
