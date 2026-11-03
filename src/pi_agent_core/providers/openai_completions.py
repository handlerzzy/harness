from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx

from .._utils import maybe_await, now_ms
from ..event_stream import AssistantMessageEventStream
from ..types import Model


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if any(block.get("type") not in {"text", "thinking"} for block in content):
        raise ValueError("Chat Completions currently supports only text history")
    return "".join(block["text"] for block in content if block["type"] == "text")


def _payload(model: Model, context: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    messages = []
    if context.get("systemPrompt"):
        messages.append({"role": "system", "content": context["systemPrompt"]})
    for message in context.get("messages", []):
        if message["role"] not in {"user", "assistant"}:
            raise ValueError(f"Unsupported Chat Completions history role: {message['role']}")
        converted = {"role": message["role"], "content": _text(message["content"])}
        if message["role"] == "assistant":
            thinking = [
                block for block in message["content"] if isinstance(block, dict) and block.get("type") == "thinking"
            ]
            if thinking:
                converted["reasoning_content"] = "".join(block["thinking"] for block in thinking)
        messages.append(converted)
    if context.get("tools"):
        raise ValueError("Chat Completions tool calling is not implemented yet")
    payload: dict[str, Any] = {
        "model": model["id"],
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": options.get("maxTokens") or model["maxTokens"],
    }
    if options.get("temperature") is not None:
        payload["temperature"] = options["temperature"]
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

    def emit(event_type: str, **fields: Any) -> None:
        stream.push({"type": event_type, "partial": copy.deepcopy(message), **fields})

    async def request(client: httpx.AsyncClient) -> None:
        payload = _payload(model, context, options)
        if callback := options.get("onPayload"):
            replacement = await maybe_await(callback(payload))
            if replacement is not None:
                payload = replacement
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"}
        headers.update(options.get("headers") or {})
        async with client.stream(
            "POST",
            model["baseUrl"].rstrip("/") + "/chat/completions",
            json=payload,
            headers=headers,
            timeout=options.get("timeoutMs", 60_000) / 1000,
        ) as response:
            if callback := options.get("onResponse"):
                await maybe_await(callback({"status": response.status_code, "headers": dict(response.headers)}))
            if not response.is_success:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise ValueError(f"Chat Completions HTTP {response.status_code}: {body}")
            emit("start")
            reason = None
            block_indexes: dict[str, int] = {}
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
                if delta.get("tool_calls"):
                    raise ValueError("Chat Completions tool calling is not implemented yet")
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
                if choice.get("finish_reason") is not None:
                    reason = choice["finish_reason"]
            if reason not in {"stop", "length"}:
                raise ValueError(f"Stream ended without a supported finish_reason: {reason!r}")
            message["stopReason"] = reason
            for index, block in enumerate(message["content"]):
                kind = block["type"]
                emit(f"{kind}_end", contentIndex=index, content=block[kind])

    async def run() -> object:
        signal = options.get("signal")
        remove_abort: Callable[[], None] | None = None
        try:
            if signal is not None:
                signal.throw_if_aborted()
                task = asyncio.current_task()
                assert task is not None
                remove_abort = signal.add_callback(task.cancel)
            async with asyncio.timeout(options.get("timeoutMs", 60_000) / 1000):
                if http_client is None:
                    async with httpx.AsyncClient() as client:
                        await request(client)
                else:
                    await request(http_client)
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
