from __future__ import annotations

import asyncio
import copy
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from .test_sdk import chunk, sdk, sse
from .test_sdk_tools import call, recorder, tool_chunk


def tool_response(name: str, arguments: dict[str, Any], call_id: str = "work") -> bytes:
    return sse(tool_chunk(call(0, json.dumps(arguments), call_id=call_id, name=name), finish="tool_calls"))


def python_command(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


async def wait_for_file(path: Path) -> None:
    for _ in range(200):
        if await asyncio.to_thread(path.exists):
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(f"Command did not create {path}")


def read_and_remove_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()


async def test_coding_task_reads_repairs_validates_and_consumes_real_results(tmp_path: Path) -> None:
    source = tmp_path / "answer.py"
    source.write_text("answer = 41\n", encoding="utf-8")
    requests: list[dict[str, Any]] = []
    responses = [
        tool_response("read", {"path": "answer.py"}),
        tool_response("edit", {"path": "answer.py", "oldText": "41", "newText": "42"}),
        tool_response(
            "bash", {"command": python_command("from answer import answer; assert answer == 42; print('verified')")}
        ),
        tool_response("write", {"path": "reports/result.txt", "content": "verified 世界\n"}),
        tool_response("record", {"value": "done"}),
        sse(chunk("Repair verified.", finish="stop")),
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=responses[len(requests) - 1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, builtin_tools=True, tools=[recorder(tmp_path / "record.txt")])
        result = await asyncio.wait_for(harness.prompt("Repair and verify answer.py"), 5)

    assert result["stopReason"] == "stop"
    assert source.read_text() == "answer = 42\n"
    assert (tmp_path / "reports/result.txt").read_text() == "verified 世界\n"
    assert (tmp_path / "record.txt").read_text() == "work:done\n"
    assert {tool["function"]["name"] for tool in requests[0]["tools"]} == {"read", "write", "edit", "bash", "record"}
    assert requests[1]["messages"][-1]["content"] == "answer = 41\n"
    assert "verified" in requests[3]["messages"][-1]["content"]
    assert "Exit code: 0" in requests[3]["messages"][-1]["content"]


async def test_shell_timeout_stops_children_even_after_shell_leader_exits(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []
    command = "(printf ready; sleep 0.5; echo leaked > leaked.txt) & exit 0"

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = (
            tool_response("bash", {"command": command, "timeout": 0.1})
            if len(requests) == 1
            else sse(chunk("Command timed out.", finish="stop"))
        )
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, builtin_tools=True)
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda event: events.append(copy.deepcopy(event)))
        result = await asyncio.wait_for(harness.prompt("Run with a timeout"), 2)

    assert result["stopReason"] == "stop"
    assert "timed out" in requests[1]["messages"][-1]["content"]
    assert "ready" in requests[1]["messages"][-1]["content"]
    assert next(e for e in events if e["type"] == "tool_execution_end")["isError"] is True
    await asyncio.sleep(0.6)
    assert not (tmp_path / "leaked.txt").exists()


@pytest.mark.parametrize("ending", ["success", "nonzero", "timeout", "abort"])
@pytest.mark.parametrize("output_kind", ["bytes", "lines"])
async def test_long_shell_output_has_readable_complete_log_on_every_outcome(
    tmp_path: Path, ending: str, output_kind: str
) -> None:
    expected = "START\n" + ("界" * 40_000 if output_kind == "bytes" else "line\n" * 3000) + "\nEND\n"
    code = (
        "from pathlib import Path; import sys, time; "
        f"sys.stdout.write({expected!r}); sys.stdout.flush(); Path('ready').touch(); "
    )
    code += "time.sleep(2)" if ending in {"timeout", "abort"} else f"sys.exit({7 if ending == 'nonzero' else 0})"
    arguments: dict[str, Any] = {"command": python_command(code)}
    if ending == "timeout":
        arguments["timeout"] = 0.3
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = tool_response("bash", arguments) if len(requests) == 1 else sse(chunk("Output consumed.", finish="stop"))
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, builtin_tools=True)
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda event: events.append(copy.deepcopy(event)))
        task = asyncio.create_task(harness.prompt("Run and inspect output"))
        if ending == "abort":
            try:
                await wait_for_file(tmp_path / "ready")
            finally:
                await asyncio.wait_for(harness.abort(), 2)
        result = await asyncio.wait_for(task, 3)

    end = next(e for e in events if e["type"] == "tool_execution_end")
    text = end["result"]["content"][0]["text"]
    assert end["isError"] is (ending != "success")
    assert "Output truncated" in text
    assert len(text.encode("utf-8")) < 53_000
    match = re.search(r"Full output: (.+)\]", text)
    assert match is not None
    log = Path(match[1])
    assert read_and_remove_log(log) == expected
    if ending == "abort":
        assert result["stopReason"] == "aborted"
        assert len(requests) == 1
    else:
        assert requests[1]["messages"][-1]["content"] == text
        assert result["stopReason"] == "stop"


@pytest.mark.parametrize(
    "name,arguments,error",
    [
        ("read", {"path": "missing.txt"}, "No such file"),
        ("read", {"path": "."}, "directory"),
        ("write", {"path": "target/child", "content": "wrong"}, "exists"),
        ("edit", {"path": "missing.txt", "oldText": "x", "newText": "y"}, "No such file"),
        ("edit", {"path": "target", "oldText": "missing", "newText": "y"}, "Could not find"),
        ("edit", {"path": "target", "oldText": "a", "newText": "y"}, "Ambiguous"),
        # Overlapping occurrences must also count as ambiguous.
        ("edit", {"path": "target", "oldText": "aa", "newText": "y"}, "Ambiguous"),
        ("edit", {"path": "target", "oldText": "", "newText": "y"}, "fewer than 1"),
        ("bash", {"command": "printf problem >&2; exit 7"}, "Exit code: 7"),
        ("bash", {"command": "kill -TERM $$"}, "Exit code: 143"),
        ("bash", {"command": "echo wrong > should-not-run", "timeout": 0}, "must be > 0"),
    ],
)
async def test_model_receives_tool_errors_without_mutating_failed_edit_targets(
    tmp_path: Path, name: str, arguments: dict[str, Any], error: str
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"\xef\xbb\xbfaaa\r\nunchanged\r\n")
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = (
            tool_response(name, arguments) if len(requests) == 1 else sse(chunk("Failure understood.", finish="stop"))
        )
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, builtin_tools=True)
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda event: events.append(copy.deepcopy(event)))
        result = await asyncio.wait_for(harness.prompt("Attempt operation"), 2)

    assert result["stopReason"] == "stop"
    assert error in requests[1]["messages"][-1]["content"]
    assert next(e for e in events if e["type"] == "tool_execution_end")["isError"] is True
    assert target.read_bytes() == b"\xef\xbb\xbfaaa\r\nunchanged\r\n"
    assert not (tmp_path / "missing.txt").exists()
    assert not (tmp_path / "should-not-run").exists()


@pytest.mark.parametrize("leader_exits", [False, True])
async def test_agent_cancellation_kills_shell_children_and_skips_later_work(tmp_path: Path, leader_exits: bool) -> None:
    ready = tmp_path / "ready"
    command = "(echo ready > ready; sleep 0.7; echo leaked > leaked.txt) & "
    command += "exit 0" if leader_exits else "wait"
    requests: list[dict[str, Any]] = []
    body = sse(
        tool_chunk(
            call(0, json.dumps({"command": command}), call_id="running", name="bash"),
            call(1, json.dumps({"path": "later.txt", "content": "wrong"}), call_id="later", name="write"),
            finish="tool_calls",
        )
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, builtin_tools=True)
        events: list[dict[str, Any]] = []
        harness.subscribe(lambda event: events.append(copy.deepcopy(event)))
        task = asyncio.create_task(harness.prompt("Run until cancelled"))
        try:
            await wait_for_file(ready)
        finally:
            await asyncio.wait_for(harness.abort(), 2)
            result = await asyncio.wait_for(task, 2)

    assert result["stopReason"] == "aborted"
    assert len(requests) == 1
    assert [e["toolCallId"] for e in events if e["type"] == "tool_execution_start"] == ["running"]
    assert not (tmp_path / "later.txt").exists()
    await asyncio.sleep(0.8)
    assert not (tmp_path / "leaked.txt").exists()


async def test_request_retry_does_not_repeat_real_writes_edits_or_commands(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []
    modified_times: list[int] = []
    body = sse(
        tool_chunk(
            call(0, json.dumps({"path": "created.txt", "content": "before\r\n"}), call_id="write", name="write"),
            call(
                1,
                json.dumps({"path": "created.txt", "oldText": "before", "newText": "after"}),
                call_id="edit",
                name="edit",
            ),
            call(
                2, json.dumps({"command": "printf once >> counter.txt; cat created.txt"}), call_id="shell", name="bash"
            ),
            finish="tool_calls",
        )
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, content=body)
        modified_times.append((tmp_path / "created.txt").stat().st_mtime_ns)
        if len(requests) == 2:
            return httpx.Response(503, text="retry this request")
        return httpx.Response(200, content=sse(chunk("Changes verified.", finish="stop")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, builtin_tools=True)
        result = await asyncio.wait_for(harness.prompt("Write, edit and verify"), 3)

    assert result["stopReason"] == "stop"
    assert (tmp_path / "created.txt").read_bytes() == b"after\r\n"
    assert (tmp_path / "counter.txt").read_text() == "once"
    assert len(modified_times) == 2 and modified_times[0] == modified_times[1]
    assert requests[1] == requests[2]
    results = requests[2]["messages"][-3:]
    assert [message["tool_call_id"] for message in results] == ["write", "edit", "shell"]
    assert "after" in results[-1]["content"]
    assert "Exit code: 0" in results[-1]["content"]


async def test_long_text_read_is_bounded_and_points_to_readable_full_file(tmp_path: Path) -> None:
    text = "line\n" * 3000
    source = tmp_path / "long.txt"
    source.write_text(text, encoding="utf-8")
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = (
            tool_response("read", {"path": str(source)}) if len(requests) == 1 else sse(chunk("Read.", finish="stop"))
        )
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        harness = sdk(tmp_path, client, builtin_tools=True)
        result = await asyncio.wait_for(harness.prompt("Read the file"), 2)

    assert result["stopReason"] == "stop"
    output = requests[1]["messages"][-1]["content"]
    assert len(output) < len(text)
    assert "Output truncated" in output
    assert f"Full output: {source}" in output
    assert source.read_text() == text
