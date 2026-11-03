from __future__ import annotations

import asyncio
import copy
import json
import math
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx

from .._utils import get_field, maybe_await, now_ms
from ..event_stream import AssistantMessageEventStream
from ..types import Model

_TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}


def _text(content: Any, *, assistant: bool = False, separator: str = "") -> str:
    if isinstance(content, str):
        return content
    allowed = {"text", "thinking", "toolCall"} if assistant else {"text", "thinking"}
    if any(block.get("type") not in allowed for block in content):
        raise ValueError("Chat Completions currently supports only text history")
    return separator.join(block["text"] for block in content if block["type"] == "text")


def _payload(model: Model, context: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    messages = []
    if context.get("systemPrompt"):
        messages.append({"role": "system", "content": context["systemPrompt"]})
    for message in context.get("messages", []):
        if message["role"] == "assistant" and message.get("stopReason") in {"error", "aborted"}:
            continue
        if message["role"] == "toolResult":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": message["toolCallId"],
                    "content": _text(message["content"], separator="\n"),
                }
            )
            continue
        if message["role"] not in {"user", "assistant"}:
            raise ValueError(f"Unsupported Chat Completions history role: {message['role']}")
        converted = {
            "role": message["role"],
            "content": _text(message["content"], assistant=message["role"] == "assistant"),
        }
        if message["role"] == "assistant":
            thinking = [
                block for block in message["content"] if isinstance(block, dict) and block.get("type") == "thinking"
            ]
            if thinking:
                converted["reasoning_content"] = "".join(block["thinking"] for block in thinking)
            calls = [
                block for block in message["content"] if isinstance(block, dict) and block.get("type") == "toolCall"
            ]
            if calls:
                converted["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                        },
                    }
                    for call in calls
                ]
        messages.append(converted)
    payload: dict[str, Any] = {
        "model": model["id"],
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": options.get("maxTokens") or model["maxTokens"],
    }
    if options.get("temperature") is not None:
        payload["temperature"] = options["temperature"]
    if context.get("tools"):
        payload["tools"] = [
            {
                "type": "function",
                "function": {key: get_field(tool, key) for key in ("name", "description", "parameters")},
            }
            for tool in context["tools"]
        ]
    return payload


def _usage(raw: Mapping[str, Any]) -> dict[str, Any]:
    usage = {
        target: raw[source]
        for source, target in (
            ("prompt_tokens", "input"),
            ("completion_tokens", "output"),
            ("total_tokens", "totalTokens"),
        )
        if raw.get(source) is not None
    }
    cached = (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is not None:
        usage["cacheRead"] = cached
        if "input" in usage:
            usage["input"] -= cached
    return usage


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Invalid JSON constant: {value}")


async def _sse(response: httpx.Response) -> AsyncIterator[str]:
    data: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield "\n".join(data)
                data.clear()
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)


def stream_chat_completions(
    model: Model,
    context: Mapping[str, Any],
    options: Mapping[str, Any],
    *,
    api_key: str,
    http_client: httpx.AsyncClient | None = None,
) -> AssistantMessageEventStream:
    """Translate a Chat Completions SSE response to shared model events."""
    stream = AssistantMessageEventStream()
    message: dict[str, Any] = {
        "role": "assistant",
        "content": [],
        "api": model.get("api", "openai-completions"),
        "provider": model.get("provider", "openai-compatible"),
        "model": model["id"],
        "stopReason": "stop",
        "timestamp": now_ms(),
    }
    published = False
    retryable_response = True

    def emit(event_type: str, **fields: Any) -> None:
        nonlocal published
        published = True
        stream.push({"type": event_type, "partial": copy.deepcopy(message), **fields})

    async def request(client: httpx.AsyncClient, payload: Any) -> None:
        nonlocal retryable_response
        if signal := options.get("signal"):
            signal.throw_if_aborted()
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"}
        headers.update(options.get("headers") or {})
        async with client.stream(
            "POST",
            model["baseUrl"].rstrip("/") + "/chat/completions",
            json=payload,
            headers=headers,
            timeout=options.get("timeoutMs", 60_000) / 1000,
        ) as response:
            # A known permanent status stays permanent even if reading its
            # error body subsequently times out or disconnects.
            retryable_response = response.is_success or response.status_code in _TRANSIENT_HTTP_STATUSES
            if callback := options.get("onResponse"):
                await maybe_await(callback({"status": response.status_code, "headers": dict(response.headers)}))
            if not response.is_success:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise httpx.HTTPStatusError(
                    f"Chat Completions HTTP {response.status_code}: {body}",
                    request=response.request,
                    response=response,
                )
            emit("start")
            reason = None
            block_indexes: dict[str, int] = {}
            tool_indexes: dict[int, int] = {}
            tool_ids: dict[str, int] = {}
            tool_arguments: dict[int, str] = {}
            async for data in _sse(response):
                if data == "[DONE]":
                    break
                raw = json.loads(data)
                if raw.get("error"):
                    raise ValueError(f"Chat Completions error: {raw['error']}")
                if raw.get("id"):
                    message.setdefault("responseId", raw["id"])
                if raw.get("model") and raw["model"] != model["id"]:
                    message.setdefault("responseModel", raw["model"])
                if raw.get("usage") is not None:
                    message["usage"] = _usage(raw["usage"])
                choices = raw.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                for kind, source in (("thinking", "reasoning_content"), ("text", "content")):
                    value = delta.get(source)
                    if not value:
                        continue
                    if kind not in block_indexes:
                        block_indexes[kind] = len(message["content"])
                        block = {"type": kind, kind: ""}
                        if kind == "thinking":
                            block["thinkingSignature"] = source
                        message["content"].append(block)
                        emit(f"{kind}_start", contentIndex=block_indexes[kind])
                    index = block_indexes[kind]
                    message["content"][index][kind] += value
                    emit(f"{kind}_delta", contentIndex=index, delta=value)
                for call in delta.get("tool_calls") or []:
                    stream_index = call.get("index")
                    call_id = call.get("id")
                    if stream_index is not None and (type(stream_index) is not int or stream_index < 0):
                        raise ValueError("Tool call index must be a non-negative integer")
                    if call_id is not None and (not isinstance(call_id, str) or not call_id):
                        raise ValueError("Tool call ID must be a non-empty string")
                    function = call.get("function") or {}
                    tool_index = tool_indexes.get(stream_index) if stream_index is not None else None
                    if tool_index is None and call_id:
                        tool_index = tool_ids.get(call_id)
                    if tool_index is None:
                        if stream_index is None and not call_id:
                            raise ValueError("Tool call delta has neither an index nor an ID")
                        tool_index = len(message["content"])
                        message["content"].append(
                            {
                                "type": "toolCall",
                                "id": "",
                                "name": "",
                                "arguments": {},
                            }
                        )
                        tool_arguments[tool_index] = ""
                        emit("toolcall_start", contentIndex=tool_index)
                    block = message["content"][tool_index]
                    if stream_index is not None:
                        if tool_index in tool_indexes.values() and tool_indexes.get(stream_index) != tool_index:
                            raise ValueError("Conflicting tool call indexes for the same ID")
                        tool_indexes[stream_index] = tool_index
                    if call_id:
                        if (block["id"] and block["id"] != call_id) or tool_ids.get(call_id, tool_index) != tool_index:
                            raise ValueError("Conflicting tool call IDs in response")
                        block["id"] = call_id
                        tool_ids[call_id] = tool_index
                    block["name"] += function.get("name") or ""
                    value = function.get("arguments") or ""
                    tool_arguments[tool_index] += value
                    emit("toolcall_delta", contentIndex=tool_index, delta=value)
                if choice.get("finish_reason") is not None:
                    reason = choice["finish_reason"]
            if reason not in {"stop", "length", "tool_calls"}:
                raise ValueError(f"Stream ended without a supported finish_reason: {reason!r}")
            if tool_arguments and reason == "stop":
                raise ValueError("Tool calls require finish_reason 'tool_calls' to execute")
            if reason == "tool_calls" and not tool_arguments:
                raise ValueError("finish_reason 'tool_calls' received with no tool calls")
            message["stopReason"] = "toolUse" if reason == "tool_calls" else reason
            for index, arguments in tool_arguments.items():
                block = message["content"][index]
                if not block["id"]:
                    raise ValueError("Tool call is missing its ID")
                if not block["name"]:
                    raise ValueError(f"Tool call {block['id']!r} is missing its name")
                try:
                    block["arguments"] = json.loads(arguments, parse_constant=_reject_json_constant)
                except ValueError as error:
                    if reason != "length":
                        raise ValueError(f"Invalid JSON arguments for tool call {block['id']!r}: {error}") from error
                    # Keep unparseable previews empty. The loop rejects the entire
                    # length-truncated batch and feeds the failure back to the model.
            for index, block in enumerate(message["content"]):
                kind = block["type"]
                if kind == "toolCall":
                    emit("toolcall_end", contentIndex=index, toolCall=copy.deepcopy(block))
                else:
                    emit(f"{kind}_end", contentIndex=index, content=block[kind])

    async def request_with_retries(client: httpx.AsyncClient, payload: Any) -> None:
        retries = options.get("maxRetries", 2)
        delay_cap = options.get("maxRetryDelayMs", 1000)
        timeout = options.get("timeoutMs", 60_000)
        if type(retries) is not int or retries < 0:
            raise ValueError("maxRetries must be a non-negative integer")
        if not isinstance(delay_cap, (int, float)) or not math.isfinite(delay_cap) or delay_cap < 0:
            raise ValueError("maxRetryDelayMs must be finite and non-negative")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeoutMs must be finite and positive")
        delay = min(100, delay_cap)
        for attempt in range(retries + 1):
            try:
                async with asyncio.timeout(timeout / 1000):
                    await request(client, payload)
                return
            except (httpx.HTTPStatusError, httpx.TransportError, TimeoutError) as error:
                transient = (
                    error.response.status_code in _TRANSIENT_HTTP_STATUSES
                    if isinstance(error, httpx.HTTPStatusError)
                    else isinstance(
                        error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, TimeoutError)
                    )
                )
                if not transient or not retryable_response or published or attempt == retries:
                    raise
                # Only the failed HTTP request is repeated. No model events or
                # tool execution can be replayed across this boundary.
                await asyncio.sleep(delay / 1000)
                delay = min(delay * 2, delay_cap)

    async def run() -> object:
        signal = options.get("signal")
        remove_abort: Callable[[], None] | None = None
        try:
            if signal is not None:
                signal.throw_if_aborted()
                task = asyncio.current_task()
                assert task is not None
                remove_abort = signal.add_callback(task.cancel)
            payload = _payload(model, context, options)
            if callback := options.get("onPayload"):
                replacement = await maybe_await(callback(payload))
                if replacement is not None:
                    payload = replacement
            if http_client is None:
                async with httpx.AsyncClient() as client:
                    await request_with_retries(client, payload)
            else:
                await request_with_retries(http_client, payload)
            if signal is not None:
                signal.throw_if_aborted()
            stream.push({"type": "done", "reason": message["stopReason"], "message": message})
        except (Exception, asyncio.CancelledError) as error:
            reason = "aborted" if isinstance(error, asyncio.CancelledError) or (signal and signal.aborted) else "error"
            message["stopReason"] = reason
            message["errorMessage"] = str(error) or type(error).__name__
            stream.push({"type": "error", "reason": reason, "error": message})
        finally:
            if remove_abort is not None:
                remove_abort()
            stream.end()
        return None

    stream.attach_task(asyncio.create_task(run()))
    return stream
