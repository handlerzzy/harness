from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from .._utils import JS_UNDEFINED, get_any_field, maybe_await, now_ms
from ..abort import AbortController, AbortSignal
from ..agent_loop import run_agent_loop
from ..types import AgentContext, AgentMessage
from .compaction.branch_summarization import collect_entries_for_branch_summary, generate_branch_summary
from .compaction.compaction import DEFAULT_COMPACTION_SETTINGS, prepare_compaction
from .compaction.compaction import compact as compact_session
from .messages import convert_to_llm
from .prompt_templates import format_prompt_template_invocation
from .skills import format_skill_invocation
from .types import (
    AgentHarnessError,
    BranchSummaryError,
    CompactionError,
    SessionError,
    to_error,
)

SUBSCRIBER_EVENT_TYPE = "*"
_NO_SIGNAL = object()


def _field(value: Any, snake: str, camel: str | None = None, default: Any = None) -> Any:
    return get_any_field(value, snake, *(tuple([camel]) if camel else ()), default=default)


def _create_user_message(text: str, images: list[dict[str, Any]] | None = None) -> AgentMessage:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if images:
        content.extend(images)
    return {"role": "user", "content": content, "timestamp": now_ms()}


def _create_failure_message(model: Any, error: object, aborted: bool) -> AgentMessage:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": ""}],
        "api": _field(model, "api", default=""),
        "provider": _field(model, "provider", default=""),
        "model": _field(model, "id", default=""),
        "stopReason": "aborted" if aborted else "error",
        "errorMessage": str(error),
        "timestamp": now_ms(),
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
    }


def _clone_stream_options(options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    aliases = {
        "timeout_ms": "timeoutMs",
        "max_retries": "maxRetries",
        "max_retry_delay_ms": "maxRetryDelayMs",
        "cache_retention": "cacheRetention",
    }
    result: dict[str, Any] = {}
    for key, value in dict(options or {}).items():
        result[aliases.get(key, key)] = value
    if result.get("headers") is not None:
        result["headers"] = dict(result["headers"])
    else:
        result["headers"] = None
    if result.get("metadata") is not None:
        result["metadata"] = dict(result["metadata"])
    else:
        result["metadata"] = None
    return result


def _apply_stream_options_patch(base: Mapping[str, Any], patch: Mapping[str, Any] | None) -> dict[str, Any]:
    result = _clone_stream_options(base)
    if patch is None:
        return result
    for key in ("transport", "timeoutMs", "maxRetries", "maxRetryDelayMs", "cacheRetention"):
        snake = {
            "timeoutMs": "timeout_ms",
            "maxRetries": "max_retries",
            "maxRetryDelayMs": "max_retry_delay_ms",
            "cacheRetention": "cache_retention",
        }.get(key, key)
        if key in patch or snake in patch:
            result[key] = patch.get(key, patch.get(snake))
    for key in ("headers", "metadata"):
        if key not in patch:
            continue
        value = patch[key]
        if value is None or value is JS_UNDEFINED:
            result.pop(key, None)
            continue
        merged = dict(result.get(key) or {})
        for item_key, item_value in value.items():
            if item_value is JS_UNDEFINED:
                merged.pop(item_key, None)
            else:
                merged[item_key] = item_value
        if merged:
            result[key] = merged
        else:
            result.pop(key, None)
    return result


def _duplicates(names: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in names:
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    return duplicates


def _normalize_harness_error(error: object, fallback_code: str) -> AgentHarnessError:
    if isinstance(error, AgentHarnessError):
        return error
    cause = to_error(error)
    if isinstance(cause, SessionError):
        return AgentHarnessError("session", str(cause), cause)
    if isinstance(cause, CompactionError):
        return AgentHarnessError("compaction", str(cause), cause)
    if isinstance(cause, BranchSummaryError):
        return AgentHarnessError("branch_summary", str(cause), cause)
    return AgentHarnessError(fallback_code, str(cause), cause)


def _same_handler(left: Callable[..., Any] | None, right: Callable[..., Any]) -> bool:
    if left is right:
        return True
    return (
        left is not None
        and getattr(left, "__self__", _NO_SIGNAL) is getattr(right, "__self__", None)
        and getattr(left, "__func__", _NO_SIGNAL) is getattr(right, "__func__", None)
    )


async def _invoke_handler(
    handler: Callable[..., Any],
    event: dict[str, Any],
    signal: AbortSignal | object | None = _NO_SIGNAL,
) -> Any:
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        value = handler(event) if signal is _NO_SIGNAL else handler(event, signal)
    else:
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
        ]
        has_varargs = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        include_signal = signal is not _NO_SIGNAL and (has_varargs or len(positional) >= 2)
        value = handler(event, signal) if include_signal else handler(event)
    return await maybe_await(value)


class AgentHarness:
    def __init__(self, options: Mapping[str, Any]) -> None:
        self.env = options["env"]
        self._session = options["session"]
        self.models = options["models"]
        initial_resources = dict(options.get("resources") or {})
        if "prompt_templates" in initial_resources and "promptTemplates" not in initial_resources:
            initial_resources["promptTemplates"] = initial_resources.pop("prompt_templates")
        self._resources = initial_resources
        self._stream_options = _clone_stream_options(options.get("streamOptions", options.get("stream_options")))
        self._system_prompt = options.get("systemPrompt", options.get("system_prompt"))
        tools = list(options.get("tools") or [])
        self._validate_unique_names([str(_field(tool, "name")) for tool in tools], "Duplicate tool name(s)")
        self._tools = {str(_field(tool, "name")): tool for tool in tools}
        self._model = options["model"]
        thinking_level = options.get("thinkingLevel", options.get("thinking_level"))
        self._thinking_level = "off" if thinking_level is None else thinking_level
        explicit_active = options.get("activeToolNames", options.get("active_tool_names"))
        self._active_tool_names = list(explicit_active) if explicit_active is not None else list(self._tools)
        self._validate_tool_names(self._active_tool_names)
        steering_mode = options.get("steeringMode", options.get("steering_mode"))
        follow_up_mode = options.get("followUpMode", options.get("follow_up_mode"))
        self._steering_queue_mode = "one-at-a-time" if steering_mode is None else steering_mode
        self._follow_up_queue_mode = "one-at-a-time" if follow_up_mode is None else follow_up_mode
        self._phase = "idle"
        self._run_abort_controller: AbortController | None = None
        self._idle_future: asyncio.Future[None] | None = None
        self._pending_session_writes: list[dict[str, Any]] = []
        self._steer_queue: list[AgentMessage] = []
        self._follow_up_queue: list[AgentMessage] = []
        self._next_turn_queue: list[AgentMessage] = []
        self._handlers: dict[str, list[Callable[..., Any] | None]] = {}

    def _iter_handlers(self, event_type: str) -> Iterator[Callable[..., Any]]:
        handlers = self._handlers.get(event_type, [])
        index = 0
        while index < len(handlers):
            handler = handlers[index]
            index += 1
            if handler is not None:
                yield handler

    async def _emit_own(self, event: dict[str, Any], signal: AbortSignal | None = None) -> None:
        for listener in self._iter_handlers(SUBSCRIBER_EVENT_TYPE):
            try:
                await _invoke_handler(listener, event, signal)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise _normalize_harness_error(error, "hook") from error

    async def _emit_any(self, event: dict[str, Any], signal: AbortSignal | None = None) -> None:
        await self._emit_own(event, signal)

    async def _emit_hook(self, event: dict[str, Any]) -> Any:
        last_result: Any = None
        for handler in self._iter_handlers(str(event["type"])):
            try:
                result = await _invoke_handler(handler, event)
                if result is not None:
                    last_result = result
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise _normalize_harness_error(error, "hook") from error
        return last_result

    async def _emit_before_provider_request(
        self, model: Any, session_id: str, stream_options: Mapping[str, Any]
    ) -> dict[str, Any]:
        current = _clone_stream_options(stream_options)
        for handler in self._iter_handlers("before_provider_request"):
            try:
                result = await _invoke_handler(
                    handler,
                    {
                        "type": "before_provider_request",
                        "model": model,
                        "sessionId": session_id,
                        "streamOptions": _clone_stream_options(current),
                    },
                )
                if result and result.get("streamOptions") is not None:
                    current = _apply_stream_options_patch(current, result["streamOptions"])
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise _normalize_harness_error(error, "hook") from error
        return current

    async def _emit_before_provider_payload(self, model: Any, payload: Any) -> Any:
        current = payload
        for handler in self._iter_handlers("before_provider_payload"):
            try:
                result = await _invoke_handler(
                    handler, {"type": "before_provider_payload", "model": model, "payload": current}
                )
                if result is not None:
                    current = result["payload"]
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise _normalize_harness_error(error, "hook") from error
        return current

    async def _emit_queue_update(self) -> None:
        await self._emit_own(
            {
                "type": "queue_update",
                "steer": list(self._steer_queue),
                "followUp": list(self._follow_up_queue),
                "nextTurn": list(self._next_turn_queue),
            }
        )

    def _start_run_future(self) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._idle_future = future
        controller = AbortController() if self._phase == "turn" else None
        if controller is not None:
            self._run_abort_controller = controller

        def finish() -> None:
            if not future.done():
                future.set_result(None)
            if self._idle_future is future:
                self._idle_future = None
            if self._run_abort_controller is controller:
                self._run_abort_controller = None

        return finish

    async def _create_turn_state(self) -> dict[str, Any]:
        context = await self._session.build_context()
        resources = self.get_resources()
        metadata = await self._session.get_metadata()
        tools = list(self._tools.values())
        active_tools = [self._tools[name] for name in self._active_tool_names if name in self._tools]
        system_prompt = "You are a helpful assistant."
        if isinstance(self._system_prompt, str):
            system_prompt = self._system_prompt
        elif callable(self._system_prompt):
            system_prompt = await maybe_await(
                self._system_prompt(
                    {
                        "env": self.env,
                        "session": self._session,
                        "model": self._model,
                        "thinkingLevel": self._thinking_level,
                        "activeTools": active_tools,
                        "resources": resources,
                    }
                )
            )
        return {
            "messages": context["messages"],
            "resources": resources,
            "streamOptions": _clone_stream_options(self._stream_options),
            "sessionId": metadata["id"],
            "systemPrompt": system_prompt,
            "model": self._model,
            "thinkingLevel": self._thinking_level,
            "tools": tools,
            "activeTools": active_tools,
        }

    @staticmethod
    def _create_context(turn_state: Mapping[str, Any], system_prompt: str | None = None) -> AgentContext:
        return AgentContext(
            system_prompt if system_prompt is not None else turn_state["systemPrompt"],
            list(turn_state["messages"]),
            list(turn_state["activeTools"]),
        )

    def _create_stream_fn(self, get_turn_state: Callable[[], Mapping[str, Any]]) -> Callable[..., Any]:
        async def stream(model: Any, context: dict[str, Any], stream_options: dict[str, Any]) -> Any:
            turn_state = get_turn_state()
            request_options = await self._emit_before_provider_request(
                model, turn_state["sessionId"], turn_state["streamOptions"]
            )

            async def on_payload(payload: Any) -> Any:
                return await self._emit_before_provider_payload(model, payload)

            async def on_response(response: Any) -> None:
                headers = dict(_field(response, "headers", default={}) or {})
                await self._emit_own(
                    {
                        "type": "after_provider_response",
                        "status": _field(response, "status", default=0),
                        "headers": headers,
                    },
                    stream_options.get("signal"),
                )

            provider_options = {
                "cacheRetention": request_options.get("cacheRetention"),
                "headers": request_options.get("headers"),
                "maxRetries": request_options.get("maxRetries"),
                "maxRetryDelayMs": request_options.get("maxRetryDelayMs"),
                "metadata": request_options.get("metadata"),
                "onPayload": on_payload,
                "onResponse": on_response,
                "reasoning": stream_options.get("reasoning"),
                "signal": stream_options.get("signal"),
                "sessionId": turn_state["sessionId"],
                "timeoutMs": request_options.get("timeoutMs"),
                "transport": request_options.get("transport"),
            }
            method = _field(self.models, "stream_simple", "streamSimple")
            if method is None:
                raise TypeError("models must provide stream_simple() or streamSimple()")
            return await maybe_await(method(model, context, provider_options))

        return stream

    async def _drain_queued_messages(self, queue: list[AgentMessage], mode: str) -> list[AgentMessage]:
        count = len(queue) if mode == "all" else min(1, len(queue))
        messages = queue[:count]
        del queue[:count]
        if not messages:
            return messages
        try:
            await self._emit_queue_update()
            return messages
        except BaseException:
            queue[:0] = messages
            raise

    def _create_loop_config(
        self,
        get_turn_state: Callable[[], dict[str, Any]],
        set_turn_state: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        turn_state = get_turn_state()

        async def transform_context(
            messages: list[AgentMessage], _signal: AbortSignal | None = None
        ) -> list[AgentMessage]:
            result = await self._emit_hook({"type": "context", "messages": list(messages)})
            replacement = result.get("messages") if result else None
            return replacement if replacement is not None else messages

        async def before_tool_call(payload: dict[str, Any]) -> dict[str, Any] | None:
            tool_call = payload["toolCall"]
            result = await self._emit_hook(
                {
                    "type": "tool_call",
                    "toolCallId": tool_call["id"],
                    "toolName": tool_call["name"],
                    "input": payload["args"],
                }
            )
            return {"block": result.get("block"), "reason": result.get("reason")} if result else None

        async def after_tool_call(payload: dict[str, Any]) -> dict[str, Any] | None:
            tool_call = payload["toolCall"]
            result = payload["result"]
            patch = await self._emit_hook(
                {
                    "type": "tool_result",
                    "toolCallId": tool_call["id"],
                    "toolName": tool_call["name"],
                    "input": payload["args"],
                    "content": result.get("content"),
                    "details": result.get("details"),
                    "isError": payload["isError"],
                }
            )
            if not patch:
                return None
            return {
                "content": patch.get("content"),
                "details": patch.get("details"),
                "isError": patch.get("isError"),
                "terminate": patch.get("terminate"),
            }

        async def prepare_next_turn(_payload: dict[str, Any] | None = None) -> dict[str, Any]:
            await self._flush_pending_session_writes()
            next_state = await self._create_turn_state()
            set_turn_state(next_state)
            return {
                "context": self._create_context(next_state),
                "model": next_state["model"],
                "thinkingLevel": next_state["thinkingLevel"],
            }

        return {
            "model": turn_state["model"],
            "reasoning": None if turn_state["thinkingLevel"] == "off" else turn_state["thinkingLevel"],
            "convertToLlm": convert_to_llm,
            "transformContext": transform_context,
            "beforeToolCall": before_tool_call,
            "afterToolCall": after_tool_call,
            "prepareNextTurn": prepare_next_turn,
            "getSteeringMessages": lambda: self._drain_queued_messages(self._steer_queue, self._steering_queue_mode),
            "getFollowUpMessages": lambda: self._drain_queued_messages(
                self._follow_up_queue, self._follow_up_queue_mode
            ),
        }

    @staticmethod
    def _validate_unique_names(names: list[str], message: str) -> None:
        duplicates = _duplicates(names)
        if duplicates:
            raise AgentHarnessError("invalid_argument", f"{message}: {', '.join(duplicates)}")

    def _validate_tool_names(self, names: list[str], tools: Mapping[str, Any] | None = None) -> None:
        self._validate_unique_names(names, "Duplicate active tool name(s)")
        source = tools if tools is not None else self._tools
        missing = [name for name in names if name not in source]
        if missing:
            raise AgentHarnessError("invalid_argument", f"Unknown tool(s): {', '.join(missing)}")

    async def _flush_pending_session_writes(self) -> None:
        while self._pending_session_writes:
            write = self._pending_session_writes[0]
            entry_type = write["type"]
            if entry_type == "message":
                await self._session.append_message(write["message"])
            elif entry_type == "model_change":
                await self._session.append_model_change(write["provider"], write["modelId"])
            elif entry_type == "thinking_level_change":
                await self._session.append_thinking_level_change(write["thinkingLevel"])
            elif entry_type == "active_tools_change":
                await self._session.append_active_tools_change(write["activeToolNames"])
            elif entry_type == "custom":
                await self._session.append_custom_entry(write["customType"], write.get("data"))
            elif entry_type == "custom_message":
                await self._session.append_custom_message_entry(
                    write["customType"], write["content"], write["display"], write.get("details")
                )
            elif entry_type == "label":
                await self._session.append_label(write["targetId"], write.get("label"))
            elif entry_type == "session_info":
                await self._session.append_session_name(write.get("name") or "")
            elif entry_type == "leaf":
                await self._session.get_storage().set_leaf_id(write.get("targetId"))
            self._pending_session_writes.pop(0)

    async def _handle_agent_event(self, event: dict[str, Any], signal: AbortSignal | None = None) -> None:
        event_type = event["type"]
        if event_type == "message_end":
            await self._session.append_message(event["message"])
            await self._emit_any(event, signal)
            return
        if event_type == "turn_end":
            event_error: BaseException | None = None
            try:
                await self._emit_any(event, signal)
            except BaseException as error:
                event_error = error
            had_pending = bool(self._pending_session_writes)
            await self._flush_pending_session_writes()
            if event_error:
                raise event_error
            await self._emit_own({"type": "save_point", "hadPendingMutations": had_pending})
            return
        if event_type == "agent_end":
            await self._flush_pending_session_writes()
            self._phase = "idle"
            await self._emit_any(event, signal)
            await self._emit_own({"type": "settled", "nextTurnCount": len(self._next_turn_queue)}, signal)
            return
        await self._emit_any(event, signal)

    async def _emit_run_failure(
        self, model: Any, error: object, aborted: bool, signal: AbortSignal
    ) -> list[AgentMessage]:
        message = _create_failure_message(model, error, aborted)
        await self._handle_agent_event({"type": "message_start", "message": message}, signal)
        await self._handle_agent_event({"type": "message_end", "message": message}, signal)
        await self._handle_agent_event({"type": "turn_end", "message": message, "toolResults": []}, signal)
        await self._handle_agent_event({"type": "agent_end", "messages": [message]}, signal)
        return [message]

    async def _execute_turn(
        self, turn_state: dict[str, Any], text: str, options: Mapping[str, Any] | None = None
    ) -> AgentMessage:
        options = options or {}
        active_state = turn_state
        messages = [_create_user_message(text, options.get("images"))]
        if self._next_turn_queue:
            queued, self._next_turn_queue = self._next_turn_queue, []
            try:
                await self._emit_queue_update()
            except BaseException:
                self._next_turn_queue[:0] = queued
                raise
            messages = [*queued, messages[0]]
        before = await self._emit_hook(
            {
                "type": "before_agent_start",
                "prompt": text,
                "images": options.get("images"),
                "systemPrompt": turn_state["systemPrompt"],
                "resources": turn_state["resources"],
            }
        )
        if before and before.get("messages"):
            messages.extend(before["messages"])
        controller = self._run_abort_controller or AbortController()
        self._run_abort_controller = controller

        def get_state() -> dict[str, Any]:
            return active_state

        def set_state(value: dict[str, Any]) -> None:
            nonlocal active_state
            active_state = value

        try:
            try:
                new_messages = await run_agent_loop(
                    messages,
                    self._create_context(turn_state, before.get("systemPrompt") if before else None),
                    self._create_loop_config(get_state, set_state),
                    lambda event: self._handle_agent_event(event, controller.signal),
                    controller.signal,
                    self._create_stream_fn(get_state),
                )
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                try:
                    new_messages = await self._emit_run_failure(
                        active_state["model"], error, controller.signal.aborted, controller.signal
                    )
                except BaseException as reporting_error:
                    if isinstance(reporting_error, (KeyboardInterrupt, SystemExit)):
                        raise
                    group = ExceptionGroup(
                        "Agent run failed and failure reporting failed",
                        [to_error(error), to_error(reporting_error)],
                    )
                    raise AgentHarnessError(
                        "unknown", "Agent run failed and failure reporting failed", group
                    ) from group
            for message in reversed(new_messages):
                if message.get("role") == "assistant":
                    return message
            raise AgentHarnessError("invalid_state", "AgentHarness prompt completed without an assistant message")
        finally:
            try:
                await self._flush_pending_session_writes()
            finally:
                self._run_abort_controller = None

    async def prompt(self, text: str, options: Mapping[str, Any] | None = None) -> AgentMessage:
        if self._phase != "idle":
            raise AgentHarnessError("busy", "AgentHarness is busy")
        self._phase = "turn"
        finish = self._start_run_future()
        try:
            return await self._execute_turn(await self._create_turn_state(), text, options)
        except BaseException as error:
            self._phase = "idle"
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _normalize_harness_error(error, "unknown") from error
        finally:
            finish()

    async def skill(self, name: str, additional_instructions: str | None = None) -> AgentMessage:
        if self._phase != "idle":
            raise AgentHarnessError("busy", "AgentHarness is busy")
        self._phase = "turn"
        finish = self._start_run_future()
        try:
            turn_state = await self._create_turn_state()
            skill = next(
                (
                    candidate
                    for candidate in (turn_state["resources"].get("skills") or [])
                    if _field(candidate, "name") == name
                ),
                None,
            )
            if skill is None:
                raise AgentHarnessError("invalid_argument", f"Unknown skill: {name}")
            return await self._execute_turn(turn_state, format_skill_invocation(skill, additional_instructions))
        except BaseException as error:
            self._phase = "idle"
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _normalize_harness_error(error, "unknown") from error
        finally:
            finish()

    async def prompt_from_template(self, name: str, args: list[str] | None = None) -> AgentMessage:
        if self._phase != "idle":
            raise AgentHarnessError("busy", "AgentHarness is busy")
        self._phase = "turn"
        finish = self._start_run_future()
        try:
            turn_state = await self._create_turn_state()
            template = next(
                (
                    candidate
                    for candidate in (turn_state["resources"].get("promptTemplates") or [])
                    if _field(candidate, "name") == name
                ),
                None,
            )
            if template is None:
                raise AgentHarnessError("invalid_argument", f"Unknown prompt template: {name}")
            return await self._execute_turn(turn_state, format_prompt_template_invocation(template, args or []))
        except BaseException as error:
            self._phase = "idle"
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _normalize_harness_error(error, "unknown") from error
        finally:
            finish()

    async def steer(self, text: str, options: Mapping[str, Any] | None = None) -> None:
        if self._phase == "idle":
            raise AgentHarnessError("invalid_state", "Cannot steer while idle")
        self._steer_queue.append(_create_user_message(text, (options or {}).get("images")))
        await self._emit_queue_update()

    async def follow_up(self, text: str, options: Mapping[str, Any] | None = None) -> None:
        if self._phase == "idle":
            raise AgentHarnessError("invalid_state", "Cannot follow up while idle")
        self._follow_up_queue.append(_create_user_message(text, (options or {}).get("images")))
        await self._emit_queue_update()

    async def next_turn(self, text: str, options: Mapping[str, Any] | None = None) -> None:
        self._next_turn_queue.append(_create_user_message(text, (options or {}).get("images")))
        await self._emit_queue_update()

    async def append_message(self, message: AgentMessage) -> None:
        try:
            if self._phase == "idle":
                await self._session.append_message(message)
            else:
                self._pending_session_writes.append({"type": "message", "message": message})
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _normalize_harness_error(error, "session") from error

    async def compact(self, custom_instructions: str | None = None) -> dict[str, Any]:
        if self._phase != "idle":
            raise AgentHarnessError("busy", "compact() requires idle harness")
        self._phase = "compaction"
        try:
            if self._model is None:
                raise AgentHarnessError("invalid_state", "No model set for compaction")
            branch_entries = await self._session.get_branch()
            preparation_result = prepare_compaction(branch_entries, DEFAULT_COMPACTION_SETTINGS)
            if not preparation_result["ok"]:
                raise preparation_result["error"]
            preparation = preparation_result["value"]
            if preparation is None:
                raise AgentHarnessError("compaction", "Nothing to compact")
            hook_result = await self._emit_hook(
                {
                    "type": "session_before_compact",
                    "preparation": preparation,
                    "branchEntries": branch_entries,
                    "customInstructions": custom_instructions,
                    "signal": AbortController().signal,
                }
            )
            if hook_result and hook_result.get("cancel"):
                raise AgentHarnessError("compaction", "Compaction cancelled")
            provided = hook_result.get("compaction") if hook_result else None
            result_value: dict[str, Any]
            if provided is not None:
                result_value = provided
            else:
                result = await compact_session(
                    preparation,
                    self.models,
                    self._model,
                    custom_instructions,
                    None,
                    self._thinking_level,
                )
                if not result["ok"]:
                    raise result["error"]
                result_value = result["value"]
            entry_id = await self._session.append_compaction(
                result_value["summary"],
                result_value["firstKeptEntryId"],
                result_value["tokensBefore"],
                result_value.get("details"),
                provided is not None,
            )
            entry = await self._session.get_entry(entry_id)
            if entry and entry.get("type") == "compaction":
                await self._emit_own(
                    {"type": "session_compact", "compactionEntry": entry, "fromHook": provided is not None}
                )
            return result_value
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _normalize_harness_error(error, "compaction") from error
        finally:
            self._phase = "idle"

    async def navigate_tree(self, target_id: str, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self._phase != "idle":
            raise AgentHarnessError("busy", "navigateTree() requires idle harness")
        self._phase = "branch_summary"
        options = options or {}
        try:
            old_leaf_id = await self._session.get_leaf_id()
            if old_leaf_id == target_id:
                return {"cancelled": False}
            target = await self._session.get_entry(target_id)
            if target is None:
                raise AgentHarnessError("invalid_argument", f"Entry {target_id} not found")
            collected = await collect_entries_for_branch_summary(self._session, old_leaf_id, target_id)
            preparation = {
                "targetId": target_id,
                "oldLeafId": old_leaf_id,
                "commonAncestorId": collected["commonAncestorId"],
                "entriesToSummarize": collected["entries"],
                "userWantsSummary": options.get("summarize", False),
                "customInstructions": options.get("customInstructions", options.get("custom_instructions")),
                "replaceInstructions": options.get("replaceInstructions", options.get("replace_instructions")),
                "label": options.get("label"),
            }
            hook = await self._emit_hook(
                {"type": "session_before_tree", "preparation": preparation, "signal": AbortController().signal}
            )
            if hook and hook.get("cancel"):
                return {"cancelled": True}
            hook_summary = hook.get("summary") if hook else None
            summary_text = hook_summary.get("summary") if hook_summary else None
            summary_details = hook_summary.get("details") if hook_summary else None
            if not summary_text and options.get("summarize") and collected["entries"]:
                if self._model is None:
                    raise AgentHarnessError("invalid_state", "No model set for branch summary")
                branch_result = await generate_branch_summary(
                    collected["entries"],
                    {
                        "models": self.models,
                        "model": self._model,
                        "signal": AbortController().signal,
                        "customInstructions": (
                            (hook or {}).get("customInstructions")
                            if (hook or {}).get("customInstructions") is not None
                            else options.get("customInstructions", options.get("custom_instructions"))
                        ),
                        "replaceInstructions": (
                            (hook or {}).get("replaceInstructions")
                            if (hook or {}).get("replaceInstructions") is not None
                            else options.get("replaceInstructions", options.get("replace_instructions"))
                        ),
                    },
                )
                if not branch_result["ok"]:
                    if branch_result["error"].code == "aborted":
                        return {"cancelled": True}
                    raise branch_result["error"]
                summary_text = branch_result["value"]["summary"]
                summary_details = {
                    "readFiles": branch_result["value"]["readFiles"],
                    "modifiedFiles": branch_result["value"]["modifiedFiles"],
                }
            editor_text: str | None = None
            if target.get("type") == "message" and target.get("message", {}).get("role") == "user":
                new_leaf_id = target.get("parentId")
                content = target["message"].get("content", "")
                editor_text = (
                    content
                    if isinstance(content, str)
                    else "".join(str(block.get("text", "")) for block in content if block.get("type") == "text")
                )
            elif target.get("type") == "custom_message":
                new_leaf_id = target.get("parentId")
                content = target.get("content", "")
                editor_text = (
                    content
                    if isinstance(content, str)
                    else "".join(str(block.get("text", "")) for block in content if block.get("type") == "text")
                )
            else:
                new_leaf_id = target_id
            summary_id = await self._session.move_to(
                new_leaf_id,
                (
                    {
                        "summary": summary_text,
                        "details": summary_details,
                        "fromHook": hook_summary is not None,
                    }
                    if summary_text
                    else None
                ),
            )
            summary_entry = await self._session.get_entry(summary_id) if summary_id else None
            await self._emit_own(
                {
                    "type": "session_tree",
                    "newLeafId": await self._session.get_leaf_id(),
                    "oldLeafId": old_leaf_id,
                    "summaryEntry": summary_entry,
                    "fromHook": hook_summary is not None,
                }
            )
            return {
                "cancelled": False,
                "editorText": editor_text,
                "summaryEntry": summary_entry,
            }
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _normalize_harness_error(error, "branch_summary") from error
        finally:
            self._phase = "idle"

    def get_model(self) -> Any:
        return self._model

    async def set_model(self, model: Any) -> None:
        try:
            previous = self._model
            write = {"type": "model_change", "provider": _field(model, "provider"), "modelId": _field(model, "id")}
            if self._phase == "idle":
                await self._session.append_model_change(write["provider"], write["modelId"])
            else:
                self._pending_session_writes.append(write)
            self._model = model
            await self._emit_own({"type": "model_update", "model": model, "previousModel": previous, "source": "set"})
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _normalize_harness_error(error, "session") from error

    def get_thinking_level(self) -> str:
        return self._thinking_level

    async def set_thinking_level(self, level: str) -> None:
        try:
            previous = self._thinking_level
            if self._phase == "idle":
                await self._session.append_thinking_level_change(level)
            else:
                self._pending_session_writes.append({"type": "thinking_level_change", "thinkingLevel": level})
            self._thinking_level = level
            await self._emit_own({"type": "thinking_level_update", "level": level, "previousLevel": previous})
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _normalize_harness_error(error, "session") from error

    def get_tools(self) -> list[Any]:
        return list(self._tools.values())

    async def set_tools(self, tools: list[Any], active_tool_names: list[str] | None = None) -> None:
        try:
            names = [str(_field(tool, "name")) for tool in tools]
            self._validate_unique_names(names, "Duplicate tool name(s)")
            next_tools = dict(zip(names, tools, strict=True))
            next_active = list(active_tool_names) if active_tool_names is not None else list(self._active_tool_names)
            self._validate_tool_names(next_active, next_tools)
            previous_names = list(self._tools)
            previous_active = list(self._active_tool_names)
            if self._phase == "idle":
                await self._session.append_active_tools_change(next_active)
            else:
                self._pending_session_writes.append(
                    {"type": "active_tools_change", "activeToolNames": list(next_active)}
                )
            self._tools = next_tools
            self._active_tool_names = next_active
            await self._emit_own(
                {
                    "type": "tools_update",
                    "toolNames": list(self._tools),
                    "previousToolNames": previous_names,
                    "activeToolNames": list(self._active_tool_names),
                    "previousActiveToolNames": previous_active,
                    "source": "set",
                }
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _normalize_harness_error(error, "invalid_argument") from error

    def get_active_tools(self) -> list[Any]:
        return [self._tools[name] for name in self._active_tool_names]

    async def set_active_tools(self, tool_names: list[str]) -> None:
        try:
            self._validate_tool_names(tool_names)
            previous_names = list(self._tools)
            previous_active = list(self._active_tool_names)
            if self._phase == "idle":
                await self._session.append_active_tools_change(tool_names)
            else:
                self._pending_session_writes.append(
                    {"type": "active_tools_change", "activeToolNames": list(tool_names)}
                )
            self._active_tool_names = list(tool_names)
            await self._emit_own(
                {
                    "type": "tools_update",
                    "toolNames": list(self._tools),
                    "previousToolNames": previous_names,
                    "activeToolNames": list(self._active_tool_names),
                    "previousActiveToolNames": previous_active,
                    "source": "set",
                }
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _normalize_harness_error(error, "invalid_argument") from error

    def get_steering_mode(self) -> str:
        return self._steering_queue_mode

    async def set_steering_mode(self, mode: str) -> None:
        self._steering_queue_mode = mode

    def get_follow_up_mode(self) -> str:
        return self._follow_up_queue_mode

    async def set_follow_up_mode(self, mode: str) -> None:
        self._follow_up_queue_mode = mode

    def get_resources(self) -> dict[str, Any]:
        return {
            "skills": list(self._resources["skills"]) if self._resources.get("skills") is not None else None,
            "promptTemplates": (
                list(self._resources["promptTemplates"]) if self._resources.get("promptTemplates") is not None else None
            ),
        }

    async def set_resources(self, resources: Mapping[str, Any]) -> None:
        previous = self.get_resources()
        prompt_templates = resources.get("promptTemplates", resources.get("prompt_templates"))
        self._resources = {
            "skills": list(resources["skills"]) if resources.get("skills") is not None else None,
            "promptTemplates": list(prompt_templates) if prompt_templates is not None else None,
        }
        await self._emit_own(
            {"type": "resources_update", "resources": self.get_resources(), "previousResources": previous}
        )

    def get_stream_options(self) -> dict[str, Any]:
        return _clone_stream_options(self._stream_options)

    async def set_stream_options(self, stream_options: Mapping[str, Any]) -> None:
        self._stream_options = _clone_stream_options(stream_options)

    async def abort(self) -> dict[str, list[AgentMessage]]:
        cleared_steer, self._steer_queue = self._steer_queue, []
        cleared_follow_up, self._follow_up_queue = self._follow_up_queue, []
        if self._run_abort_controller:
            self._run_abort_controller.abort()
        errors: list[Exception] = []
        for action in (
            self._emit_queue_update,
            self.wait_for_idle,
            lambda: self._emit_own(
                {"type": "abort", "clearedSteer": cleared_steer, "clearedFollowUp": cleared_follow_up}
            ),
        ):
            try:
                await action()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                errors.append(to_error(error))
        if errors:
            cause: Exception = errors[0] if len(errors) == 1 else ExceptionGroup("Abort completed with errors", errors)
            if len(errors) > 1:
                raise AgentHarnessError("hook", "Abort completed with errors", cause) from cause
            raise _normalize_harness_error(cause, "hook") from cause
        return {"clearedSteer": cleared_steer, "clearedFollowUp": cleared_follow_up}

    async def wait_for_idle(self) -> None:
        future = self._idle_future
        if future is not None:
            await asyncio.shield(future)

    def subscribe(self, listener: Callable[..., Any]) -> Callable[[], None]:
        handlers = self._handlers.setdefault(SUBSCRIBER_EVENT_TYPE, [])
        if not any(_same_handler(current, listener) for current in handlers):
            handlers.append(listener)

        def unsubscribe() -> None:
            for index, current in enumerate(handlers):
                if _same_handler(current, listener):
                    handlers[index] = None
                    return

        return unsubscribe

    def on(self, event_type: str, handler: Callable[..., Any]) -> Callable[[], None]:
        handlers = self._handlers.setdefault(event_type, [])
        if not any(_same_handler(current, handler) for current in handlers):
            handlers.append(handler)

        def unsubscribe() -> None:
            for index, current in enumerate(handlers):
                if _same_handler(current, handler):
                    handlers[index] = None
                    return

        return unsubscribe

    promptFromTemplate = prompt_from_template
    followUp = follow_up
    nextTurn = next_turn
    appendMessage = append_message
    navigateTree = navigate_tree
    getModel = get_model
    setModel = set_model
    getThinkingLevel = get_thinking_level
    setThinkingLevel = set_thinking_level
    getTools = get_tools
    setTools = set_tools
    getActiveTools = get_active_tools
    setActiveTools = set_active_tools
    getSteeringMode = get_steering_mode
    setSteeringMode = set_steering_mode
    getFollowUpMode = get_follow_up_mode
    setFollowUpMode = set_follow_up_mode
    getResources = get_resources
    setResources = set_resources
    getStreamOptions = get_stream_options
    setStreamOptions = set_stream_options
    waitForIdle = wait_for_idle
