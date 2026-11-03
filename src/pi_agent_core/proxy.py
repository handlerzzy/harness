from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import Callable, Mapping
from typing import Any, TypeAlias, cast

import httpx

from ._utils import get_any_field, js_json_stringify, js_parse_json_number, now_ms
from .abort import AbortSignal
from .event_stream import EventStream

ProxyAssistantMessageEvent: TypeAlias = dict[str, Any]
ProxyStreamOptions: TypeAlias = Mapping[str, Any]


class ProxyMessageEventStream(EventStream[dict[str, Any], dict[str, Any]]):
    def __init__(self) -> None:
        super().__init__(
            lambda event: event.get("type") in {"done", "error"},
            lambda event: cast(dict[str, Any], event["message"] if event.get("type") == "done" else event["error"]),
        )


def _field(value: Any, snake: str, camel: str | None = None, default: Any = None) -> Any:
    return get_any_field(value, snake, *(tuple([camel]) if camel else ()), default=default)


def build_proxy_request_options(options: Mapping[str, Any]) -> dict[str, Any]:
    aliases = {
        "temperature": "temperature",
        "maxTokens": "max_tokens",
        "reasoning": "reasoning",
        "cacheRetention": "cache_retention",
        "sessionId": "session_id",
        "headers": "headers",
        "metadata": "metadata",
        "transport": "transport",
        "thinkingBudgets": "thinking_budgets",
        "maxRetryDelayMs": "max_retry_delay_ms",
    }
    result: dict[str, Any] = {}
    for camel, snake in aliases.items():
        value = options.get(camel, options.get(snake))
        if value is not None:
            result[camel] = value
    return result


_VALID_JSON_ESCAPES = frozenset({'"', "\\", "/", "b", "f", "n", "r", "t", "u"})


def _escape_control_character(character: str) -> str:
    return {
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }.get(character, f"\\u{ord(character):04x}")


def repair_json(value: str) -> str:
    """Port of ``pi-ai``'s ``repairJson`` string-literal repair pass."""

    repaired: list[str] = []
    in_string = False
    index = 0
    while index < len(value):
        character = value[index]
        if not in_string:
            repaired.append(character)
            if character == '"':
                in_string = True
            index += 1
            continue

        if character == '"':
            repaired.append(character)
            in_string = False
            index += 1
            continue

        if character == "\\":
            next_character = value[index + 1] if index + 1 < len(value) else None
            if next_character is None:
                repaired.append("\\\\")
                index += 1
                continue
            if next_character == "u":
                unicode_digits = value[index + 2 : index + 6]
                if len(unicode_digits) == 4 and all(char in "0123456789abcdefABCDEF" for char in unicode_digits):
                    repaired.append(f"\\u{unicode_digits}")
                    index += 6
                    continue
            if next_character in _VALID_JSON_ESCAPES:
                repaired.append(f"\\{next_character}")
                index += 2
                continue
            repaired.append("\\\\")
            index += 1
            continue

        repaired.append(_escape_control_character(character) if ord(character) <= 0x1F else character)
        index += 1
    return "".join(repaired)


def _strict_json_loads(value: str) -> Any:
    def reject_constant(constant: str) -> Any:
        raise ValueError(f"Invalid JSON constant: {constant}")

    return json.loads(
        value,
        parse_int=js_parse_json_number,
        parse_float=js_parse_json_number,
        parse_constant=reject_constant,
    )


class _PartialJsonParser:
    """Behavioral port of ``partial-json@0.1.7`` with ``Allow.ALL``."""

    def __init__(self, value: str) -> None:
        self.value = value.strip()
        self.length = len(self.value)
        self.index = 0

    def parse(self) -> Any:
        if not self.value:
            raise ValueError("JSON is empty")
        return self._parse_any()

    def _skip_blank(self) -> None:
        while self.index < self.length and self.value[self.index] in " \n\r\t":
            self.index += 1

    def _parse_any(self) -> Any:
        self._skip_blank()
        if self.index >= self.length:
            raise ValueError("Unexpected end of input")
        character = self.value[self.index]
        if character == '"':
            return self._parse_string()
        if character == "{":
            return self._parse_object()
        if character == "[":
            return self._parse_array()

        remaining = self.value[self.index :]
        specials: tuple[tuple[str, Any, int], ...] = (
            ("null", None, 4),
            ("true", True, 4),
            ("false", False, 5),
            ("Infinity", float("inf"), 8),
            ("-Infinity", float("-inf"), 9),
            ("NaN", float("nan"), 3),
        )
        for spelling, parsed, width in specials:
            is_allowed_prefix = len(remaining) < width and spelling.startswith(remaining)
            if remaining.startswith(spelling) or is_allowed_prefix:
                # The JavaScript dependency advances by the full literal width,
                # even when only a partial prefix was available.
                self.index += width
                return parsed
        return self._parse_number()

    def _parse_string(self) -> str:
        start = self.index
        escaped = False
        self.index += 1
        while self.index < self.length and (
            self.value[self.index] != '"' or (escaped and self.value[self.index - 1] == "\\")
        ):
            escaped = not escaped if self.value[self.index] == "\\" else False
            self.index += 1

        if self.index < self.length and self.value[self.index] == '"':
            self.index += 1
            end = self.index - int(escaped)
            return cast(str, _strict_json_loads(self.value[start:end]))

        candidate = self.value[start : self.index - int(escaped)] + '"'
        try:
            return cast(str, _strict_json_loads(candidate))
        except (json.JSONDecodeError, ValueError):
            last_backslash = self.value.rfind("\\")
            if last_backslash < start:
                raise
            return cast(str, _strict_json_loads(self.value[start:last_backslash] + '"'))

    def _parse_object(self) -> dict[str, Any]:
        self.index += 1
        self._skip_blank()
        result: dict[str, Any] = {}
        try:
            while self.index >= self.length or self.value[self.index] != "}":
                self._skip_blank()
                if self.index >= self.length:
                    return result
                key = self._parse_string()
                self._skip_blank()
                self.index += 1  # partial-json intentionally skips, rather than validates, ':'
                try:
                    parsed = self._parse_any()
                    result[key] = parsed
                except (json.JSONDecodeError, TypeError, ValueError):
                    return result
                self._skip_blank()
                if self.index < self.length and self.value[self.index] == ",":
                    self.index += 1
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            return result
        self.index += 1
        return result

    def _parse_array(self) -> list[Any]:
        self.index += 1
        result: list[Any] = []
        try:
            while self.index >= self.length or self.value[self.index] != "]":
                result.append(self._parse_any())
                self._skip_blank()
                if self.index < self.length and self.value[self.index] == ",":
                    self.index += 1
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            return result
        self.index += 1
        return result

    def _parse_number(self) -> int | float:
        if self.index == 0:
            if self.value == "-":
                raise ValueError("Not sure what '-' is")
            try:
                return cast(int | float, _strict_json_loads(self.value))
            except (json.JSONDecodeError, ValueError) as original_error:
                last_exponent = self.value.rfind("e")
                try:
                    # JS substring(0, -1) is the empty string.
                    prefix = self.value[: max(last_exponent, 0)]
                    return cast(int | float, _strict_json_loads(prefix))
                except (json.JSONDecodeError, ValueError):
                    raise ValueError(str(original_error)) from original_error

        start = self.index
        if self.index < self.length and self.value[self.index] == "-":
            self.index += 1
        while self.index < self.length and self.value[self.index] not in ",]}":
            self.index += 1
        candidate = self.value[start : self.index]
        try:
            return cast(int | float, _strict_json_loads(candidate))
        except (json.JSONDecodeError, ValueError) as original_error:
            if candidate == "-":
                raise ValueError("Not sure what '-' is") from original_error
            last_exponent = self.value.rfind("e")
            try:
                # JavaScript String.substring swaps its arguments when end < start.
                if last_exponent < start:
                    prefix = self.value[max(last_exponent, 0) : start]
                else:
                    prefix = self.value[start:last_exponent]
                return cast(int | float, _strict_json_loads(prefix))
            except (json.JSONDecodeError, ValueError):
                raise ValueError(str(original_error)) from original_error


def parse_streaming_json(value: str | None) -> Any:
    if not value or not value.strip():
        return {}
    try:
        return _strict_json_loads(value)
    except (json.JSONDecodeError, ValueError):
        try:
            repaired = repair_json(value)
            if repaired != value:
                return _strict_json_loads(repaired)
        except (json.JSONDecodeError, ValueError):
            pass
        for candidate in (value, repair_json(value)):
            try:
                parsed = _PartialJsonParser(candidate).parse()
                return {} if parsed is None else parsed
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return {}


def _ensure_content_index(partial: dict[str, Any], index: int) -> None:
    while len(partial["content"]) <= index:
        partial["content"].append(None)


def process_proxy_event(proxy_event: Mapping[str, Any], partial: dict[str, Any]) -> dict[str, Any] | None:
    event_type = proxy_event.get("type")
    if event_type == "start":
        return {"type": "start", "partial": partial}
    if event_type in {"text_start", "thinking_start", "toolcall_start"}:
        index = int(proxy_event["contentIndex"])
        _ensure_content_index(partial, index)
        if event_type == "text_start":
            partial["content"][index] = {"type": "text", "text": ""}
        elif event_type == "thinking_start":
            partial["content"][index] = {"type": "thinking", "thinking": ""}
        else:
            partial["content"][index] = {
                "type": "toolCall",
                "id": proxy_event["id"],
                "name": proxy_event["toolName"],
                "arguments": {},
                "partialJson": "",
            }
        return {"type": event_type, "contentIndex": index, "partial": partial}
    if event_type in {"text_delta", "thinking_delta", "toolcall_delta"}:
        index = int(proxy_event["contentIndex"])
        content = partial["content"][index] if index < len(partial["content"]) else None
        expected = {"text_delta": "text", "thinking_delta": "thinking", "toolcall_delta": "toolCall"}[event_type]
        if not isinstance(content, dict) or content.get("type") != expected:
            raise RuntimeError(f"Received {event_type} for non-{expected} content")
        delta = str(proxy_event.get("delta", ""))
        if event_type == "text_delta":
            content["text"] += delta
        elif event_type == "thinking_delta":
            content["thinking"] += delta
        else:
            content["partialJson"] += delta
            content["arguments"] = parse_streaming_json(content["partialJson"]) or {}
            partial["content"][index] = dict(content)
        return {"type": event_type, "contentIndex": index, "delta": delta, "partial": partial}
    if event_type in {"text_end", "thinking_end", "toolcall_end"}:
        index = int(proxy_event["contentIndex"])
        content = partial["content"][index] if index < len(partial["content"]) else None
        if event_type == "text_end":
            if not isinstance(content, dict) or content.get("type") != "text":
                raise RuntimeError("Received text_end for non-text content")
            if proxy_event.get("contentSignature") is not None:
                content["textSignature"] = proxy_event["contentSignature"]
            return {"type": "text_end", "contentIndex": index, "content": content["text"], "partial": partial}
        if event_type == "thinking_end":
            if not isinstance(content, dict) or content.get("type") != "thinking":
                raise RuntimeError("Received thinking_end for non-thinking content")
            if proxy_event.get("contentSignature") is not None:
                content["thinkingSignature"] = proxy_event["contentSignature"]
            return {
                "type": "thinking_end",
                "contentIndex": index,
                "content": content["thinking"],
                "partial": partial,
            }
        if isinstance(content, dict) and content.get("type") == "toolCall":
            content.pop("partialJson", None)
            return {
                "type": "toolcall_end",
                "contentIndex": index,
                "toolCall": content,
                "partial": partial,
            }
        return None
    if event_type == "done":
        partial["stopReason"] = proxy_event["reason"]
        partial["usage"] = proxy_event["usage"]
        return {"type": "done", "reason": proxy_event["reason"], "message": partial}
    if event_type == "error":
        partial["stopReason"] = proxy_event["reason"]
        if proxy_event.get("errorMessage") is not None:
            partial["errorMessage"] = proxy_event["errorMessage"]
        partial["usage"] = proxy_event["usage"]
        return {"type": "error", "reason": proxy_event["reason"], "error": partial}
    return None


def _empty_usage() -> dict[str, Any]:
    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def stream_proxy(model: Any, context: Mapping[str, Any], options: Mapping[str, Any]) -> ProxyMessageEventStream:
    stream = ProxyMessageEventStream()
    partial: dict[str, Any] = {
        "role": "assistant",
        "stopReason": "stop",
        "content": [],
        "api": _field(model, "api", default=""),
        "provider": _field(model, "provider", default=""),
        "model": _field(model, "id", default=""),
        "usage": _empty_usage(),
        "timestamp": now_ms(),
    }
    signal = cast(AbortSignal | None, options.get("signal"))

    async def request(client: httpx.AsyncClient) -> dict[str, Any]:
        proxy_url = str(options.get("proxyUrl", options.get("proxy_url", "")))
        auth_token = str(options.get("authToken", options.get("auth_token", "")))
        request_body = js_json_stringify(
            {"model": model, "context": dict(context), "options": build_proxy_request_options(options)}
        )
        if request_body is None:
            raise TypeError("Proxy request is not JSON serializable")
        async with client.stream(
            "POST",
            f"{proxy_url}/api/stream",
            headers={"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"},
            content=request_body.encode("utf-8", errors="surrogatepass"),
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                message = f"Proxy error: {response.status_code} {response.reason_phrase}"
                try:
                    parsed = json.loads(await response.aread())
                    if isinstance(parsed, dict) and parsed.get("error"):
                        message = f"Proxy error: {parsed['error']}"
                except (json.JSONDecodeError, ValueError):
                    pass
                raise RuntimeError(message)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            buffer = ""
            async for chunk in response.aiter_bytes():
                if signal and signal.aborted:
                    raise asyncio.CancelledError("Request aborted by user")
                buffer += decoder.decode(chunk, final=False)
                lines = buffer.split("\n")
                buffer = lines.pop() or ""
                for line in lines:
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data:
                            event = process_proxy_event(_strict_json_loads(data), partial)
                            if event is not None:
                                if event["type"] in {"done", "error"}:
                                    return event
                                stream.push(event)
        raise RuntimeError("Proxy stream ended without a terminal event")

    async def run() -> object:
        remove_abort: Callable[[], None] | None = None
        try:
            if signal is not None:
                signal.throw_if_aborted()
                task = asyncio.current_task()
                assert task is not None
                remove_abort = signal.add_callback(task.cancel)
            provided_client = options.get("httpClient", options.get("http_client"))
            if provided_client is None:
                async with httpx.AsyncClient() as client:
                    terminal = await request(client)
            else:
                terminal = await request(provided_client)
            if signal is not None:
                signal.throw_if_aborted()
            stream.push(terminal)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            reason = "aborted" if signal and signal.aborted else "error"
            partial["stopReason"] = reason
            partial["errorMessage"] = "Request aborted by user" if reason == "aborted" else str(error)
            stream.push({"type": "error", "reason": reason, "error": partial})
        finally:
            if remove_abort is not None:
                remove_abort()
            stream.end()
        return None

    task = asyncio.create_task(run())
    stream.attach_task(task)
    return stream


buildProxyRequestOptions = build_proxy_request_options
repairJson = repair_json
parseStreamingJson = parse_streaming_json
processProxyEvent = process_proxy_event
streamProxy = stream_proxy
