from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from ._utils import maybe_await, now_ms
from .abort import AbortController, AbortSignal
from .agent_loop import run_agent_loop, run_agent_loop_continue, stream_simple
from .types import (
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentTool,
    Model,
    QueueMode,
    StreamFn,
    ThinkingLevel,
    ToolExecutionMode,
)

EMPTY_USAGE = {
    "input": 0,
    "output": 0,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 0,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}

DEFAULT_MODEL: Model = {
    "id": "unknown",
    "name": "unknown",
    "api": "unknown",
    "provider": "unknown",
    "baseUrl": "",
    "reasoning": False,
    "input": [],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "contextWindow": 0,
    "maxTokens": 0,
}


class AgentState:
    """Mutable state with copy-on-assignment semantics for tools and messages."""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        initial = initial or {}

        def pick(*names: str, default: Any = None) -> Any:
            value = next((initial[name] for name in names if name in initial), None)
            return default if value is None else value

        self.system_prompt = cast(str, pick("system_prompt", "systemPrompt", default=""))
        self.model: Model = cast(Model, pick("model", default=DEFAULT_MODEL))
        self.thinking_level: ThinkingLevel = cast(ThinkingLevel, pick("thinking_level", "thinkingLevel", default="off"))
        self._tools: list[AgentTool | Mapping[str, Any]] = list(pick("tools", default=[]))
        self._messages: list[AgentMessage] = list(pick("messages", default=[]))
        self.is_streaming = False
        self.streaming_message: AgentMessage | None = None
        self.pending_tool_calls: frozenset[str] = frozenset()
        self.error_message: str | None = None

    @property
    def tools(self) -> list[AgentTool | Mapping[str, Any]]:
        return self._tools

    @tools.setter
    def tools(self, value: list[AgentTool | Mapping[str, Any]]) -> None:
        self._tools = list(value)

    @property
    def messages(self) -> list[AgentMessage]:
        return self._messages

    @messages.setter
    def messages(self, value: list[AgentMessage]) -> None:
        self._messages = list(value)

    @property
    def systemPrompt(self) -> str:
        return self.system_prompt

    @systemPrompt.setter
    def systemPrompt(self, value: str) -> None:
        self.system_prompt = value

    @property
    def thinkingLevel(self) -> ThinkingLevel:
        return self.thinking_level

    @thinkingLevel.setter
    def thinkingLevel(self, value: ThinkingLevel) -> None:
        self.thinking_level = value

    @property
    def isStreaming(self) -> bool:
        return self.is_streaming

    @property
    def streamingMessage(self) -> AgentMessage | None:
        return self.streaming_message

    @property
    def pendingToolCalls(self) -> frozenset[str]:
        return self.pending_tool_calls

    @property
    def errorMessage(self) -> str | None:
        return self.error_message


@dataclass(slots=True)
class AgentOptions:
    initial_state: Mapping[str, Any] | None = None
    convert_to_llm: Callable[[list[AgentMessage]], list[AgentMessage] | Awaitable[list[AgentMessage]]] | None = None
    transform_context: Callable[..., Any] | None = None
    stream_fn: StreamFn | None = None
    get_api_key: Callable[..., Any] | None = None
    on_payload: Callable[..., Any] | None = None
    on_response: Callable[..., Any] | None = None
    before_tool_call: Callable[..., Any] | None = None
    after_tool_call: Callable[..., Any] | None = None
    prepare_next_turn: Callable[..., Any] | None = None
    prepare_next_turn_with_context: Callable[..., Any] | None = None
    steering_mode: QueueMode = "one-at-a-time"
    follow_up_mode: QueueMode = "one-at-a-time"
    session_id: str | None = None
    thinking_budgets: Mapping[str, int] | None = None
    transport: str = "auto"
    max_retry_delay_ms: int | None = None
    tool_execution: ToolExecutionMode = "parallel"

    @classmethod
    def from_value(cls, value: AgentOptions | Mapping[str, Any] | None = None, **overrides: Any) -> AgentOptions:
        if isinstance(value, cls):
            if not overrides:
                return value
            source = {name: getattr(value, name) for name in cls.__dataclass_fields__}
        else:
            source = dict(cast(Mapping[str, Any], value or {}))
        source.update({key: item for key, item in overrides.items() if item is not None})
        aliases = {
            "initial_state": ("initial_state", "initialState"),
            "convert_to_llm": ("convert_to_llm", "convertToLlm"),
            "transform_context": ("transform_context", "transformContext"),
            "stream_fn": ("stream_fn", "streamFn"),
            "get_api_key": ("get_api_key", "getApiKey"),
            "on_payload": ("on_payload", "onPayload"),
            "on_response": ("on_response", "onResponse"),
            "before_tool_call": ("before_tool_call", "beforeToolCall"),
            "after_tool_call": ("after_tool_call", "afterToolCall"),
            "prepare_next_turn": ("prepare_next_turn", "prepareNextTurn"),
            "prepare_next_turn_with_context": ("prepare_next_turn_with_context", "prepareNextTurnWithContext"),
            "steering_mode": ("steering_mode", "steeringMode"),
            "follow_up_mode": ("follow_up_mode", "followUpMode"),
            "session_id": ("session_id", "sessionId"),
            "thinking_budgets": ("thinking_budgets", "thinkingBudgets"),
            "max_retry_delay_ms": ("max_retry_delay_ms", "maxRetryDelayMs"),
            "tool_execution": ("tool_execution", "toolExecution"),
            "transport": ("transport",),
        }

        def pick(name: str, default: Any = None) -> Any:
            return next((source[key] for key in aliases[name] if key in source), default)

        return cls(
            initial_state=pick("initial_state"),
            convert_to_llm=pick("convert_to_llm"),
            transform_context=pick("transform_context"),
            stream_fn=pick("stream_fn"),
            get_api_key=pick("get_api_key"),
            on_payload=pick("on_payload"),
            on_response=pick("on_response"),
            before_tool_call=pick("before_tool_call"),
            after_tool_call=pick("after_tool_call"),
            prepare_next_turn=pick("prepare_next_turn"),
            prepare_next_turn_with_context=pick("prepare_next_turn_with_context"),
            steering_mode=pick("steering_mode", "one-at-a-time"),
            follow_up_mode=pick("follow_up_mode", "one-at-a-time"),
            session_id=pick("session_id"),
            thinking_budgets=pick("thinking_budgets"),
            transport=pick("transport", "auto"),
            max_retry_delay_ms=pick("max_retry_delay_ms"),
            tool_execution=pick("tool_execution", "parallel"),
        )


class _PendingMessageQueue:
    def __init__(self, mode: QueueMode) -> None:
        self.messages: list[AgentMessage] = []
        self.mode = mode

    def enqueue(self, message: AgentMessage) -> None:
        self.messages.append(message)

    def has_items(self) -> bool:
        return bool(self.messages)

    def drain(self) -> list[AgentMessage]:
        if self.mode == "all":
            drained, self.messages = self.messages, []
            return drained
        if not self.messages:
            return []
        first, self.messages = self.messages[0], self.messages[1:]
        return [first]

    def clear(self) -> None:
        self.messages = []


@dataclass(slots=True)
class _ActiveRun:
    future: asyncio.Future[None]
    abort_controller: AbortController


def _default_convert_to_llm(messages: list[AgentMessage]) -> list[AgentMessage]:
    return [message for message in messages if message.get("role") in {"user", "assistant", "toolResult"}]


class Agent:
    """Stateful wrapper around the low-level agent loop."""

    def __init__(self, options: AgentOptions | Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        resolved = AgentOptions.from_value(options, **kwargs)
        self._state = AgentState(resolved.initial_state)
        self._listeners: list[Callable[[AgentEvent, AbortSignal], Awaitable[None] | None] | None] = []
        self._steering_queue = _PendingMessageQueue(resolved.steering_mode or "one-at-a-time")
        self._follow_up_queue = _PendingMessageQueue(resolved.follow_up_mode or "one-at-a-time")
        self.convert_to_llm = resolved.convert_to_llm or _default_convert_to_llm
        self.transform_context = resolved.transform_context
        self.stream_fn = resolved.stream_fn or stream_simple
        self.get_api_key = resolved.get_api_key
        self.on_payload = resolved.on_payload
        self.on_response = resolved.on_response
        self.before_tool_call = resolved.before_tool_call
        self.after_tool_call = resolved.after_tool_call
        self.prepare_next_turn = resolved.prepare_next_turn
        self.prepare_next_turn_with_context = resolved.prepare_next_turn_with_context
        self._active_run: _ActiveRun | None = None
        self.session_id = resolved.session_id
        self.thinking_budgets = resolved.thinking_budgets
        self.transport = resolved.transport or "auto"
        self.max_retry_delay_ms = resolved.max_retry_delay_ms
        self.tool_execution = resolved.tool_execution or "parallel"

    @property
    def state(self) -> AgentState:
        return self._state

    def subscribe(self, listener: Callable[[AgentEvent, AbortSignal], Awaitable[None] | None]) -> Callable[[], None]:
        if not any(current is listener for current in self._listeners):
            self._listeners.append(listener)

        def unsubscribe() -> None:
            for index, current in enumerate(self._listeners):
                if current is listener:
                    self._listeners[index] = None
                    return

        return unsubscribe

    @property
    def steering_mode(self) -> QueueMode:
        return self._steering_queue.mode

    @steering_mode.setter
    def steering_mode(self, mode: QueueMode) -> None:
        self._steering_queue.mode = mode

    @property
    def follow_up_mode(self) -> QueueMode:
        return self._follow_up_queue.mode

    @follow_up_mode.setter
    def follow_up_mode(self, mode: QueueMode) -> None:
        self._follow_up_queue.mode = mode

    def steer(self, message: AgentMessage) -> None:
        self._steering_queue.enqueue(message)

    def follow_up(self, message: AgentMessage) -> None:
        self._follow_up_queue.enqueue(message)

    def clear_steering_queue(self) -> None:
        self._steering_queue.clear()

    def clear_follow_up_queue(self) -> None:
        self._follow_up_queue.clear()

    def clear_all_queues(self) -> None:
        self.clear_steering_queue()
        self.clear_follow_up_queue()

    def has_queued_messages(self) -> bool:
        return self._steering_queue.has_items() or self._follow_up_queue.has_items()

    @property
    def signal(self) -> AbortSignal | None:
        return self._active_run.abort_controller.signal if self._active_run else None

    def abort(self) -> None:
        if self._active_run:
            self._active_run.abort_controller.abort()

    async def wait_for_idle(self) -> None:
        active = self._active_run
        if active:
            await asyncio.shield(active.future)

    def reset(self) -> None:
        self._state.messages = []
        self._state.is_streaming = False
        self._state.streaming_message = None
        self._state.pending_tool_calls = frozenset()
        self._state.error_message = None
        self.clear_all_queues()

    async def prompt(
        self,
        input: str | AgentMessage | list[AgentMessage],
        images: list[dict[str, Any]] | None = None,
    ) -> None:
        if self._active_run:
            raise RuntimeError(
                "Agent is already processing a prompt. Use steer() or followUp() to queue messages, or wait for completion."
            )
        await self._run_prompt_messages(self._normalize_prompt_input(input, images))

    async def continue_(self) -> None:
        if self._active_run:
            raise RuntimeError("Agent is already processing. Wait for completion before continuing.")
        if not self._state.messages:
            raise RuntimeError("No messages to continue from")
        last_message = self._state.messages[-1]
        if last_message.get("role") == "assistant":
            steering = self._steering_queue.drain()
            if steering:
                await self._run_prompt_messages(steering, skip_initial_steering_poll=True)
                return
            follow_ups = self._follow_up_queue.drain()
            if follow_ups:
                await self._run_prompt_messages(follow_ups)
                return
            raise RuntimeError("Cannot continue from message role: assistant")
        await self._run_continuation()

    def _normalize_prompt_input(
        self,
        input: str | AgentMessage | list[AgentMessage],
        images: list[dict[str, Any]] | None,
    ) -> list[AgentMessage]:
        if isinstance(input, list):
            return input
        if isinstance(input, dict):
            return [input]
        content: list[dict[str, Any]] = [{"type": "text", "text": input}]
        if images:
            content.extend(images)
        return [{"role": "user", "content": content, "timestamp": now_ms()}]

    async def _run_prompt_messages(
        self, messages: list[AgentMessage], *, skip_initial_steering_poll: bool = False
    ) -> None:
        async def execute(signal: AbortSignal) -> None:
            await run_agent_loop(
                messages,
                self._create_context_snapshot(),
                self._create_loop_config(skip_initial_steering_poll),
                self._process_event,
                signal,
                self.stream_fn,
            )

        await self._run_with_lifecycle(execute)

    async def _run_continuation(self) -> None:
        async def execute(signal: AbortSignal) -> None:
            await run_agent_loop_continue(
                self._create_context_snapshot(),
                self._create_loop_config(),
                self._process_event,
                signal,
                self.stream_fn,
            )

        await self._run_with_lifecycle(execute)

    def _create_context_snapshot(self) -> AgentContext:
        return AgentContext(
            self._state.system_prompt,
            list(self._state.messages),
            list(self._state.tools),
        )

    def _create_loop_config(self, skip_initial_steering_poll: bool = False) -> AgentLoopConfig:
        skip = skip_initial_steering_poll

        async def get_steering_messages() -> list[AgentMessage]:
            nonlocal skip
            if skip:
                skip = False
                return []
            return self._steering_queue.drain()

        async def prepare_next_turn(context: dict[str, Any]) -> dict[str, Any] | None:
            if self.prepare_next_turn_with_context:
                return cast(
                    dict[str, Any] | None,
                    await self._call_optional_signal(self.prepare_next_turn_with_context, context),
                )
            if self.prepare_next_turn:
                return cast(dict[str, Any] | None, await self._call_optional_signal(self.prepare_next_turn))
            return None

        return AgentLoopConfig(
            model=self._state.model,
            reasoning=None if self._state.thinking_level == "off" else self._state.thinking_level,
            session_id=self.session_id,
            on_payload=self.on_payload,
            on_response=self.on_response,
            transport=self.transport,
            thinking_budgets=self.thinking_budgets,
            max_retry_delay_ms=self.max_retry_delay_ms,
            tool_execution=self.tool_execution,
            before_tool_call=self.before_tool_call,
            after_tool_call=self.after_tool_call,
            prepare_next_turn=prepare_next_turn
            if self.prepare_next_turn or self.prepare_next_turn_with_context
            else None,
            convert_to_llm=self.convert_to_llm,
            transform_context=self.transform_context,
            get_api_key=self.get_api_key,
            get_steering_messages=get_steering_messages,
            get_follow_up_messages=lambda: self._follow_up_queue.drain(),
        )

    async def _call_optional_signal(self, callback: Callable[..., Any], *args: Any) -> Any:
        import inspect

        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            value = callback(*args, self.signal)
        else:
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
            ]
            has_varargs = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
            value = callback(*args, self.signal) if has_varargs or len(positional) > len(args) else callback(*args)
        return await maybe_await(value)

    async def _run_with_lifecycle(self, executor: Callable[[AbortSignal], Awaitable[None]]) -> None:
        if self._active_run:
            raise RuntimeError("Agent is already processing.")
        controller = AbortController()
        future = asyncio.get_running_loop().create_future()
        self._active_run = _ActiveRun(future, controller)
        self._state.is_streaming = True
        self._state.streaming_message = None
        self._state.error_message = None
        try:
            await executor(controller.signal)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            await self._handle_run_failure(error, controller.signal.aborted)
        finally:
            self._finish_run()

    async def _handle_run_failure(self, error: BaseException, aborted: bool) -> None:
        message: AgentMessage = {
            "role": "assistant",
            "content": [{"type": "text", "text": ""}],
            "api": self._state.model.get("api", "unknown"),
            "provider": self._state.model.get("provider", "unknown"),
            "model": self._state.model.get("id", "unknown"),
            "usage": EMPTY_USAGE.copy(),
            "stopReason": "aborted" if aborted else "error",
            "errorMessage": str(error),
            "timestamp": now_ms(),
        }
        await self._process_event({"type": "message_start", "message": message})
        await self._process_event({"type": "message_end", "message": message})
        await self._process_event({"type": "turn_end", "message": message, "toolResults": []})
        await self._process_event({"type": "agent_end", "messages": [message]})

    def _finish_run(self) -> None:
        active = self._active_run
        self._state.is_streaming = False
        self._state.streaming_message = None
        self._state.pending_tool_calls = frozenset()
        if active and not active.future.done():
            active.future.set_result(None)
        self._active_run = None

    async def _process_event(self, event: AgentEvent) -> None:
        event_type = event.get("type")
        if event_type in {"message_start", "message_update"}:
            self._state.streaming_message = event["message"]
        elif event_type == "message_end":
            self._state.streaming_message = None
            self._state.messages.append(event["message"])
        elif event_type == "tool_execution_start":
            self._state.pending_tool_calls = self._state.pending_tool_calls | {event["toolCallId"]}
        elif event_type == "tool_execution_end":
            self._state.pending_tool_calls = self._state.pending_tool_calls - {event["toolCallId"]}
        elif event_type == "turn_end":
            message = event["message"]
            if message.get("role") == "assistant" and message.get("errorMessage"):
                self._state.error_message = message["errorMessage"]
        elif event_type == "agent_end":
            self._state.streaming_message = None

        active = self._active_run
        if active is None:
            raise RuntimeError("Agent listener invoked outside active run")
        # JavaScript Set iterators are live: removals suppress a not-yet-run
        # listener, and listeners appended during dispatch are visited later in
        # the same dispatch. Tombstones preserve those semantics in Python.
        index = 0
        while index < len(self._listeners):
            listener = self._listeners[index]
            index += 1
            if listener is not None:
                await self._call_optional_signal(listener, event)

    # TypeScript-compatible properties and method aliases.
    @property
    def steeringMode(self) -> QueueMode:
        return self.steering_mode

    @steeringMode.setter
    def steeringMode(self, value: QueueMode) -> None:
        self.steering_mode = value

    @property
    def followUpMode(self) -> QueueMode:
        return self.follow_up_mode

    @followUpMode.setter
    def followUpMode(self, value: QueueMode) -> None:
        self.follow_up_mode = value

    @property
    def sessionId(self) -> str | None:
        return self.session_id

    @sessionId.setter
    def sessionId(self, value: str | None) -> None:
        self.session_id = value

    @property
    def thinkingBudgets(self) -> Mapping[str, int] | None:
        return self.thinking_budgets

    @thinkingBudgets.setter
    def thinkingBudgets(self, value: Mapping[str, int] | None) -> None:
        self.thinking_budgets = value

    @property
    def maxRetryDelayMs(self) -> int | None:
        return self.max_retry_delay_ms

    @maxRetryDelayMs.setter
    def maxRetryDelayMs(self, value: int | None) -> None:
        self.max_retry_delay_ms = value

    @property
    def toolExecution(self) -> ToolExecutionMode:
        return self.tool_execution

    @toolExecution.setter
    def toolExecution(self, value: ToolExecutionMode) -> None:
        self.tool_execution = value

    @property
    def convertToLlm(self) -> Callable[[list[AgentMessage]], list[AgentMessage] | Awaitable[list[AgentMessage]]]:
        return self.convert_to_llm

    @convertToLlm.setter
    def convertToLlm(
        self, value: Callable[[list[AgentMessage]], list[AgentMessage] | Awaitable[list[AgentMessage]]]
    ) -> None:
        self.convert_to_llm = value

    @property
    def transformContext(self) -> Callable[..., Any] | None:
        return self.transform_context

    @transformContext.setter
    def transformContext(self, value: Callable[..., Any] | None) -> None:
        self.transform_context = value

    @property
    def streamFn(self) -> StreamFn:
        return self.stream_fn

    @streamFn.setter
    def streamFn(self, value: StreamFn) -> None:
        self.stream_fn = value

    @property
    def getApiKey(self) -> Callable[..., Any] | None:
        return self.get_api_key

    @getApiKey.setter
    def getApiKey(self, value: Callable[..., Any] | None) -> None:
        self.get_api_key = value

    @property
    def onPayload(self) -> Callable[..., Any] | None:
        return self.on_payload

    @onPayload.setter
    def onPayload(self, value: Callable[..., Any] | None) -> None:
        self.on_payload = value

    @property
    def onResponse(self) -> Callable[..., Any] | None:
        return self.on_response

    @onResponse.setter
    def onResponse(self, value: Callable[..., Any] | None) -> None:
        self.on_response = value

    @property
    def beforeToolCall(self) -> Callable[..., Any] | None:
        return self.before_tool_call

    @beforeToolCall.setter
    def beforeToolCall(self, value: Callable[..., Any] | None) -> None:
        self.before_tool_call = value

    @property
    def afterToolCall(self) -> Callable[..., Any] | None:
        return self.after_tool_call

    @afterToolCall.setter
    def afterToolCall(self, value: Callable[..., Any] | None) -> None:
        self.after_tool_call = value

    @property
    def prepareNextTurn(self) -> Callable[..., Any] | None:
        return self.prepare_next_turn

    @prepareNextTurn.setter
    def prepareNextTurn(self, value: Callable[..., Any] | None) -> None:
        self.prepare_next_turn = value

    @property
    def prepareNextTurnWithContext(self) -> Callable[..., Any] | None:
        return self.prepare_next_turn_with_context

    @prepareNextTurnWithContext.setter
    def prepareNextTurnWithContext(self, value: Callable[..., Any] | None) -> None:
        self.prepare_next_turn_with_context = value

    followUp = follow_up
    clearSteeringQueue = clear_steering_queue
    clearFollowUpQueue = clear_follow_up_queue
    clearAllQueues = clear_all_queues
    hasQueuedMessages = has_queued_messages
    waitForIdle = wait_for_idle


# ``continue`` is a Python keyword, so normal calls use ``continue_``. The exact
# upstream member remains available through ``getattr(agent, "continue")``.
setattr(Agent, "continue", Agent.continue_)
