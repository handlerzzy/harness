from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import httpx
import pytest

from pi_agent_core import AbortController, stream_proxy
from pi_agent_core.proxy import parse_streaming_json, process_proxy_event, repair_json

from .test_agent_loop import model, usage


def partial() -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [],
        "stopReason": "stop",
        "api": "mock-api",
        "provider": "mock",
        "model": "mock",
        "usage": usage(),
        "timestamp": 1,
    }


def test_process_proxy_events_reconstructs_all_content_types() -> None:
    message = partial()
    events = [
        {"type": "start"},
        {"type": "thinking_start", "contentIndex": 0},
        {"type": "thinking_delta", "contentIndex": 0, "delta": "plan"},
        {"type": "thinking_end", "contentIndex": 0, "contentSignature": "sig-a"},
        {"type": "text_start", "contentIndex": 1},
        {"type": "text_delta", "contentIndex": 1, "delta": "hello"},
        {"type": "text_end", "contentIndex": 1, "contentSignature": "sig-b"},
        {"type": "toolcall_start", "contentIndex": 2, "id": "t", "toolName": "read"},
        {"type": "toolcall_delta", "contentIndex": 2, "delta": '{"path":"a'},
        {"type": "toolcall_delta", "contentIndex": 2, "delta": '.py"}'},
        {"type": "toolcall_end", "contentIndex": 2},
        {"type": "done", "reason": "toolUse", "usage": usage()},
    ]
    processed = [process_proxy_event(event, message) for event in events]
    assert message["content"] == [
        {"type": "thinking", "thinking": "plan", "thinkingSignature": "sig-a"},
        {"type": "text", "text": "hello", "textSignature": "sig-b"},
        {"type": "toolCall", "id": "t", "name": "read", "arguments": {"path": "a.py"}},
    ]
    assert processed[-1] == {"type": "done", "reason": "toolUse", "message": message}
    assert parse_streaming_json('{"path":"half') == {"path": "half"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a":"line\nnext"}', {"a": "line\nnext"}),
        ('{"a":"bad\\q"}', {"a": "bad\\q"}),
        ('{"a":"trail\\', {"a": "trail"}),
        ('{"a":"bad\\u12', {"a": "bad"}),
        ('{"a":[1,{"b":"x', {"a": [1, {"b": "x"}]}),
        ('[{"a":1},2,', [{"a": 1}, 2]),
        ('{"a":1,"b":', {"a": 1}),
        ('{"a":nu', {"a": None}),
        ('{"a":tr', {"a": True}),
        ('{"a":fa', {"a": False}),
        ('{"a":1e', {"a": 1}),
        ('{"a":1.', {}),
        ("12e", 12),
        ('{"a":"ok"}garbage', {"a": "ok"}),
    ],
)
def test_streaming_json_matches_pi_ai_partial_json_0_1_7(raw: str, expected: object) -> None:
    assert parse_streaming_json(raw) == expected


def test_repair_json_matches_pi_ai_control_and_escape_rules() -> None:
    assert repair_json('"a\x00b\n"') == '"a\\u0000b\\n"'
    assert repair_json('"bad\\q"') == '"bad\\\\q"'
    assert repair_json('"valid\\u1234"') == '"valid\\u1234"'
    parsed = parse_streaming_json('{"large":9007199254740993,"negativeZero":-0}')
    assert parsed["large"] == 9007199254740992
    assert math.copysign(1, parsed["negativeZero"]) == -1


def test_proxy_omits_undefined_optional_signature_and_error_fields() -> None:
    message = partial()
    process_proxy_event({"type": "text_start", "contentIndex": 0}, message)
    process_proxy_event({"type": "text_end", "contentIndex": 0}, message)
    process_proxy_event({"type": "error", "reason": "error", "usage": usage()}, message)

    assert "textSignature" not in message["content"][0]
    assert "errorMessage" not in message


@pytest.mark.asyncio
async def test_stream_proxy_posts_sanitized_request_and_reads_sse() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content)
        lines = [
            {"type": "start"},
            {"type": "text_start", "contentIndex": 0},
            {"type": "text_delta", "contentIndex": 0, "delta": "hello"},
            {"type": "text_end", "contentIndex": 0},
            {"type": "done", "reason": "stop", "usage": usage()},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in lines)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream = stream_proxy(
        model(),
        {"systemPrompt": "help", "messages": [], "tools": []},
        {
            "proxyUrl": "https://proxy.example",
            "authToken": "secret",
            "temperature": 0.2,
            "maxTokens": 123,
            "signal": AbortController().signal,
            "httpClient": client,
        },
    )
    events = [event async for event in stream]
    result = await stream.result()
    await client.aclose()

    assert result["content"] == [{"type": "text", "text": "hello"}]
    assert [event["type"] for event in events] == ["start", "text_start", "text_delta", "text_end", "done"]
    assert captured["authorization"] == "Bearer secret"
    assert captured["body"]["options"] == {"temperature": 0.2, "maxTokens": 123}
    assert "authToken" not in captured["body"]["options"]


@pytest.mark.asyncio
async def test_stream_proxy_maps_http_and_abort_errors() -> None:
    error_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, json={"error": "bad token"}))
    )
    failed = stream_proxy(
        model(),
        {"messages": []},
        {"proxyUrl": "https://proxy.example", "authToken": "x", "httpClient": error_client},
    )
    failed_result = await failed.result()
    await error_client.aclose()
    assert failed_result["stopReason"] == "error"
    assert failed_result["errorMessage"] == "Proxy error: bad token"

    controller = AbortController()
    controller.abort()
    aborted_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    aborted = stream_proxy(
        model(),
        {"messages": []},
        {
            "proxyUrl": "https://proxy.example",
            "authToken": "x",
            "signal": controller.signal,
            "httpClient": aborted_client,
        },
    )
    aborted_result = await aborted.result()
    await aborted_client.aclose()
    assert aborted_result["stopReason"] == "aborted"
    assert aborted_result["errorMessage"] == "Request aborted by user"


@pytest.mark.asyncio
async def test_stream_proxy_immediate_abort_always_settles() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200)

    controller = AbortController()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stream = stream_proxy(
        model(),
        {"systemPrompt": "", "messages": []},
        {
            "proxyUrl": "https://proxy.example",
            "authToken": "token",
            "signal": controller.signal,
            "httpClient": client,
        },
    )
    controller.abort()

    result = await asyncio.wait_for(stream.result(), 1)
    await client.aclose()
    assert result["stopReason"] == "aborted"


@pytest.mark.asyncio
async def test_stream_proxy_matches_fetch_chunk_decoding_and_ignores_unterminated_sse_line() -> None:
    class ChunkedBody(httpx.AsyncByteStream):
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        async def __aiter__(self):
            for chunk in self.chunks:
                yield chunk

    text_event = json.dumps(
        {"type": "text_delta", "contentIndex": 0, "delta": "🙂"},
        ensure_ascii=False,
    )
    terminated = (
        f'data: {{"type":"start"}}\ndata: {{"type":"text_start","contentIndex":0}}\ndata: {text_event}\n'
    ).encode()
    emoji_start = terminated.index("🙂".encode()) + 1
    unterminated_done = f"data: {json.dumps({'type': 'done', 'reason': 'stop', 'usage': usage()})}".encode()
    chunks = [terminated[:emoji_start], terminated[emoji_start:], unterminated_done]
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                stream=ChunkedBody(chunks),
                headers={"content-type": "text/event-stream"},
            )
        )
    )
    stream = stream_proxy(
        model(),
        {"messages": []},
        {"proxyUrl": "https://proxy.example", "authToken": "x", "httpClient": client},
    )
    events = [event async for event in stream]
    await client.aclose()

    assert [event["type"] for event in events] == ["start", "text_start", "text_delta"]
    assert events[-1]["delta"] == "🙂"
