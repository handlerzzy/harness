from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, cast

from ._utils import get_any_field, maybe_await, now_ms
from .abort import AbortSignal
from .event_stream import EventStream
from .schema import validate_tool_arguments
from .types import (
    AgentContext,
    AgentEvent,
    AgentEventSink,
    AgentLoopConfig,
    AgentMessage,
    AgentToolResult,
    StreamFn,
    context_from_value,
)


def _get_role(message: Mapping[str, Any]) -> str | None:
    return cast(str | None, message.get("role"))


def _tool_field(tool: object, snake: str, camel: str | None = None, default: Any = None) -> Any:
    return get_any_field(tool, snake, *(tuple([camel]) if camel else ()), default=default)


def _tool_by_name(context: AgentContext, name: str) -> object | None:
    return next((tool for tool in context.tools or [] if _tool_field(tool, "name") == name), None)


async def _emit(sink: AgentEventSink, event: AgentEvent) -> None:
    await maybe_await(sink(event))


async def _invoke_with_signal(callback: Callable[..., Any], payload: dict[str, Any], signal: AbortSignal | None) -> Any:
    """Call a TS-shaped hook while allowing an idiomatic one-argument Python hook."""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        value = callback(payload, signal)
    else:
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
        ]
        has_varargs = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        value = callback(payload, signal) if has_varargs or len(positional) >= 2 else callback(payload)
    return await maybe_await(value)


async def _invoke_tool_execute(
    callback: Callable[..., Any],
    tool_call_id: str,
    arguments: Any,
    signal: AbortSignal | None,
    update: Callable[[AgentToolResult], object],
) -> AgentToolResult:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        value = callback(tool_call_id, arguments, signal, update)
    else:
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
        ]
        has_varargs = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        args = (tool_call_id, arguments, signal, update)
        value = callback(*args if has_varargs else args[: len(positional)])
    return cast(AgentToolResult, await maybe_await(value))


async def stream_simple(
    model: dict[str, Any], context: dict[str, Any], options: dict[str, Any]
) -> AsyncIterable[dict[str, Any]]:
    stream = model.get("stream_fn", model.get("stream"))
    if not callable(stream):
        raise RuntimeError(
            "No default Python model provider is configured. Pass stream_fn to Agent/agent_loop "
            "or put a callable under model['stream_fn']."
        )
    return cast(AsyncIterable[dict[str, Any]], await maybe_await(stream(model, context, options)))


def _create_agent_stream() -> EventStream[AgentEvent, list[AgentMessage]]:
    return EventStream(
        lambda event: event.get("type") == "agent_end",
        lambda event: cast(list[AgentMessage], event.get("messages", [])),
    )


def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext | Mapping[str, Any],
    config: AgentLoopConfig | Mapping[str, Any],
    signal: AbortSignal | None = None,
    stream_fn: StreamFn | None = None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    stream = _create_agent_stream()

    async def run() -> object:
        try:
            messages = await run_agent_loop(prompts, context, config, stream.push, signal, stream_fn)
            stream.end(messages)
            return messages
        except BaseException as error:
            stream.set_exception(error)
            return None

    task = asyncio.create_task(run())
    stream.attach_task(task)
    return stream


def agent_loop_continue(
    context: AgentContext | Mapping[str, Any],
    config: AgentLoopConfig | Mapping[str, Any],
    signal: AbortSignal | None = None,
    stream_fn: StreamFn | None = None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    normalized = context_from_value(context)
    if not normalized.messages:
        raise ValueError("Cannot continue: no messages in context")
    if _get_role(normalized.messages[-1]) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")

    stream = _create_agent_stream()

    async def run() -> object:
        try:
            messages = await run_agent_loop_continue(normalized, config, stream.push, signal, stream_fn)
            stream.end(messages)
            return messages
        except BaseException as error:
            stream.set_exception(error)
            return None

    task = asyncio.create_task(run())
    stream.attach_task(task)
    return stream


async def run_agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext | Mapping[str, Any],
    config: AgentLoopConfig | Mapping[str, Any],
    emit: AgentEventSink,
    signal: AbortSignal | None = None,
    stream_fn: StreamFn | None = None,
) -> list[AgentMessage]:
    source = context_from_value(context)
    current = AgentContext(source.system_prompt, [*source.messages, *prompts], source.tools)
    normalized_config = AgentLoopConfig.from_value(config)
    new_messages = [*prompts]
    await _emit(emit, {"type": "agent_start"})
    await _emit(emit, {"type": "turn_start"})
    for prompt in prompts:
        await _emit(emit, {"type": "message_start", "message": prompt})
        await _emit(emit, {"type": "message_end", "message": prompt})
    await _run_loop(current, new_messages, normalized_config, signal, emit, stream_fn)
    return new_messages


async def run_agent_loop_continue(
    context: AgentContext | Mapping[str, Any],
    config: AgentLoopConfig | Mapping[str, Any],
    emit: AgentEventSink,
    signal: AbortSignal | None = None,
    stream_fn: StreamFn | None = None,
) -> list[AgentMessage]:
    source = context_from_value(context)
    if not source.messages:
        raise ValueError("Cannot continue: no messages in context")
    if _get_role(source.messages[-1]) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")
    current = AgentContext(source.system_prompt, source.messages, source.tools)
    normalized_config = AgentLoopConfig.from_value(config)
    new_messages: list[AgentMessage] = []
    await _emit(emit, {"type": "agent_start"})
    await _emit(emit, {"type": "turn_start"})
    await _run_loop(current, new_messages, normalized_config, signal, emit, stream_fn)
    return new_messages


async def _run_loop(
    current_context: AgentContext,
    new_messages: list[AgentMessage],
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
    stream_fn: StreamFn | None,
) -> None:
    first_turn = True
    pending_messages = (
        await maybe_await(config.get_steering_messages()) if config.get_steering_messages is not None else []
    ) or []

    while True:
        has_more_tool_calls = True
        while has_more_tool_calls or pending_messages:
            if not first_turn:
                await _emit(emit, {"type": "turn_start"})
            else:
                first_turn = False

            if pending_messages:
                for message in pending_messages:
                    await _emit(emit, {"type": "message_start", "message": message})
                    await _emit(emit, {"type": "message_end", "message": message})
                    current_context.messages.append(message)
                    new_messages.append(message)
                pending_messages = []

            message = await _stream_assistant_response(current_context, config, signal, emit, stream_fn)
            new_messages.append(message)
            if message.get("stopReason") in {"error", "aborted"}:
                await _emit(emit, {"type": "turn_end", "message": message, "toolResults": []})
                await _emit(emit, {"type": "agent_end", "messages": new_messages})
                return

            tool_calls = [content for content in message.get("content", []) if content.get("type") == "toolCall"]
            tool_results: list[AgentMessage] = []
            has_more_tool_calls = False
            if tool_calls:
                batch = (
                    await _fail_tool_calls_from_truncated_message(tool_calls, emit)
                    if message.get("stopReason") == "length"
                    else await _execute_tool_calls(current_context, message, config, signal, emit)
                )
                tool_results.extend(batch.messages)
                has_more_tool_calls = not batch.terminate
                for result in tool_results:
                    current_context.messages.append(result)
                    new_messages.append(result)

            await _emit(emit, {"type": "turn_end", "message": message, "toolResults": tool_results})
            next_turn_context = {
                "message": message,
                "toolResults": tool_results,
                "context": current_context,
                "newMessages": new_messages,
            }
            if config.prepare_next_turn is not None:
                snapshot = await maybe_await(config.prepare_next_turn(next_turn_context))
                if snapshot:
                    replacement = snapshot.get("context")
                    if replacement is not None:
                        current_context = context_from_value(replacement)
                    candidate_model = snapshot.get("model")
                    next_model = config.model if candidate_model is None else candidate_model
                    next_reasoning = config.reasoning
                    if "thinkingLevel" in snapshot or "thinking_level" in snapshot:
                        level = snapshot.get("thinkingLevel", snapshot.get("thinking_level"))
                        next_reasoning = None if level == "off" else level
                    config = replace(config, model=next_model, reasoning=next_reasoning)

            if config.should_stop_after_turn is not None:
                stop_context = {
                    "message": message,
                    "toolResults": tool_results,
                    "context": current_context,
                    "newMessages": new_messages,
                }
                if await maybe_await(config.should_stop_after_turn(stop_context)):
                    await _emit(emit, {"type": "agent_end", "messages": new_messages})
                    return

            pending_messages = (
                await maybe_await(config.get_steering_messages()) if config.get_steering_messages is not None else []
            ) or []

        follow_ups = (
            await maybe_await(config.get_follow_up_messages()) if config.get_follow_up_messages is not None else []
        ) or []
        if follow_ups:
            pending_messages = follow_ups
            continue
        break

    await _emit(emit, {"type": "agent_end", "messages": new_messages})


async def _stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
    stream_fn: StreamFn | None,
) -> AgentMessage:
    messages = context.messages
    if config.transform_context is not None:
        messages = await maybe_await(config.transform_context(messages, signal))
    llm_messages = await maybe_await(config.convert_to_llm(messages))
    llm_context = {"systemPrompt": context.system_prompt, "messages": llm_messages, "tools": context.tools}
    api_key = (
        await maybe_await(config.get_api_key(str(config.model.get("provider", "")))) if config.get_api_key else None
    )
    resolved_api_key = api_key or config.api_key
    function = stream_fn or stream_simple
    response = await maybe_await(function(config.model, llm_context, config.stream_options(signal, resolved_api_key)))

    partial_message: AgentMessage | None = None
    added_partial = False
    terminal_event: dict[str, Any] | None = None
    async for event in response:
        event_type = event.get("type")
        if event_type == "start":
            partial_message = cast(AgentMessage, event["partial"])
            context.messages.append(partial_message)
            added_partial = True
            await _emit(emit, {"type": "message_start", "message": dict(partial_message)})
        elif event_type in {
            "text_start",
            "text_delta",
            "text_end",
            "thinking_start",
            "thinking_delta",
            "thinking_end",
            "toolcall_start",
            "toolcall_delta",
            "toolcall_end",
        }:
            if partial_message is not None:
                partial_message = cast(AgentMessage, event["partial"])
                context.messages[-1] = partial_message
                await _emit(
                    emit,
                    {"type": "message_update", "assistantMessageEvent": event, "message": dict(partial_message)},
                )
        elif event_type in {"done", "error"}:
            terminal_event = event
            final_message = await _stream_result(response, terminal_event)
            if added_partial:
                context.messages[-1] = final_message
            else:
                context.messages.append(final_message)
                await _emit(emit, {"type": "message_start", "message": dict(final_message)})
            await _emit(emit, {"type": "message_end", "message": final_message})
            return final_message

    final_message = await _stream_result(response, terminal_event)
    if added_partial:
        context.messages[-1] = final_message
    else:
        context.messages.append(final_message)
        await _emit(emit, {"type": "message_start", "message": dict(final_message)})
    await _emit(emit, {"type": "message_end", "message": final_message})
    return final_message


async def _stream_result(response: object, terminal_event: dict[str, Any] | None) -> AgentMessage:
    result_method = getattr(response, "result", None)
    if callable(result_method):
        return cast(AgentMessage, await maybe_await(result_method()))
    if terminal_event is not None:
        key = "message" if terminal_event.get("type") == "done" else "error"
        return cast(AgentMessage, terminal_event[key])
    raise RuntimeError("Assistant stream ended without a terminal event or result()")


@dataclass(slots=True)
class _ExecutedBatch:
    messages: list[AgentMessage]
    terminate: bool


@dataclass(slots=True)
class _PreparedToolCall:
    tool_call: dict[str, Any]
    tool: object
    args: Any


@dataclass(slots=True)
class _FinalizedToolCall:
    tool_call: dict[str, Any]
    result: AgentToolResult
    is_error: bool


async def _fail_tool_calls_from_truncated_message(
    tool_calls: list[dict[str, Any]], emit: AgentEventSink
) -> _ExecutedBatch:
    messages: list[AgentMessage] = []
    for tool_call in tool_calls:
        await _emit_tool_start(tool_call, emit)
        finalized = _FinalizedToolCall(
            tool_call,
            _create_error_tool_result(
                f'Tool call "{tool_call["name"]}" was not executed: the response hit the output token limit, '
                "so its arguments may be truncated. Re-issue the tool call with complete arguments."
            ),
            True,
        )
        await _emit_tool_execution_end(finalized, emit)
        message = _create_tool_result_message(finalized)
        await _emit_tool_result_message(message, emit)
        messages.append(message)
    return _ExecutedBatch(messages, False)


async def _execute_tool_calls(
    context: AgentContext,
    assistant_message: AgentMessage,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> _ExecutedBatch:
    tool_calls = [item for item in assistant_message.get("content", []) if item.get("type") == "toolCall"]
    force_sequential = any(
        _tool_field(_tool_by_name(context, str(call.get("name"))) or {}, "execution_mode", "executionMode")
        == "sequential"
        for call in tool_calls
    )
    if config.tool_execution == "sequential" or force_sequential:
        return await _execute_tool_calls_sequential(context, assistant_message, tool_calls, config, signal, emit)
    return await _execute_tool_calls_parallel(context, assistant_message, tool_calls, config, signal, emit)


async def _emit_tool_start(tool_call: dict[str, Any], emit: AgentEventSink) -> None:
    await _emit(
        emit,
        {
            "type": "tool_execution_start",
            "toolCallId": tool_call["id"],
            "toolName": tool_call["name"],
            "args": tool_call.get("arguments", {}),
        },
    )


async def _execute_tool_calls_sequential(
    context: AgentContext,
    assistant_message: AgentMessage,
    tool_calls: list[dict[str, Any]],
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> _ExecutedBatch:
    finalized_calls: list[_FinalizedToolCall] = []
    messages: list[AgentMessage] = []
    for tool_call in tool_calls:
        await _emit_tool_start(tool_call, emit)
        preparation = await _prepare_tool_call(context, assistant_message, tool_call, config, signal)
        if isinstance(preparation, _FinalizedToolCall):
            finalized = preparation
        else:
            executed_result, is_error = await _execute_prepared_tool_call(preparation, signal, emit)
            finalized = await _finalize_executed_tool_call(
                context, assistant_message, preparation, executed_result, is_error, config, signal
            )
        await _emit_tool_execution_end(finalized, emit)
        message = _create_tool_result_message(finalized)
        await _emit_tool_result_message(message, emit)
        finalized_calls.append(finalized)
        messages.append(message)
        if signal is not None and signal.aborted:
            break
    return _ExecutedBatch(messages, _should_terminate_tool_batch(finalized_calls))


async def _execute_tool_calls_parallel(
    context: AgentContext,
    assistant_message: AgentMessage,
    tool_calls: list[dict[str, Any]],
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> _ExecutedBatch:
    entries: list[_FinalizedToolCall | _PreparedToolCall] = []
    for tool_call in tool_calls:
        await _emit_tool_start(tool_call, emit)
        preparation = await _prepare_tool_call(context, assistant_message, tool_call, config, signal)
        if isinstance(preparation, _FinalizedToolCall):
            await _emit_tool_execution_end(preparation, emit)
        entries.append(preparation)
        if signal is not None and signal.aborted:
            break

    async def execute(entry: _FinalizedToolCall | _PreparedToolCall) -> _FinalizedToolCall:
        if isinstance(entry, _FinalizedToolCall):
            return entry
        result, is_error = await _execute_prepared_tool_call(entry, signal, emit)
        finalized = await _finalize_executed_tool_call(
            context, assistant_message, entry, result, is_error, config, signal
        )
        await _emit_tool_execution_end(finalized, emit)
        return finalized

    ordered = list(await asyncio.gather(*(execute(entry) for entry in entries)))
    messages: list[AgentMessage] = []
    for finalized in ordered:
        message = _create_tool_result_message(finalized)
        await _emit_tool_result_message(message, emit)
        messages.append(message)
    return _ExecutedBatch(messages, _should_terminate_tool_batch(ordered))


def _should_terminate_tool_batch(calls: list[_FinalizedToolCall]) -> bool:
    return bool(calls) and all(call.result.get("terminate") is True for call in calls)


async def _prepare_tool_call(
    context: AgentContext,
    assistant_message: AgentMessage,
    tool_call: dict[str, Any],
    config: AgentLoopConfig,
    signal: AbortSignal | None,
) -> _PreparedToolCall | _FinalizedToolCall:
    tool = _tool_by_name(context, str(tool_call.get("name")))
    if tool is None:
        return _FinalizedToolCall(tool_call, _create_error_tool_result(f"Tool {tool_call.get('name')} not found"), True)
    try:
        prepare_arguments = _tool_field(tool, "prepare_arguments", "prepareArguments")
        prepared_call = tool_call
        if callable(prepare_arguments):
            prepared_arguments = prepare_arguments(tool_call.get("arguments", {}))
            if prepared_arguments is not tool_call.get("arguments"):
                prepared_call = {**tool_call, "arguments": prepared_arguments}
        validated_args = validate_tool_arguments(tool, prepared_call)
        if config.before_tool_call is not None:
            result = await _invoke_with_signal(
                config.before_tool_call,
                {
                    "assistantMessage": assistant_message,
                    "toolCall": tool_call,
                    "args": validated_args,
                    "context": context,
                },
                signal,
            )
            if signal is not None and signal.aborted:
                return _FinalizedToolCall(tool_call, _create_error_tool_result("Operation aborted"), True)
            if result and result.get("block"):
                return _FinalizedToolCall(
                    tool_call,
                    _create_error_tool_result(result.get("reason") or "Tool execution was blocked"),
                    True,
                )
        if signal is not None and signal.aborted:
            return _FinalizedToolCall(tool_call, _create_error_tool_result("Operation aborted"), True)
        return _PreparedToolCall(tool_call, tool, validated_args)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return _FinalizedToolCall(tool_call, _create_error_tool_result(str(error)), True)


async def _execute_prepared_tool_call(
    prepared: _PreparedToolCall, signal: AbortSignal | None, emit: AgentEventSink
) -> tuple[AgentToolResult, bool]:
    update_tasks: list[asyncio.Task[None]] = []
    accepting_updates = True

    def on_update(partial_result: AgentToolResult) -> None:
        if not accepting_updates:
            return
        event = {
            "type": "tool_execution_update",
            "toolCallId": prepared.tool_call["id"],
            "toolName": prepared.tool_call["name"],
            "args": prepared.tool_call.get("arguments", {}),
            "partialResult": partial_result,
        }
        update_tasks.append(asyncio.create_task(_emit(emit, event)))

    execute = _tool_field(prepared.tool, "execute")
    try:
        if not callable(execute):
            raise TypeError(f"Tool {prepared.tool_call.get('name')} has no executable callback")
        result = await _invoke_tool_execute(execute, prepared.tool_call["id"], prepared.args, signal, on_update)
        accepting_updates = False
        if update_tasks:
            await asyncio.gather(*update_tasks)
        return result, False
    except BaseException as error:
        accepting_updates = False
        if update_tasks:
            await asyncio.gather(*update_tasks)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return _create_error_tool_result(str(error)), True
    finally:
        accepting_updates = False


async def _finalize_executed_tool_call(
    context: AgentContext,
    assistant_message: AgentMessage,
    prepared: _PreparedToolCall,
    result: AgentToolResult,
    is_error: bool,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
) -> _FinalizedToolCall:
    if config.after_tool_call is not None:
        try:
            patch = await _invoke_with_signal(
                config.after_tool_call,
                {
                    "assistantMessage": assistant_message,
                    "toolCall": prepared.tool_call,
                    "args": prepared.args,
                    "result": result,
                    "isError": is_error,
                    "context": context,
                },
                signal,
            )
            if patch:
                merged = dict(result)
                for key in ("content", "details", "terminate"):
                    if key in patch and patch[key] is not None:
                        merged[key] = patch[key]
                result = cast(AgentToolResult, merged)
                if patch.get("isError") is not None:
                    is_error = bool(patch["isError"])
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            result = _create_error_tool_result(str(error))
            is_error = True
    return _FinalizedToolCall(prepared.tool_call, result, is_error)


def _create_error_tool_result(message: str) -> AgentToolResult:
    return {"content": [{"type": "text", "text": message}], "details": {}}


async def _emit_tool_execution_end(finalized: _FinalizedToolCall, emit: AgentEventSink) -> None:
    await _emit(
        emit,
        {
            "type": "tool_execution_end",
            "toolCallId": finalized.tool_call["id"],
            "toolName": finalized.tool_call["name"],
            "result": finalized.result,
            "isError": finalized.is_error,
        },
    )


def _create_tool_result_message(finalized: _FinalizedToolCall) -> AgentMessage:
    message: AgentMessage = {
        "role": "toolResult",
        "toolCallId": finalized.tool_call["id"],
        "toolName": finalized.tool_call["name"],
        "content": [] if finalized.result.get("content") is None else finalized.result["content"],
        "isError": finalized.is_error,
        "timestamp": now_ms(),
    }
    if "details" in finalized.result:
        message["details"] = finalized.result["details"]
    added_names = finalized.result.get("addedToolNames")
    if added_names:
        message["addedToolNames"] = added_names
    return message


async def _emit_tool_result_message(message: AgentMessage, emit: AgentEventSink) -> None:
    await _emit(emit, {"type": "message_start", "message": message})
    await _emit(emit, {"type": "message_end", "message": message})


# TypeScript-compatible aliases.
agentLoop = agent_loop
agentLoopContinue = agent_loop_continue
runAgentLoop = run_agent_loop
runAgentLoopContinue = run_agent_loop_continue
streamSimple = stream_simple
