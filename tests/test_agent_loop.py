from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pi_agent_core import (
    AgentContext,
    AgentLoopConfig,
    AgentTool,
    AssistantMessageEventStream,
    agent_loop,
    agent_loop_continue,
)


def usage() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def model() -> dict[str, Any]:
    return {
        "id": "mock",
        "name": "mock",
        "api": "mock-api",
        "provider": "mock",
        "baseUrl": "",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 8192,
        "maxTokens": 2048,
    }


def assistant(content: list[dict[str, Any]], stop_reason: str = "stop") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "api": "mock-api",
        "provider": "mock",
        "model": "mock",
        "usage": usage(),
        "stopReason": stop_reason,
        "timestamp": 1,
    }


def completed_stream(message: dict[str, Any]) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    asyncio.get_running_loop().call_soon(
        stream.push,
        {
            "type": "done" if message["stopReason"] not in {"error", "aborted"} else "error",
            "reason": message["stopReason"],
            "message" if message["stopReason"] not in {"error", "aborted"} else "error": message,
        },
    )
    return stream


@pytest.mark.asyncio
async def test_basic_event_sequence_matches_upstream() -> None:
    prompt = {"role": "user", "content": "Hello", "timestamp": 1}

    def stream_fn(*_: Any) -> AssistantMessageEventStream:
        return completed_stream(assistant([{"type": "text", "text": "Hi"}]))

    stream = agent_loop(
        [prompt],
        AgentContext("You are helpful", [], []),
        AgentLoopConfig(model(), lambda messages: messages),
        stream_fn=stream_fn,
    )
    events = [event async for event in stream]
    messages = await stream.result()

    assert [event["type"] for event in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert [message["role"] for message in messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_parallel_completion_events_and_source_order_results() -> None:
    completions: list[str] = []

    async def execute(tool_call_id: str, args: dict[str, Any], *_: Any) -> dict[str, Any]:
        await asyncio.sleep(0.03 if args["value"] == "first" else 0.001)
        completions.append(args["value"])
        return {"content": [{"type": "text", "text": args["value"]}], "details": {}}

    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    tools = [AgentTool("work", "Work", "Work", schema, execute)]
    responses = iter(
        [
            assistant(
                [
                    {"type": "toolCall", "id": "a", "name": "work", "arguments": {"value": "first"}},
                    {"type": "toolCall", "id": "b", "name": "work", "arguments": {"value": "second"}},
                ],
                "toolUse",
            ),
            assistant([{"type": "text", "text": "done"}]),
        ]
    )

    def stream_fn(*_: Any) -> AssistantMessageEventStream:
        return completed_stream(next(responses))

    stream = agent_loop(
        [{"role": "user", "content": "go", "timestamp": 1}],
        AgentContext("", [], tools),
        AgentLoopConfig(model(), lambda messages: messages),
        stream_fn=stream_fn,
    )
    events = [event async for event in stream]
    messages = await stream.result()
    end_order = [event["toolCallId"] for event in events if event["type"] == "tool_execution_end"]
    result_order = [message["toolCallId"] for message in messages if message["role"] == "toolResult"]

    assert completions == ["second", "first"]
    assert end_order == ["b", "a"]
    assert result_order == ["a", "b"]


@pytest.mark.asyncio
async def test_length_truncated_tool_call_is_not_executed() -> None:
    called = False

    async def execute(*_: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"content": [], "details": {}}

    tool = AgentTool("danger", "Danger", "Danger", {"type": "object"}, execute)
    responses = iter(
        [
            assistant(
                [{"type": "toolCall", "id": "a", "name": "danger", "arguments": {}}],
                "length",
            ),
            assistant([{"type": "text", "text": "retry"}]),
        ]
    )

    def stream_fn(*_: Any) -> AssistantMessageEventStream:
        return completed_stream(next(responses))

    stream = agent_loop(
        [{"role": "user", "content": "go", "timestamp": 1}],
        AgentContext("", [], [tool]),
        AgentLoopConfig(model(), lambda messages: messages),
        stream_fn=stream_fn,
    )
    await stream.result()
    assert not called


@pytest.mark.asyncio
async def test_transform_runs_before_conversion_and_custom_messages_are_supported() -> None:
    order: list[str] = []
    seen: list[dict[str, Any]] = []

    async def transform(messages: list[dict[str, Any]], _signal: Any) -> list[dict[str, Any]]:
        order.append("transform")
        return [*messages, {"role": "custom", "content": "injected", "timestamp": 2}]

    def convert(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        order.append("convert")
        return [
            {"role": "user", "content": message["content"], "timestamp": message["timestamp"]}
            for message in messages
            if message["role"] in {"user", "custom"}
        ]

    def stream_fn(_model: Any, context: dict[str, Any], _options: Any) -> AssistantMessageEventStream:
        seen.extend(context["messages"])
        return completed_stream(assistant([{"type": "text", "text": "ok"}]))

    stream = agent_loop(
        [{"role": "user", "content": "hello", "timestamp": 1}],
        AgentContext("", [], []),
        AgentLoopConfig(model(), convert, transform_context=transform),
        stream_fn=stream_fn,
    )
    await stream.result()
    assert order == ["transform", "convert"]
    assert [message["content"] for message in seen] == ["hello", "injected"]


@pytest.mark.asyncio
async def test_prepare_arguments_then_validate_and_before_hook_can_mutate_without_revalidation() -> None:
    executed: list[dict[str, Any]] = []

    async def execute(_id: str, args: dict[str, Any], *_: Any) -> dict[str, Any]:
        executed.append(dict(args))
        return {"content": [], "details": {}}

    tool = AgentTool(
        "work",
        "Work",
        "",
        {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
        execute,
        prepare_arguments=lambda raw: {"count": int(raw["count"])},
    )
    responses = iter(
        [
            assistant([{"type": "toolCall", "id": "t", "name": "work", "arguments": {"count": "2"}}], "toolUse"),
            assistant([{"type": "text", "text": "done"}]),
        ]
    )

    async def before(payload: dict[str, Any]) -> None:
        payload["args"]["count"] = "mutated after validation"

    stream = agent_loop(
        [{"role": "user", "content": "go", "timestamp": 1}],
        AgentContext("", [], [tool]),
        AgentLoopConfig(model(), lambda messages: messages, before_tool_call=before),
        stream_fn=lambda *_: completed_stream(next(responses)),
    )
    await stream.result()
    assert executed == [{"count": "mutated after validation"}]


@pytest.mark.asyncio
async def test_sequential_override_and_mixed_termination_continue() -> None:
    active = 0
    max_active = 0

    async def execute(_id: str, args: dict[str, Any], *_: Any) -> dict[str, Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.005)
        active -= 1
        return {"content": [], "details": {}, "terminate": args["terminate"]}

    schema = {
        "type": "object",
        "properties": {"terminate": {"type": "boolean"}},
        "required": ["terminate"],
    }
    sequential = AgentTool("work", "Work", "", schema, execute, execution_mode="sequential")
    responses = iter(
        [
            assistant(
                [
                    {"type": "toolCall", "id": "a", "name": "work", "arguments": {"terminate": True}},
                    {"type": "toolCall", "id": "b", "name": "work", "arguments": {"terminate": False}},
                ],
                "toolUse",
            ),
            assistant([{"type": "text", "text": "continued"}]),
        ]
    )
    stream = agent_loop(
        [{"role": "user", "content": "go", "timestamp": 1}],
        AgentContext("", [], [sequential]),
        AgentLoopConfig(model(), lambda messages: messages),
        stream_fn=lambda *_: completed_stream(next(responses)),
    )
    messages = await stream.result()
    assert max_active == 1
    assert messages[-1]["content"][0]["text"] == "continued"


@pytest.mark.asyncio
async def test_steering_is_injected_only_after_entire_tool_batch_finishes() -> None:
    release_first = asyncio.Event()
    second_finished = asyncio.Event()
    provider_contexts: list[list[str]] = []
    polls = 0

    async def execute(_id: str, args: dict[str, Any], *_: Any) -> dict[str, Any]:
        if args["name"] == "first":
            await release_first.wait()
        else:
            second_finished.set()
        return {"content": [{"type": "text", "text": args["name"]}], "details": {}}

    async def steering() -> list[dict[str, Any]]:
        nonlocal polls
        polls += 1
        if polls == 2:
            assert second_finished.is_set()
            release_first.set()
            return [{"role": "user", "content": "steer", "timestamp": 3}]
        return []

    tool = AgentTool(
        "work",
        "Work",
        "",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        execute,
    )
    responses = iter(
        [
            assistant(
                [
                    {"type": "toolCall", "id": "a", "name": "work", "arguments": {"name": "first"}},
                    {"type": "toolCall", "id": "b", "name": "work", "arguments": {"name": "second"}},
                ],
                "toolUse",
            ),
            assistant([{"type": "text", "text": "after steering"}]),
        ]
    )

    def stream_fn(_model: Any, context: dict[str, Any], _options: Any) -> AssistantMessageEventStream:
        provider_contexts.append([message["role"] for message in context["messages"]])
        return completed_stream(next(responses))

    task = asyncio.create_task(
        agent_loop(
            [{"role": "user", "content": "go", "timestamp": 1}],
            AgentContext("", [], [tool]),
            AgentLoopConfig(model(), lambda messages: messages, get_steering_messages=steering),
            stream_fn=stream_fn,
        ).result()
    )
    await second_finished.wait()
    release_first.set()
    messages = await task

    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "toolResult",
        "toolResult",
        "user",
        "assistant",
    ]
    assert provider_contexts[1][-3:] == ["toolResult", "toolResult", "user"]


@pytest.mark.asyncio
async def test_all_terminate_and_after_hook_termination_stop_without_extra_provider_turn() -> None:
    provider_calls = 0

    async def execute(*_: Any) -> dict[str, Any]:
        return {"content": [], "details": {}}

    async def after(payload: dict[str, Any]) -> dict[str, Any]:
        return {"terminate": True, "details": {"patched": payload["toolCall"]["id"]}}

    tools = [AgentTool("stop", "Stop", "", {"type": "object"}, execute)]
    response = assistant(
        [
            {"type": "toolCall", "id": "a", "name": "stop", "arguments": {}},
            {"type": "toolCall", "id": "b", "name": "stop", "arguments": {}},
        ],
        "toolUse",
    )

    def stream_fn(*_: Any) -> AssistantMessageEventStream:
        nonlocal provider_calls
        provider_calls += 1
        return completed_stream(response)

    stream = agent_loop(
        [{"role": "user", "content": "go", "timestamp": 1}],
        AgentContext("", [], tools),
        AgentLoopConfig(model(), lambda messages: messages, after_tool_call=after),
        stream_fn=stream_fn,
    )
    messages = await stream.result()

    assert provider_calls == 1
    assert [message["role"] for message in messages] == ["user", "assistant", "toolResult", "toolResult"]
    assert [message["details"]["patched"] for message in messages[-2:]] == ["a", "b"]


@pytest.mark.asyncio
async def test_prepare_next_turn_snapshot_and_should_stop() -> None:
    calls: list[str] = []
    first_model = model()
    second_model = {**model(), "id": "second"}
    tool = AgentTool("noop", "Noop", "", {"type": "object"}, lambda *_: {"content": [], "details": {}})
    responses = iter(
        [
            assistant([{"type": "toolCall", "id": "t", "name": "noop", "arguments": {}}], "toolUse"),
            assistant([{"type": "text", "text": "second"}]),
        ]
    )

    def stream_fn(selected: dict[str, Any], *_: Any) -> AssistantMessageEventStream:
        calls.append(selected["id"])
        return completed_stream(next(responses))

    async def prepare(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "context": AgentContext("changed", [{"role": "user", "content": "replacement", "timestamp": 3}], []),
            "model": second_model,
            "thinkingLevel": "high",
        }

    turns = 0

    async def should_stop(_payload: dict[str, Any]) -> bool:
        nonlocal turns
        turns += 1
        return turns == 2

    config = AgentLoopConfig(
        first_model,
        lambda messages: messages,
        prepare_next_turn=prepare,
        should_stop_after_turn=should_stop,
        reasoning="low",
    )
    stream = agent_loop(
        [{"role": "user", "content": "go", "timestamp": 1}],
        AgentContext("", [], [tool]),
        config,
        stream_fn=stream_fn,
    )
    await stream.result()
    assert calls == ["mock", "second"]
    assert turns == 2
    assert config.model is first_model and config.reasoning == "low"


@pytest.mark.asyncio
async def test_prepare_next_turn_null_model_uses_current_model() -> None:
    calls: list[str] = []
    tool = AgentTool(
        "noop",
        "Noop",
        "",
        {"type": "object"},
        lambda *_: {"content": [], "details": {}},
    )
    responses = iter(
        [
            assistant([{"type": "toolCall", "id": "t", "name": "noop", "arguments": {}}], "toolUse"),
            assistant([{"type": "text", "text": "done"}]),
        ]
    )

    def stream_fn(selected: dict[str, Any], *_: Any) -> AssistantMessageEventStream:
        calls.append(selected["id"])
        return completed_stream(next(responses))

    stream = agent_loop(
        [{"role": "user", "content": "go", "timestamp": 1}],
        AgentContext("", [], [tool]),
        AgentLoopConfig(
            model(),
            lambda messages: messages,
            prepare_next_turn=lambda _context: {"model": None},
        ),
        stream_fn=stream_fn,
    )
    await stream.result()
    assert calls == ["mock", "mock"]


@pytest.mark.asyncio
async def test_tool_result_normalization_and_added_tool_names_presence() -> None:
    async def execute(_id: str, args: dict[str, Any], *_: Any) -> dict[str, Any]:
        return {
            "content": None,
            "details": {"kind": args["kind"]},
            "addedToolNames": [] if args["kind"] == "empty" else ["new-tool"],
            "terminate": True,
        }

    tool = AgentTool(
        "install",
        "Install",
        "",
        {
            "type": "object",
            "properties": {"kind": {"type": "string"}},
            "required": ["kind"],
        },
        execute,
    )
    response = assistant(
        [
            {"type": "toolCall", "id": "a", "name": "install", "arguments": {"kind": "empty"}},
            {"type": "toolCall", "id": "b", "name": "install", "arguments": {"kind": "present"}},
        ],
        "toolUse",
    )
    stream = agent_loop(
        [{"role": "user", "content": "go", "timestamp": 1}],
        AgentContext("", [], [tool]),
        AgentLoopConfig(model(), lambda messages: messages),
        stream_fn=lambda *_: completed_stream(response),
    )
    messages = await stream.result()
    results = [message for message in messages if message["role"] == "toolResult"]
    assert results[0]["content"] == []
    assert "addedToolNames" not in results[0]
    assert results[1]["addedToolNames"] == ["new-tool"]


@pytest.mark.asyncio
async def test_stream_options_include_full_config_spread_and_resolved_api_key() -> None:
    captured: dict[str, Any] = {}

    def original_key_callback(_provider: str) -> str:
        return "fresh-key"

    def stream_fn(_model: Any, _context: Any, options: dict[str, Any]) -> AssistantMessageEventStream:
        captured.update(options)
        return completed_stream(assistant([]))

    config = AgentLoopConfig.from_value(
        {
            "model": model(),
            "convertToLlm": lambda messages: messages,
            "getApiKey": original_key_callback,
            "apiKey": "stale-key",
            "temperature": 0.25,
            "customProviderOption": "kept",
        }
    )
    stream = agent_loop(
        [{"role": "user", "content": "go", "timestamp": 1}],
        AgentContext("", [], []),
        config,
        stream_fn=stream_fn,
    )
    await stream.result()
    assert captured["apiKey"] == "fresh-key"
    assert captured["getApiKey"] is original_key_callback
    assert captured["convertToLlm"] is config.convert_to_llm
    assert captured["temperature"] == 0.25
    assert captured["customProviderOption"] == "kept"


@pytest.mark.asyncio
async def test_dataclass_config_exposes_all_standard_simple_stream_options() -> None:
    captured: dict[str, Any] = {}

    def stream_fn(_model: Any, _context: Any, options: dict[str, Any]) -> AssistantMessageEventStream:
        captured.update(options)
        return completed_stream(assistant([]))

    config = AgentLoopConfig(
        model(),
        lambda messages: messages,
        temperature=0.4,
        max_tokens=333,
        cache_retention="long",
        headers={"x-test": "yes", "x-remove": None},
        timeout_ms=10_000,
        websocket_connect_timeout_ms=2_000,
        max_retries=4,
        metadata={"user_id": "u"},
        env={"REGION": "test"},
    )
    stream = agent_loop(
        [{"role": "user", "content": "go", "timestamp": 1}],
        AgentContext("", [], []),
        config,
        stream_fn=stream_fn,
    )
    await stream.result()

    assert captured["temperature"] == 0.4
    assert captured["maxTokens"] == 333
    assert captured["cacheRetention"] == "long"
    assert captured["headers"] == {"x-test": "yes", "x-remove": None}
    assert captured["timeoutMs"] == 10_000
    assert captured["websocketConnectTimeoutMs"] == 2_000
    assert captured["maxRetries"] == 4
    assert captured["metadata"] == {"user_id": "u"}
    assert captured["env"] == {"REGION": "test"}


def test_continue_validation_is_synchronous() -> None:
    config = AgentLoopConfig(model(), lambda messages: messages)
    with pytest.raises(ValueError, match="no messages"):
        agent_loop_continue(AgentContext("", [], []), config)
    with pytest.raises(ValueError, match="assistant"):
        agent_loop_continue(AgentContext("", [assistant([])], []), config)


@pytest.mark.asyncio
async def test_mapping_context_preserves_continue_mutation_and_optional_tools() -> None:
    context: dict[str, Any] = {
        "systemPrompt": "system",
        "messages": [{"role": "user", "content": "continue", "timestamp": 1}],
    }
    captured: dict[str, Any] = {}

    def stream_fn(_model: Any, llm_context: dict[str, Any], _options: Any) -> AssistantMessageEventStream:
        captured.update(llm_context)
        return completed_stream(assistant([{"type": "text", "text": "done"}]))

    stream = agent_loop_continue(context, AgentLoopConfig(model(), lambda messages: messages), stream_fn=stream_fn)
    await stream.result()

    assert captured["tools"] is None
    assert [message["role"] for message in context["messages"]] == ["user", "assistant"]
