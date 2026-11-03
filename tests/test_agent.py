from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aster_harness import Agent, AgentTool, AssistantMessageEventStream

from .test_agent_loop import assistant, completed_stream, model


@pytest.mark.asyncio
async def test_agent_state_and_awaited_subscriber_barrier() -> None:
    def stream_fn(*_: Any) -> AssistantMessageEventStream:
        return completed_stream(assistant([{"type": "text", "text": "Hi"}]))

    agent = Agent(initial_state={"model": model()}, stream_fn=stream_fn)
    settled = False

    async def listener(event: dict[str, Any], _signal: object) -> None:
        nonlocal settled
        if event["type"] == "agent_end":
            await asyncio.sleep(0.01)
            settled = True
            assert agent.state.is_streaming

    agent.subscribe(listener)
    await agent.prompt("Hello")

    assert settled
    assert not agent.state.is_streaming
    assert [message["role"] for message in agent.state.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_continue_processes_queued_follow_up_from_assistant_tail() -> None:
    responses = iter(
        [
            assistant([{"type": "text", "text": "one"}]),
            assistant([{"type": "text", "text": "two"}]),
        ]
    )

    def stream_fn(*_: Any) -> AssistantMessageEventStream:
        return completed_stream(next(responses))

    agent = Agent(initial_state={"model": model()}, stream_fn=stream_fn)
    await agent.prompt("start")
    agent.follow_up({"role": "user", "content": "next", "timestamp": 2})
    await agent.continue_()

    assert [message["role"] for message in agent.state.messages] == ["user", "assistant", "user", "assistant"]


def test_default_custom_state_and_copy_on_assignment() -> None:
    default = Agent()
    assert default.state.systemPrompt == ""
    assert default.state.model["id"] == "unknown"
    assert default.state.thinkingLevel == "off"
    assert default.state.messages == [] and default.state.tools == []

    source_messages = [{"role": "user", "content": "hello", "timestamp": 1}]
    source_tools: list[Any] = []
    custom = Agent(
        initial_state={"model": model(), "messages": source_messages, "tools": source_tools, "thinkingLevel": "high"}
    )
    source_messages.clear()
    assert len(custom.state.messages) == 1
    replacement = [{"role": "user", "content": "other", "timestamp": 2}]
    custom.state.messages = replacement
    replacement.clear()
    assert len(custom.state.messages) == 1
    assert custom.state.thinkingLevel == "high"


def test_nullish_initial_state_and_options_use_upstream_defaults() -> None:
    agent = Agent(
        {
            "initialState": {
                "systemPrompt": None,
                "model": None,
                "thinkingLevel": None,
                "tools": None,
                "messages": None,
            },
            "steeringMode": None,
            "followUpMode": None,
            "transport": None,
            "toolExecution": None,
        }
    )
    assert agent.state.systemPrompt == ""
    assert agent.state.model["id"] == "unknown"
    assert agent.state.thinkingLevel == "off"
    assert agent.state.tools == [] and agent.state.messages == []
    assert agent.steeringMode == "one-at-a-time"
    assert agent.followUpMode == "one-at-a-time"
    assert agent.transport == "auto"
    assert agent.toolExecution == "parallel"


@pytest.mark.asyncio
async def test_one_argument_subscriber_and_failure_lifecycle() -> None:
    events: list[str] = []

    async def broken(*_: Any) -> AssistantMessageEventStream:
        raise RuntimeError("provider exploded")

    agent = Agent(initial_state={"model": model()}, stream_fn=broken)
    agent.subscribe(lambda event: events.append(event["type"]))
    await agent.prompt("hello")

    assert events == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert agent.state.messages[-1]["stopReason"] == "error"
    assert agent.state.errorMessage == "provider exploded"


@pytest.mark.asyncio
async def test_agent_forwards_session_options_and_active_signal() -> None:
    captured: dict[str, Any] = {}

    def stream_fn(_model: Any, _context: Any, options: dict[str, Any]) -> AssistantMessageEventStream:
        captured.update(options)
        return completed_stream(assistant([{"type": "text", "text": "ok"}]))

    signals: list[Any] = []
    agent = Agent(
        initial_state={"model": model(), "thinkingLevel": "high"},
        stream_fn=stream_fn,
        session_id="session-42",
        thinking_budgets={"high": 9000},
        transport="sse",
        max_retry_delay_ms=321,
    )
    agent.subscribe(lambda _event, signal: signals.append(signal))
    await agent.prompt("hello")
    assert captured["sessionId"] == "session-42"
    assert captured["reasoning"] == "high"
    assert captured["thinkingBudgets"] == {"high": 9000}
    assert captured["transport"] == "sse"
    assert captured["maxRetryDelayMs"] == 321
    assert signals and all(signal is signals[0] for signal in signals)
    assert agent.signal is None


@pytest.mark.asyncio
async def test_abort_and_wait_for_idle_include_listener_settlement() -> None:
    entered = asyncio.Event()
    listener_release = asyncio.Event()
    captured_signal: Any = None

    async def stream_fn(_model: Any, _context: Any, options: dict[str, Any]) -> AssistantMessageEventStream:
        nonlocal captured_signal
        captured_signal = options["signal"]
        entered.set()
        await captured_signal.wait()
        return completed_stream(assistant([], "aborted"))

    agent = Agent(initial_state={"model": model()}, stream_fn=stream_fn)

    async def listener(event: dict[str, Any]) -> None:
        if event["type"] == "agent_end":
            await listener_release.wait()

    agent.subscribe(listener)
    task = asyncio.create_task(agent.prompt("hello"))
    await entered.wait()
    agent.abort()
    assert captured_signal.aborted
    idle = asyncio.create_task(agent.wait_for_idle())
    await asyncio.sleep(0)
    assert not idle.done()
    listener_release.set()
    await task
    await idle
    assert not agent.state.isStreaming
    assert agent.state.messages[-1]["stopReason"] == "aborted"


@pytest.mark.asyncio
async def test_prepare_next_turn_legacy_signal_and_camel_mutators() -> None:
    calls: list[Any] = []
    tool = AgentTool(
        "noop",
        "Noop",
        "",
        {"type": "object"},
        lambda *_: {"content": [{"type": "text", "text": "ok"}], "details": {}},
    )
    responses = iter(
        [
            assistant([{"type": "toolCall", "id": "t", "name": "noop", "arguments": {}}], "toolUse"),
            assistant([{"type": "text", "text": "done"}]),
        ]
    )

    def stream_fn(*_: Any) -> AssistantMessageEventStream:
        return completed_stream(next(responses))

    async def prepare(signal: Any) -> None:
        calls.append(signal)
        return None

    agent = Agent(initial_state={"model": model(), "tools": [tool]}, stream_fn=stream_fn)
    agent.prepareNextTurn = prepare
    agent.steeringMode = "all"
    agent.followUpMode = "all"
    assert agent.prepareNextTurn is prepare
    assert agent.steeringMode == "all" and agent.followUpMode == "all"
    await agent.prompt("go")
    assert calls and calls[0].aborted is False


@pytest.mark.asyncio
async def test_subscribers_follow_live_javascript_set_iteration_semantics() -> None:
    def stream_fn(*_: Any) -> AssistantMessageEventStream:
        return completed_stream(assistant([{"type": "text", "text": "ok"}]))

    agent = Agent(initial_state={"model": model()}, stream_fn=stream_fn)
    calls: list[tuple[str, str]] = []
    remove_second: Any = None

    def third(event: dict[str, Any]) -> None:
        calls.append(("third", event["type"]))

    def first(event: dict[str, Any]) -> None:
        calls.append(("first", event["type"]))
        if event["type"] == "agent_start":
            remove_second()
            agent.subscribe(third)

    def second(event: dict[str, Any]) -> None:
        calls.append(("second", event["type"]))

    agent.subscribe(first)
    remove_second = agent.subscribe(second)
    agent.subscribe(first)  # Set.add of the same callback is a no-op.
    await agent.prompt("hello")

    assert calls[:2] == [("first", "agent_start"), ("third", "agent_start")]
    assert all(name != "second" for name, _event in calls)
    assert sum(name == "first" for name, _event in calls) == 8


@pytest.mark.asyncio
async def test_tool_updates_are_ignored_after_settlement() -> None:
    delayed_update: Any = None
    events: list[dict[str, Any]] = []

    async def execute(_id: str, _args: object, _signal: object, update: Any) -> dict[str, Any]:
        nonlocal delayed_update
        delayed_update = update
        update({"content": [{"type": "text", "text": "running"}], "details": {"status": "running"}})
        return {
            "content": [{"type": "text", "text": "done"}],
            "details": {"status": "done"},
            "terminate": True,
        }

    tool = AgentTool("delayed", "Delayed", "", {"type": "object"}, execute)
    response = assistant(
        [{"type": "toolCall", "id": "call-1", "name": "delayed", "arguments": {}}],
        "toolUse",
    )
    agent = Agent(initial_state={"model": model(), "tools": [tool]}, stream_fn=lambda *_: completed_stream(response))
    agent.subscribe(lambda event: events.append(event))
    await agent.prompt("run")
    event_count = len(events)

    delayed_update({"content": [{"type": "text", "text": "late"}], "details": {"status": "late"}})
    await asyncio.sleep(0)
    assert len(events) == event_count
    assert len([event for event in events if event["type"] == "tool_execution_update"]) == 1


@pytest.mark.asyncio
async def test_settled_parallel_tool_rejects_late_update_while_peer_is_running() -> None:
    slow_started = asyncio.Event()
    settled_ended = asyncio.Event()
    release_slow = asyncio.Event()
    settled_update: Any = None
    events: list[dict[str, Any]] = []

    async def settled(_id: str, _args: object, _signal: object, update: Any) -> dict[str, Any]:
        nonlocal settled_update
        settled_update = update
        return {"content": [], "details": {}, "terminate": True}

    async def slow(*_: Any) -> dict[str, Any]:
        slow_started.set()
        await release_slow.wait()
        return {"content": [], "details": {}, "terminate": True}

    tools = [
        AgentTool("settled", "Settled", "", {"type": "object"}, settled),
        AgentTool("slow", "Slow", "", {"type": "object"}, slow),
    ]
    response = assistant(
        [
            {"type": "toolCall", "id": "one", "name": "settled", "arguments": {}},
            {"type": "toolCall", "id": "two", "name": "slow", "arguments": {}},
        ],
        "toolUse",
    )
    agent = Agent(initial_state={"model": model(), "tools": tools}, stream_fn=lambda *_: completed_stream(response))

    def listen(event: dict[str, Any]) -> None:
        events.append(event)
        if event["type"] == "tool_execution_end" and event["toolCallId"] == "one":
            settled_ended.set()

    agent.subscribe(listen)
    task = asyncio.create_task(agent.prompt("run"))
    await asyncio.gather(slow_started.wait(), settled_ended.wait())
    count = len(events)
    settled_update({"content": [], "details": {"late": True}})
    await asyncio.sleep(0)
    assert len(events) == count
    release_slow.set()
    await task


@pytest.mark.asyncio
async def test_busy_guards_and_one_at_a_time_steering_from_assistant_tail() -> None:
    entered = asyncio.Event()

    async def waiting_stream(_model: Any, _context: Any, options: dict[str, Any]) -> AssistantMessageEventStream:
        entered.set()
        await options["signal"].wait()
        return completed_stream(assistant([], "aborted"))

    busy = Agent(initial_state={"model": model()}, stream_fn=waiting_stream)
    running = asyncio.create_task(busy.prompt("first"))
    await entered.wait()
    with pytest.raises(RuntimeError, match="already processing a prompt"):
        await busy.prompt("second")
    with pytest.raises(RuntimeError, match="already processing"):
        await busy.continue_()
    busy.abort()
    await running

    responses = iter(
        [
            assistant([{"type": "text", "text": "one"}]),
            assistant([{"type": "text", "text": "two"}]),
        ]
    )
    agent = Agent(
        initial_state={"model": model(), "messages": [{"role": "assistant", "content": [], "timestamp": 1}]},
        stream_fn=lambda *_: completed_stream(next(responses)),
    )
    agent.steer({"role": "user", "content": "first", "timestamp": 2})
    agent.steer({"role": "user", "content": "second", "timestamp": 3})
    await agent.continue_()
    assert [message["role"] for message in agent.state.messages[-4:]] == ["user", "assistant", "user", "assistant"]
