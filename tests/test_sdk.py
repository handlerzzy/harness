from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pi_agent_core import create_harness


def sse(*chunks: dict[str, Any], done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks)
    return (body + ("data: [DONE]\n\n" if done else "")).encode()


def chunk(text: str = "", finish: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}], **extra}


def sdk(tmp_path: Path, client: httpx.AsyncClient, **options: Any):
    return create_harness(
        api_key="test-secret",
        base_url="https://model.example/v1/",
        model_id="test-model",
        context_window=8192,
        max_tokens=123,
        cwd=tmp_path,
        system_prompt="Be helpful.",
        http_client=client,
        **options,
    )


async def test_sdk_streams_text_and_sends_session_history(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = sse(chunk("Hello "), chunk("世界🙂"), chunk(finish="stop"))
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, temperature=0.2)
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda event: events.append(copy.deepcopy(event)))
        first = await asyncio.wait_for(harness.prompt("hello"), 1)
        second = await asyncio.wait_for(harness.prompt("again"), 1)

    assert first["content"] == second["content"] == [{"type": "text", "text": "Hello 世界🙂"}]
    assert first["stopReason"] == "stop"
    assert "usage" not in first
    assert "cost" not in first
    assert harness.env.cwd == str(tmp_path)
    assert harness.get_model()["contextWindow"] == 8192
    assert str(requests[0].url) == "https://model.example/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-secret"
    payload = json.loads(requests[1].content)
    assert payload == {
        "model": "test-model",
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": 123,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hello 世界🙂"},
            {"role": "user", "content": "again"},
        ],
    }
    updates = [e for e in events if e["type"] == "message_update"]
    deltas = [e for e in updates if e["assistantMessageEvent"]["type"] == "text_delta"]
    assert "".join(e["assistantMessageEvent"]["delta"] for e in deltas[:2]) == "Hello 世界🙂"
    assert deltas[0]["message"]["content"][0]["text"] == "Hello "
    assert [e["message"] for e in events if e["type"] == "message_end" and e["message"]["role"] == "assistant"] == [
        first,
        second,
    ]
    assert len([e for e in events if e["type"] == "agent_end"]) == 2
    assert len([e for e in events if e["type"] == "settled"]) == 2


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
            "prompt_tokens_details": {"cached_tokens": 3},
        },
    ],
)
async def test_optional_model_information_is_preserved_without_inventing_usage(tmp_path: Path, usage: Any) -> None:
    body = sse(
        {
            "id": "response-1",
            "model": "routed-model",
            "choices": [{"index": 0, "delta": {"reasoning_content": "plan"}}],
        },
        chunk("answer", finish="length"),
        {"choices": [], "usage": usage},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))) as client:
        harness = sdk(tmp_path, client)
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda e: events.append(copy.deepcopy(e)))
        result = await asyncio.wait_for(harness.prompt("go"), 1)

    assert result["stopReason"] == "length"
    assert result["content"] == [
        {"type": "thinking", "thinking": "plan", "thinkingSignature": "reasoning_content"},
        {"type": "text", "text": "answer"},
    ]
    assert result["responseId"] == "response-1"
    assert result["model"] == "test-model"
    assert result["responseModel"] == "routed-model"
    if usage is None:
        assert "usage" not in result
    elif usage["total_tokens"] == 0:
        assert result["usage"] == {"input": 0, "output": 0, "totalTokens": 0}
    else:
        assert result["usage"] == {"input": 9, "output": 4, "totalTokens": 16, "cacheRead": 3}
    thinking = [
        e["assistantMessageEvent"]
        for e in events
        if e["type"] == "message_update" and e["assistantMessageEvent"]["type"] == "thinking_delta"
    ]
    assert "".join(e["delta"] for e in thinking) == "plan"
    assert next(e["message"] for e in events if e["type"] == "turn_end") == result


class ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, body: bytes, size: int) -> None:
        self.body, self.size = body, size
        self.closed = False

    async def __aiter__(self):
        for start in range(0, len(self.body), self.size):
            yield self.body[start : start + self.size]

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("size", [1, 7, 10000])
async def test_public_model_stream_and_completion_share_results_across_utf8_chunks(tmp_path: Path, size: int) -> None:
    body = b": heartbeat\r\n\r\n" + sse(chunk("世界"), chunk("🙂", finish="stop")).replace(b"\n", b"\r\n")
    responses: list[ChunkedBody] = []

    def respond(_: httpx.Request) -> httpx.Response:
        response = ChunkedBody(body, size)
        responses.append(response)
        return httpx.Response(200, stream=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client)
        models, model = harness.models, harness.get_model()
        stream = models.stream_simple(model, {"messages": [{"role": "user", "content": "hi"}]})

        async def consume():
            return [e async for e in stream]

        events, result = await asyncio.wait_for(asyncio.gather(consume(), stream.result()), 1)
        complete = await asyncio.wait_for(models.complete_simple(model, {"messages": []}), 1)
    assert result["content"] == complete["content"] == [{"type": "text", "text": "世界🙂"}]
    assert events[-1] == {"type": "done", "reason": "stop", "message": result}
    assert "".join(e["delta"] for e in events if e["type"] == "text_delta") == "世界🙂"
    assert [e["partial"]["content"][0]["text"] for e in events if e["type"] == "text_delta"] == ["世界", "世界🙂"]
    assert all(response.closed for response in responses)


@pytest.mark.parametrize(
    "failure",
    ["401", "500", "timeout", "stall", "eof", "done_without_finish", "invalid_finish", "bad_json", "provider_error"],
)
async def test_failures_settle_agent_and_model_results(tmp_path: Path, failure: str) -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        if failure in {"401", "500"}:
            return httpx.Response(int(failure), json={"error": {"message": "credential or server failure"}})
        if failure == "timeout":
            raise httpx.ReadTimeout("provider timed out", request=request)
        if failure == "stall":
            await asyncio.sleep(10)
        bodies = {
            "eof": sse(chunk("partial"), done=False),
            "done_without_finish": sse(chunk("partial")),
            "invalid_finish": sse(chunk(finish="content_filter")),
            "bad_json": b"data: {broken}\n\n",
            "provider_error": sse({"error": {"message": "provider rejected request"}}),
        }
        return httpx.Response(200, content=bodies.get(failure, b""))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, timeout_ms=30)
        observed: list[dict[str, Any]] = []
        harness.subscribe(lambda e: observed.append(copy.deepcopy(e)))
        result = await asyncio.wait_for(harness.prompt("go"), 1)
        stream = harness.models.stream_simple(harness.get_model(), {"messages": []})

        async def consume():
            return [e async for e in stream]

        events, model_result = await asyncio.wait_for(asyncio.gather(consume(), stream.result()), 1)
    assert result["stopReason"] == model_result["stopReason"] == "error"
    assert result["errorMessage"]
    assert "usage" not in result
    assert events[-1] == {"type": "error", "reason": "error", "error": model_result}
    assert len([e for e in events if e["type"] in {"done", "error"}]) == 1
    assert len([e for e in observed if e["type"] == "agent_end"]) == 1
    assert observed[-1]["type"] == "settled"
    if failure in {"401", "500"}:
        assert "credential or server failure" in result["errorMessage"]


async def test_sdk_uses_existing_hooks_and_explicit_session_without_saving_credentials(tmp_path: Path) -> None:
    from pi_agent_core import InMemorySessionStorage, Session

    requests: list[httpx.Request] = []
    session = Session(InMemorySessionStorage())

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=sse({"choices": [{"delta": {"reasoning_content": "plan"}}]}, chunk("answer", finish="stop")),
            headers={"x-provider": "test"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, session=session)
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda e: events.append(copy.deepcopy(e)))
        harness.on("before_agent_start", lambda e: {"systemPrompt": e["systemPrompt"] + " From hook."})
        harness.on(
            "before_provider_request", lambda _: {"streamOptions": {"headers": {"x-hook": "yes"}, "timeoutMs": 500}}
        )

        def payload_hook(event):
            payload = event["payload"]
            payload["max_tokens"] = payload.pop("max_completion_tokens")
            return {"payload": payload}

        harness.on("before_provider_payload", payload_hook)
        await harness.prompt("first")
        await harness.prompt("second")
        assert not client.is_closed

    payload = json.loads(requests[-1].content)
    assert payload["messages"] == [
        {"role": "system", "content": "Be helpful. From hook."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer", "reasoning_content": "plan"},
        {"role": "user", "content": "second"},
    ]
    assert payload["max_tokens"] == 123
    assert requests[-1].headers["x-hook"] == "yes"
    assert requests[-1].extensions["timeout"]["read"] == 0.5
    responses = [e for e in events if e["type"] == "after_provider_response"]
    assert responses[0]["status"] == 200
    assert responses[0]["headers"]["x-provider"] == "test"
    context = await session.build_context()
    assert len(context["messages"]) == 4
    assert context["messages"][-1]["content"][-1]["text"] == "answer"
    assert "test-secret" not in repr(await session.get_entries())


async def test_model_completion_accepts_shared_request_options(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=sse(chunk("summary", finish="stop")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client)
        result = await harness.models.complete_simple(
            harness.get_model(),
            {"systemPrompt": "Summarize.", "messages": [{"role": "user", "content": "history"}]},
            {"maxTokens": 50, "temperature": 0, "headers": {"x-summary": "yes"}},
        )
    payload = json.loads(requests[0].content)
    assert result["content"] == [{"type": "text", "text": "summary"}]
    assert payload["max_completion_tokens"] == 50
    assert payload["temperature"] == 0
    assert requests[0].headers["x-summary"] == "yes"


@pytest.mark.parametrize("stage", ["connect", "read"])
async def test_harness_abort_releases_the_active_http_request(tmp_path: Path, stage: str) -> None:
    active = asyncio.Event()
    released = asyncio.Event()

    class WaitingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(chunk("partial"), done=False)
            active.set()
            await asyncio.Event().wait()

        async def aclose(self):
            released.set()

    async def respond(_: httpx.Request) -> httpx.Response:
        if stage == "connect":
            active.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()
        return httpx.Response(200, stream=WaitingBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client)
        prompt = asyncio.create_task(harness.prompt("go"))
        await asyncio.wait_for(active.wait(), 1)
        await asyncio.wait_for(harness.abort(), 1)
        result = await asyncio.wait_for(prompt, 1)
    assert result["stopReason"] == "aborted"
    assert released.is_set()


async def test_model_terminal_result_waits_for_http_cleanup(tmp_path: Path) -> None:
    class BrokenClose(ChunkedBody):
        async def aclose(self) -> None:
            raise httpx.ReadError("response cleanup failed")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=BrokenClose(sse(chunk("text", finish="stop")), 10000))
        )
    ) as client:
        harness = sdk(tmp_path, client)
        stream = harness.models.stream_simple(harness.get_model(), {"messages": []})
        events = [e async for e in stream]
        result = await asyncio.wait_for(stream.result(), 1)
    assert result["stopReason"] == "error"
    assert events[-1] == {"type": "error", "reason": "error", "error": result}


async def test_pre_aborted_model_stream_settles_without_sending_http(tmp_path: Path) -> None:
    from pi_agent_core import AbortController

    def respond(_: httpx.Request) -> httpx.Response:
        pytest.fail("A pre-aborted request must not reach the provider")

    controller = AbortController()
    controller.abort()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client)
        stream = harness.models.stream_simple(harness.get_model(), {"messages": []}, {"signal": controller.signal})
        events = [event async for event in stream]
        result = await asyncio.wait_for(stream.result(), 1)
    assert result["stopReason"] == "aborted"
    assert events == [{"type": "error", "reason": "aborted", "error": result}]


async def test_response_read_timeout_settles_and_closes_http(tmp_path: Path) -> None:
    closed = asyncio.Event()

    class StalledBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse(chunk("partial"), done=False)
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=StalledBody()))
    ) as client:
        harness = sdk(tmp_path, client, timeout_ms=30)
        result = await asyncio.wait_for(harness.prompt("go"), 1)
    assert result["stopReason"] == "error"
    assert result["errorMessage"] == "TimeoutError"
    assert closed.is_set()
