from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias, TypedDict, cast

from .abort import AbortSignal

ThinkingLevel: TypeAlias = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
ToolExecutionMode: TypeAlias = Literal["sequential", "parallel"]
QueueMode: TypeAlias = Literal["all", "one-at-a-time"]
AgentMessage: TypeAlias = dict[str, Any]
AgentEvent: TypeAlias = dict[str, Any]
Model: TypeAlias = dict[str, Any]
AgentToolCall: TypeAlias = dict[str, Any]
BeforeToolCallResult: TypeAlias = dict[str, Any]
AfterToolCallResult: TypeAlias = dict[str, Any]
BeforeToolCallContext: TypeAlias = dict[str, Any]
AfterToolCallContext: TypeAlias = dict[str, Any]
ShouldStopAfterTurnContext: TypeAlias = dict[str, Any]
AgentLoopTurnUpdate: TypeAlias = dict[str, Any]
PrepareNextTurnContext: TypeAlias = ShouldStopAfterTurnContext
CustomAgentMessages: TypeAlias = dict[str, AgentMessage]


class AgentToolResult(TypedDict, total=False):
    content: list[dict[str, Any]]
    details: Any
    addedToolNames: list[str]
    terminate: bool


AgentToolUpdateCallback: TypeAlias = Callable[[AgentToolResult], object]
ToolExecutor: TypeAlias = Callable[
    [str, Any, AbortSignal | None, AgentToolUpdateCallback | None],
    Awaitable[AgentToolResult] | AgentToolResult,
]


@dataclass(slots=True)
class AgentTool:
    name: str
    label: str
    description: str
    parameters: Mapping[str, Any]
    execute: ToolExecutor
    prepare_arguments: Callable[[Any], Any] | None = None
    execution_mode: ToolExecutionMode | None = None

    @property
    def prepareArguments(self) -> Callable[[Any], Any] | None:
        return self.prepare_arguments

    @prepareArguments.setter
    def prepareArguments(self, value: Callable[[Any], Any] | None) -> None:
        self.prepare_arguments = value

    @property
    def executionMode(self) -> ToolExecutionMode | None:
        return self.execution_mode

    @executionMode.setter
    def executionMode(self, value: ToolExecutionMode | None) -> None:
        self.execution_mode = value


@dataclass(slots=True)
class AgentContext:
    system_prompt: str
    messages: list[AgentMessage]
    tools: list[AgentTool | Mapping[str, Any]] | None = None

    @property
    def systemPrompt(self) -> str:
        return self.system_prompt

    @systemPrompt.setter
    def systemPrompt(self, value: str) -> None:
        self.system_prompt = value


class StreamResponse(Protocol):
    def __aiter__(self) -> AsyncIterable[dict[str, Any]]: ...

    async def result(self) -> AgentMessage: ...


StreamFn: TypeAlias = Callable[
    [Model, dict[str, Any], dict[str, Any]],
    AsyncIterable[dict[str, Any]] | Awaitable[AsyncIterable[dict[str, Any]]],
]
AgentEventSink: TypeAlias = Callable[[AgentEvent], Awaitable[None] | None]


@dataclass(slots=True)
class AgentLoopConfig:
    model: Model
    convert_to_llm: Callable[[list[AgentMessage]], list[AgentMessage] | Awaitable[list[AgentMessage]]]
    transform_context: (
        Callable[[list[AgentMessage], AbortSignal | None], Awaitable[list[AgentMessage]] | list[AgentMessage]] | None
    ) = None
    get_api_key: Callable[[str], str | None | Awaitable[str | None]] | None = None
    should_stop_after_turn: Callable[[dict[str, Any]], bool | Awaitable[bool]] | None = None
    prepare_next_turn: Callable[[dict[str, Any]], dict[str, Any] | None | Awaitable[dict[str, Any] | None]] | None = (
        None
    )
    get_steering_messages: Callable[[], list[AgentMessage] | Awaitable[list[AgentMessage]]] | None = None
    get_follow_up_messages: Callable[[], list[AgentMessage] | Awaitable[list[AgentMessage]]] | None = None
    tool_execution: ToolExecutionMode = "parallel"
    before_tool_call: Callable[..., Any] | None = None
    after_tool_call: Callable[..., Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    api_key: str | None = None
    reasoning: str | None = None
    cache_retention: str | None = None
    session_id: str | None = None
    on_payload: Callable[..., Any] | None = None
    on_response: Callable[..., Any] | None = None
    headers: Mapping[str, str | None] | None = None
    timeout_ms: int | None = None
    websocket_connect_timeout_ms: int | None = None
    max_retries: int | None = None
    transport: str = "auto"
    thinking_budgets: Mapping[str, int] | None = None
    max_retry_delay_ms: int | None = None
    metadata: Mapping[str, Any] | None = None
    env: Mapping[str, str] | None = None
    extra_options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: AgentLoopConfig | Mapping[str, Any]) -> AgentLoopConfig:
        if isinstance(value, cls):
            return value
        mapping = cast(Mapping[str, Any], value)

        aliases = {
            "convert_to_llm": ("convert_to_llm", "convertToLlm"),
            "transform_context": ("transform_context", "transformContext"),
            "get_api_key": ("get_api_key", "getApiKey"),
            "should_stop_after_turn": ("should_stop_after_turn", "shouldStopAfterTurn"),
            "prepare_next_turn": ("prepare_next_turn", "prepareNextTurn"),
            "get_steering_messages": ("get_steering_messages", "getSteeringMessages"),
            "get_follow_up_messages": ("get_follow_up_messages", "getFollowUpMessages"),
            "tool_execution": ("tool_execution", "toolExecution"),
            "before_tool_call": ("before_tool_call", "beforeToolCall"),
            "after_tool_call": ("after_tool_call", "afterToolCall"),
            "max_tokens": ("max_tokens", "maxTokens"),
            "api_key": ("api_key", "apiKey"),
            "cache_retention": ("cache_retention", "cacheRetention"),
            "session_id": ("session_id", "sessionId"),
            "on_payload": ("on_payload", "onPayload"),
            "on_response": ("on_response", "onResponse"),
            "timeout_ms": ("timeout_ms", "timeoutMs"),
            "websocket_connect_timeout_ms": ("websocket_connect_timeout_ms", "websocketConnectTimeoutMs"),
            "max_retries": ("max_retries", "maxRetries"),
            "thinking_budgets": ("thinking_budgets", "thinkingBudgets"),
            "max_retry_delay_ms": ("max_retry_delay_ms", "maxRetryDelayMs"),
        }

        def pick(names: tuple[str, ...], default: Any = None) -> Any:
            return next((mapping[name] for name in names if name in mapping), default)

        known = {name for names in aliases.values() for name in names} | {
            "env",
            "headers",
            "metadata",
            "model",
            "reasoning",
            "temperature",
            "transport",
        }
        return cls(
            model=mapping["model"],
            convert_to_llm=pick(aliases["convert_to_llm"]),
            transform_context=pick(aliases["transform_context"]),
            get_api_key=pick(aliases["get_api_key"]),
            should_stop_after_turn=pick(aliases["should_stop_after_turn"]),
            prepare_next_turn=pick(aliases["prepare_next_turn"]),
            get_steering_messages=pick(aliases["get_steering_messages"]),
            get_follow_up_messages=pick(aliases["get_follow_up_messages"]),
            tool_execution=pick(aliases["tool_execution"], "parallel"),
            before_tool_call=pick(aliases["before_tool_call"]),
            after_tool_call=pick(aliases["after_tool_call"]),
            temperature=mapping.get("temperature"),
            max_tokens=pick(aliases["max_tokens"]),
            api_key=pick(aliases["api_key"]),
            reasoning=mapping.get("reasoning"),
            cache_retention=pick(aliases["cache_retention"]),
            session_id=pick(aliases["session_id"]),
            on_payload=pick(aliases["on_payload"]),
            on_response=pick(aliases["on_response"]),
            headers=mapping.get("headers"),
            timeout_ms=pick(aliases["timeout_ms"]),
            websocket_connect_timeout_ms=pick(aliases["websocket_connect_timeout_ms"]),
            max_retries=pick(aliases["max_retries"]),
            transport=mapping.get("transport", "auto"),
            thinking_budgets=pick(aliases["thinking_budgets"]),
            max_retry_delay_ms=pick(aliases["max_retry_delay_ms"]),
            metadata=mapping.get("metadata"),
            env=mapping.get("env"),
            extra_options={key: item for key, item in mapping.items() if key not in known},
        )

    def stream_options(self, signal: AbortSignal | None, api_key: str | None) -> dict[str, Any]:
        options = dict(self.extra_options)
        values: dict[str, Any] = {
            "model": self.model,
            "convertToLlm": self.convert_to_llm,
            "transformContext": self.transform_context,
            "getApiKey": self.get_api_key,
            "shouldStopAfterTurn": self.should_stop_after_turn,
            "prepareNextTurn": self.prepare_next_turn,
            "getSteeringMessages": self.get_steering_messages,
            "getFollowUpMessages": self.get_follow_up_messages,
            "toolExecution": self.tool_execution,
            "beforeToolCall": self.before_tool_call,
            "afterToolCall": self.after_tool_call,
            "temperature": self.temperature,
            "maxTokens": self.max_tokens,
            "apiKey": api_key,
            "reasoning": self.reasoning,
            "cacheRetention": self.cache_retention,
            "sessionId": self.session_id,
            "onPayload": self.on_payload,
            "onResponse": self.on_response,
            "headers": self.headers,
            "timeoutMs": self.timeout_ms,
            "websocketConnectTimeoutMs": self.websocket_connect_timeout_ms,
            "maxRetries": self.max_retries,
            "transport": self.transport,
            "thinkingBudgets": self.thinking_budgets,
            "maxRetryDelayMs": self.max_retry_delay_ms,
            "metadata": self.metadata,
            "env": self.env,
            "signal": signal,
        }
        options.update(values)
        return options


def context_from_value(value: AgentContext | Mapping[str, Any]) -> AgentContext:
    if isinstance(value, AgentContext):
        return value
    raw_messages = value.get("messages", [])
    messages = raw_messages if isinstance(raw_messages, list) else list(raw_messages)
    raw_tools = value.get("tools")
    tools = raw_tools if isinstance(raw_tools, list) else None if raw_tools is None else list(raw_tools)
    return AgentContext(
        system_prompt=str(value.get("systemPrompt", value.get("system_prompt", ""))),
        messages=messages,
        tools=tools,
    )
