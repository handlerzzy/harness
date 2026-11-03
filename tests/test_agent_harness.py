from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aster_harness import JS_UNDEFINED, AgentTool, AssistantMessageEventStream
from aster_harness.harness import AgentHarness, AgentHarnessError
from aster_harness.harness.env import NodeExecutionEnv
from aster_harness.harness.session import InMemorySessionStorage, Session

from .test_agent_loop import assistant, completed_stream, model


class FauxModels:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.contexts: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []

    async def streamSimple(
        self, selected_model: Any, context: dict[str, Any], options: dict[str, Any]
    ) -> AssistantMessageEventStream:
        self.contexts.append(context)
        self.options.append(options)
        response = self.responses.pop(0)
        if callable(response):
            response = response(context, options)
        if asyncio.iscoroutine(response):
            response = await response
        return completed_stream(response)

    async def completeSimple(self, *_: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": "summary"}], "stopReason": "stop"}


def harness(
    tmp_path: Any,
    responses: list[Any] | None = None,
    *,
    session: Session | None = None,
    **options: Any,
) -> tuple[AgentHarness, FauxModels, Session]:
    selected_session = session or Session(InMemorySessionStorage())
    models = FauxModels(responses)
    instance = AgentHarness(
        {
            "models": models,
            "env": NodeExecutionEnv({"cwd": str(tmp_path)}),
            "session": selected_session,
            "model": model(),
            **options,
        }
    )
    return instance, models, selected_session


def user_texts(context: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for message in context["messages"]:
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            result.append(content)
        else:
            result.extend(str(block.get("text")) for block in content if block.get("type") == "text")
    return result


@pytest.mark.asyncio
async def test_construct_validation_and_state_accessors(tmp_path: Any) -> None:
    instance, _, _ = harness(tmp_path, [])
    assert instance.get_model()["id"] == "mock"
    assert instance.get_thinking_level() == "off"
    assert instance.get_steering_mode() == "one-at-a-time"
    await instance.set_steering_mode("all")
    await instance.set_follow_up_mode("all")
    assert instance.get_steering_mode() == "all"
    assert instance.get_follow_up_mode() == "all"

    tool = AgentTool("same", "Same", "", {"type": "object"}, lambda *_: {"content": []})
    with pytest.raises(AgentHarnessError, match=r"Duplicate tool name\(s\): same"):
        harness(tmp_path, [], tools=[tool, tool])

    nullish, _, _ = harness(
        tmp_path,
        [],
        thinkingLevel=None,
        steeringMode=None,
        followUpMode=None,
    )
    assert nullish.get_thinking_level() == "off"
    assert nullish.get_steering_mode() == "one-at-a-time"
    assert nullish.get_follow_up_mode() == "one-at-a-time"
    assert nullish.get_resources() == {"skills": None, "promptTemplates": None}


@pytest.mark.asyncio
async def test_pythonic_nested_aliases_and_clone_boundaries(tmp_path: Any) -> None:
    resources = {"prompt_templates": [{"name": "hello", "content": "Hello"}]}
    instance, models, _ = harness(
        tmp_path,
        [assistant([{"type": "text", "text": "ok"}])],
        resources=resources,
        stream_options={
            "timeout_ms": 10,
            "max_retries": 2,
            "max_retry_delay_ms": 30,
            "cache_retention": "none",
        },
    )
    returned = instance.get_resources()
    assert returned["promptTemplates"] == [{"name": "hello", "content": "Hello"}]
    returned["promptTemplates"].clear()
    assert len(instance.get_resources()["promptTemplates"]) == 1
    await instance.prompt("go")
    assert models.options[0]["timeoutMs"] == 10
    assert models.options[0]["maxRetries"] == 2
    assert models.options[0]["maxRetryDelayMs"] == 30
    assert models.options[0]["cacheRetention"] == "none"


@pytest.mark.asyncio
async def test_prompt_events_hooks_and_persistence(tmp_path: Any) -> None:
    instance, models, session = harness(tmp_path, [assistant([{"type": "text", "text": "ok"}])])
    events: list[str] = []

    async def subscriber(event: dict[str, Any], _signal: Any = None) -> None:
        events.append(event["type"])

    instance.subscribe(subscriber)
    instance.on(
        "before_agent_start",
        lambda _event: {
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hook"}], "timestamp": 2}],
            "systemPrompt": "hook system",
        },
    )
    result = await instance.prompt("hello")
    assert result["content"][0]["text"] == "ok"
    assert models.contexts[0]["systemPrompt"] == "hook system"
    assert user_texts(models.contexts[0]) == ["hello", "hook"]
    persisted = [item["message"]["role"] for item in await session.get_entries() if item["type"] == "message"]
    assert persisted == ["user", "user", "assistant"]
    assert events[-2:] == ["agent_end", "settled"]
    assert events.count("save_point") == 1


@pytest.mark.asyncio
async def test_steering_and_followup_queues_drain_one_at_a_time(tmp_path: Any) -> None:
    instance, models, _ = harness(
        tmp_path,
        [
            assistant([{"type": "text", "text": "one"}]),
            assistant([{"type": "text", "text": "two"}]),
            assistant([{"type": "text", "text": "three"}]),
            assistant([{"type": "text", "text": "four"}]),
        ],
    )
    queued = False

    async def listener(event: dict[str, Any]) -> None:
        nonlocal queued
        if event["type"] == "message_start" and event.get("message", {}).get("role") == "assistant" and not queued:
            queued = True
            await instance.steer("steer one")
            await instance.steer("steer two")
            await instance.follow_up("follow")

    instance.subscribe(listener)
    await instance.prompt("start")
    assert [len(user_texts(context)) for context in models.contexts] == [1, 2, 3, 4]
    assert user_texts(models.contexts[-1])[-1] == "follow"


@pytest.mark.asyncio
async def test_next_turn_and_pending_mutations_refresh_turn_state(tmp_path: Any) -> None:
    first_tool = AgentTool("first", "First", "", {"type": "object"}, lambda *_: {"content": []})
    second_tool = AgentTool("second", "Second", "", {"type": "object"}, lambda *_: {"content": []})
    instance, models, session = harness(
        tmp_path,
        [assistant([{"type": "text", "text": "one"}]), assistant([{"type": "text", "text": "two"}])],
        tools=[first_tool, second_tool],
        activeToolNames=["first"],
    )
    await instance.next_turn("queued")
    changed = False

    async def listener(event: dict[str, Any]) -> None:
        nonlocal changed
        if event["type"] == "message_start" and event.get("message", {}).get("role") == "assistant" and not changed:
            changed = True
            await instance.set_active_tools(["second"])
            await instance.set_thinking_level("high")
            await instance.follow_up("again")

    instance.subscribe(listener)
    await instance.prompt("hello")
    assert user_texts(models.contexts[0]) == ["queued", "hello"]
    assert [tool.name for tool in models.contexts[0]["tools"]] == ["first"]
    assert [tool.name for tool in models.contexts[1]["tools"]] == ["second"]
    assert models.options[1]["reasoning"] == "high"
    types = [entry["type"] for entry in await session.get_entries()]
    assert "active_tools_change" in types and "thinking_level_change" in types


@pytest.mark.asyncio
async def test_provider_hooks_patch_options_and_payload(tmp_path: Any) -> None:
    instance, models, _ = harness(
        tmp_path,
        [assistant([{"type": "text", "text": "ok"}])],
        streamOptions={"headers": {"a": "1", "remove": "x"}, "metadata": {"old": True}, "timeoutMs": 10},
    )
    seen_payloads: list[Any] = []
    instance.on(
        "before_provider_request",
        lambda _event: {
            "streamOptions": {
                "headers": {"remove": JS_UNDEFINED, "b": "2", "suppress": None},
                "metadata": None,
                "timeoutMs": 20,
            }
        },
    )
    instance.on("before_provider_payload", lambda event: {"payload": {**event["payload"], "hook": True}})
    await instance.prompt("hello")
    assert models.options[0]["headers"] == {"a": "1", "b": "2", "suppress": None}
    assert models.options[0]["metadata"] is None
    assert models.options[0]["timeoutMs"] == 20
    seen_payloads.append(await models.options[0]["onPayload"]({"value": 1}))
    assert seen_payloads == [{"value": 1, "hook": True}]


@pytest.mark.asyncio
async def test_tool_hooks_block_and_patch_results(tmp_path: Any) -> None:
    executed = False

    async def execute(*_: Any) -> dict[str, Any]:
        nonlocal executed
        executed = True
        return {"content": [{"type": "text", "text": "raw"}], "details": {}}

    tool = AgentTool("work", "Work", "", {"type": "object"}, execute)
    first = assistant([{"type": "toolCall", "id": "t", "name": "work", "arguments": {}}], "toolUse")
    instance, _, session = harness(tmp_path, [first, assistant([{"type": "text", "text": "done"}])], tools=[tool])
    instance.on("tool_result", lambda _event: {"content": [{"type": "text", "text": "patched"}]})
    await instance.prompt("go")
    assert executed
    results = [
        entry["message"]
        for entry in await session.get_entries()
        if entry["type"] == "message" and entry["message"]["role"] == "toolResult"
    ]
    assert results[0]["content"][0]["text"] == "patched"


@pytest.mark.asyncio
async def test_failure_lifecycle_busy_and_wait_for_idle(tmp_path: Any) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed(_context: Any, _options: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        raise RuntimeError("provider failed")

    instance, _, session = harness(tmp_path, [delayed])
    task = asyncio.create_task(instance.prompt("hello"))
    await entered.wait()
    with pytest.raises(AgentHarnessError, match="busy"):
        await instance.prompt("other")
    idle = asyncio.create_task(instance.wait_for_idle())
    assert not idle.done()
    release.set()
    result = await task
    await idle
    assert result["stopReason"] == "error"
    assert result["errorMessage"] == "provider failed"
    assert [entry["message"]["role"] for entry in await session.get_entries() if entry.get("type") == "message"] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_resources_manual_compaction_and_navigation_hooks(tmp_path: Any) -> None:
    session = Session(InMemorySessionStorage())
    root = await session.append_message({"role": "user", "content": "root", "timestamp": 1})
    abandoned = await session.append_message({"role": "user", "content": "abandoned", "timestamp": 2})
    await session.append_message(assistant([{"type": "text", "text": "answer"}]))
    instance, _, _ = harness(
        tmp_path,
        [],
        session=session,
        resources={
            "skills": [{"name": "demo", "filePath": "/demo/SKILL.md", "content": "do it"}],
            "promptTemplates": [{"name": "fix", "content": "Fix $1"}],
        },
    )
    resources = instance.get_resources()
    resources["skills"].clear()
    assert len(instance.get_resources()["skills"]) == 1

    instance.on(
        "session_before_compact",
        lambda event: {
            "compaction": {
                "summary": "hook summary",
                "firstKeptEntryId": event["preparation"]["firstKeptEntryId"],
                "tokensBefore": event["preparation"]["tokensBefore"],
                "details": {"source": "hook"},
            }
        },
    )
    compacted = await instance.compact()
    assert compacted["summary"] == "hook summary"

    instance.on("session_before_tree", lambda _event: {"summary": {"summary": "branch hook"}})
    result = await instance.navigate_tree(abandoned)
    assert not result["cancelled"]
    assert result["editorText"] == "abandoned"
    assert result["summaryEntry"]["parentId"] == root
    assert await session.get_leaf_id() == result["summaryEntry"]["id"]


@pytest.mark.asyncio
async def test_hook_failure_settles_as_persisted_error_and_harness_is_reusable(tmp_path: Any) -> None:
    instance, _, session = harness(
        tmp_path,
        [assistant([{"type": "text", "text": "unused"}]), assistant([{"type": "text", "text": "unused"}])],
    )
    events: list[str] = []
    instance.subscribe(lambda event: events.append(event["type"]))
    instance.on("context", lambda _event: (_ for _ in ()).throw(RuntimeError("context exploded")))
    first = await instance.prompt("hello")
    second = await instance.prompt("again")
    assert first["stopReason"] == second["stopReason"] == "error"
    assert first["errorMessage"] == "context exploded"
    messages = [entry["message"] for entry in await session.get_entries() if entry["type"] == "message"]
    assert [message["role"] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert "agent_end" in events and "settled" in events


@pytest.mark.asyncio
async def test_type_error_hook_runs_once_and_subscribers_use_live_set_iteration(tmp_path: Any) -> None:
    instance, _, _ = harness(tmp_path, [assistant([{"type": "text", "text": "unused"}])])
    hook_calls = 0

    def broken_context(_event: dict[str, Any]) -> None:
        nonlocal hook_calls
        hook_calls += 1
        raise TypeError("hook failed")

    calls: list[tuple[str, str]] = []
    remove_second: Any = None

    def third(event: dict[str, Any]) -> None:
        calls.append(("third", event["type"]))

    def first(event: dict[str, Any]) -> None:
        calls.append(("first", event["type"]))
        if event["type"] == "agent_start":
            remove_second()
            instance.subscribe(third)

    def second(event: dict[str, Any]) -> None:
        calls.append(("second", event["type"]))

    instance.on("context", broken_context)
    instance.subscribe(first)
    remove_second = instance.subscribe(second)
    instance.subscribe(first)
    result = await instance.prompt("hello")

    assert hook_calls == 1
    assert result["stopReason"] == "error" and result["errorMessage"] == "hook failed"
    assert calls[:2] == [("first", "agent_start"), ("third", "agent_start")]
    assert all(name != "second" for name, _event in calls)


@pytest.mark.asyncio
async def test_pending_listener_write_follows_assistant_and_stream_options_refresh_at_savepoint(tmp_path: Any) -> None:
    tool = AgentTool("noop", "Noop", "", {"type": "object"}, lambda *_: {"content": [], "details": {}})
    first = assistant([{"type": "toolCall", "id": "t", "name": "noop", "arguments": {}}], "toolUse")
    instance, models, session = harness(
        tmp_path,
        [first, assistant([{"type": "text", "text": "done"}])],
        tools=[tool],
        streamOptions={"timeoutMs": 1000, "headers": {"turn": "first"}},
    )
    wrote = False

    async def listener(event: dict[str, Any]) -> None:
        nonlocal wrote
        if event["type"] == "tool_execution_start":
            await instance.set_stream_options({"timeoutMs": 2000, "headers": {"turn": "second"}})
        if event["type"] == "message_end" and event.get("message", {}).get("role") == "assistant" and not wrote:
            wrote = True
            await instance.append_message(
                {
                    "role": "custom",
                    "customType": "listener",
                    "content": "listener write",
                    "display": True,
                    "timestamp": 4,
                }
            )

    instance.subscribe(listener)
    await instance.prompt("hello")
    assert models.options[0]["timeoutMs"] == 1000
    assert models.options[0]["headers"] == {"turn": "first"}
    assert models.options[1]["timeoutMs"] == 2000
    assert models.options[1]["headers"] == {"turn": "second"}
    roles = [entry["message"]["role"] for entry in await session.get_entries() if entry["type"] == "message"]
    assert roles[:4] == ["user", "assistant", "toolResult", "custom"]


@pytest.mark.asyncio
async def test_abort_clears_active_queues_preserves_next_turn_and_emits_event(tmp_path: Any) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    captured_signal: Any = None

    async def delayed(_context: Any, options: dict[str, Any]) -> dict[str, Any]:
        nonlocal captured_signal
        captured_signal = options["signal"]
        entered.set()
        await release.wait()
        return assistant([], "aborted" if captured_signal.aborted else "stop")

    instance, _, _ = harness(tmp_path, [delayed, assistant([{"type": "text", "text": "second"}])])
    abort_events: list[dict[str, Any]] = []
    instance.subscribe(lambda event: abort_events.append(event) if event["type"] == "abort" else None)
    prompt_task = asyncio.create_task(instance.prompt("first"))
    await entered.wait()
    await instance.steer("steer")
    await instance.follow_up("follow")
    await instance.next_turn("next")
    abort_task = asyncio.create_task(instance.abort())
    await asyncio.sleep(0)
    assert captured_signal.aborted
    release.set()
    cleared = await abort_task
    await prompt_task
    assert len(cleared["clearedSteer"]) == 1
    assert len(cleared["clearedFollowUp"]) == 1
    assert len(abort_events) == 1
    await instance.prompt("second")
