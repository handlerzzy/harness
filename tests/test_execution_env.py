from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from aster_harness import AbortController
from aster_harness._utils import js_utf16_length, js_utf16_slice
from aster_harness.harness.env import NodeExecutionEnv
from aster_harness.harness.env.nodejs import _find_windows_bash, _shell_arguments
from aster_harness.harness.session import JsonlSessionStorage, Session, load_jsonl_session_metadata
from aster_harness.harness.types import get_or_throw
from aster_harness.harness.types import ok as harness_ok
from aster_harness.harness.utils import (
    execute_shell_with_capture,
    format_size,
    truncate_head,
    truncate_line,
    truncate_tail,
)


async def test_filesystem_and_jsonl_round_trip(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    get_or_throw(await env.write_file("nested/file.txt", "one\ntwo\nthree\n"))
    assert get_or_throw(await env.read_text_file("nested/file.txt")) == "one\ntwo\nthree\n"
    assert get_or_throw(await env.read_text_lines("nested/file.txt", {"maxLines": 2})) == ["one", "two"]
    infos = get_or_throw(await env.list_dir("nested"))
    assert [(item["name"], item["kind"]) for item in infos] == [("file.txt", "file")]

    path = str(tmp_path / "session.jsonl")
    storage = await JsonlSessionStorage.create(
        env,
        path,
        {"cwd": str(tmp_path), "sessionId": "session-1", "metadata": {"project": "test"}},
    )
    session = Session(storage)
    entry_id = await session.append_message({"role": "user", "content": "hello", "timestamp": 1})
    metadata = await load_jsonl_session_metadata(env, path)
    reopened = await JsonlSessionStorage.open(env, path)

    assert metadata["id"] == "session-1"
    assert metadata["metadata"] == {"project": "test"}
    assert await reopened.get_leaf_id() == entry_id
    assert (await reopened.get_entry(entry_id))["message"]["content"] == "hello"


def test_format_size_uses_javascript_tofixed_rounding() -> None:
    assert format_size(1280) == "1.3KB"


async def test_shell_exec_nonzero_timeout_and_capture(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    result = get_or_throw(await env.exec("printf out; printf err >&2; exit 7"))
    assert result == {"stdout": "out", "stderr": "err", "exitCode": 7}

    timeout = await env.exec("sleep 1", {"timeout": 0.01})
    assert not timeout["ok"] and timeout["error"].code == "timeout"

    capture = get_or_throw(await execute_shell_with_capture(env, "python3 -c \"print('x' * 60000)\""))
    assert capture["truncated"]
    assert Path(capture["fullOutputPath"]).exists()
    assert os.path.getsize(capture["fullOutputPath"]) > 50_000


async def test_shell_capture_streams_full_output_in_order_without_buffering_everything() -> None:
    class FakeEnv:
        def __init__(self) -> None:
            self.appends: list[str] = []
            self.exec_options: dict[str, object] = {}

        async def create_temp_file(self, _options: object) -> dict[str, object]:
            return harness_ok("/virtual/full.log")

        async def append_file(self, _path: str, content: str, _signal: object = None) -> dict[str, object]:
            await asyncio.sleep(0)
            self.appends.append(content)
            return harness_ok(None)

        async def exec(self, _command: str, options: dict[str, object]) -> dict[str, object]:
            self.exec_options = options
            stdout = options["onStdout"]
            stderr = options["onStderr"]
            assert callable(stdout) and callable(stderr)
            stdout("a" * 30_000)
            stderr("b" * 30_000)
            stdout("c" * 30_000)
            return harness_ok({"stdout": "", "stderr": "", "exitCode": 0})

    env = FakeEnv()

    def marker(_chunk: str) -> None:
        return None

    result = get_or_throw(await execute_shell_with_capture(env, "ignored", {"onChunk": marker}))
    assert env.exec_options["onChunk"] is marker
    assert env.appends == ["a" * 30_000 + "b" * 30_000, "c" * 30_000]
    assert result["truncated"] is True
    assert result["fullOutputPath"] == "/virtual/full.log"

    class SmallEnv(FakeEnv):
        async def exec(self, _command: str, options: dict[str, object]) -> dict[str, object]:
            callback = options["onStdout"]
            assert callable(callback)
            callback("ok")
            return harness_ok({"stdout": "ok", "stderr": "", "exitCode": 0})

    small = get_or_throw(await execute_shell_with_capture(SmallEnv(), "ignored"))
    assert small == {
        "output": "ok",
        "exitCode": 0,
        "cancelled": False,
        "truncated": False,
        "fullOutputPath": None,
    }


def test_truncate_utf8_boundaries() -> None:
    head = truncate_head("alpha\nβeta\ngamma", {"maxBytes": 11, "maxLines": 20})
    assert head["content"] == "alpha\nβeta"
    assert head["truncatedBy"] == "bytes"

    tail = truncate_tail("abcdef", {"maxBytes": 3, "maxLines": 20})
    assert tail["content"] == "def"
    assert tail["lastLinePartial"]
    assert truncate_head("a\nb", {"maxLines": -1})["content"] == ""

    assert js_utf16_length("🙂") == 2
    assert js_utf16_slice("a🙂b", 1, 3) == "🙂"
    split_surrogate = truncate_line("🙂x", 1)
    assert split_surrogate["wasTruncated"]
    assert ord(split_surrogate["text"][0]) == 0xD83D


async def test_text_reads_and_process_streaming_match_node_utf8_replacement(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    (tmp_path / "invalid.txt").write_bytes(b"a\xffb")
    assert get_or_throw(await env.read_text_file("invalid.txt")) == "a�b"
    assert get_or_throw(await env.read_text_lines("invalid.txt")) == ["a�b"]

    command = "python3 -c \"import os,time; os.write(1,b'\\xf0'); time.sleep(0.02); os.write(1,b'\\x9f\\x99\\x82')\""
    result = get_or_throw(await env.exec(command))
    assert result["stdout"] == "🙂"


def test_windows_shell_discovery_and_legacy_wsl_transport(tmp_path: Path) -> None:
    git_bash = tmp_path / "Git" / "bin" / "bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.write_bytes(b"")
    assert _find_windows_bash({"ProgramFiles": str(tmp_path)}) == str(git_bash)

    argv, stdin = _shell_arguments(r"C:\Windows\System32\bash.exe", "printf ok")
    assert argv == [r"C:\Windows\System32\bash.exe", "-s"]
    assert stdin == b"printf ok"
    argv, stdin = _shell_arguments("/bin/bash", "printf ok")
    assert argv == ["/bin/bash", "-c", "printf ok"] and stdin is None


async def test_file_info_symlinks_binary_remove_and_preaborted_operations(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    get_or_throw(await env.write_file("dir/file.bin", b"\x00\x01"))
    os.symlink(tmp_path / "dir" / "file.bin", tmp_path / "link.bin")
    assert get_or_throw(await env.file_info("dir"))["kind"] == "directory"
    assert get_or_throw(await env.file_info("link.bin"))["kind"] == "symlink"
    assert get_or_throw(await env.canonical_path("link.bin")) == str((tmp_path / "dir" / "file.bin").resolve())
    assert get_or_throw(await env.read_binary_file("link.bin")) == b"\x00\x01"
    listed = sorted((item["name"], item["kind"]) for item in get_or_throw(await env.list_dir(".")))
    assert listed == [("dir", "directory"), ("link.bin", "symlink")]

    not_directory = await env.list_dir("dir/file.bin")
    assert not not_directory["ok"] and not_directory["error"].code == "not_directory"
    nonrecursive = await env.remove("dir", {"recursive": False})
    assert not nonrecursive["ok"]
    get_or_throw(await env.remove("dir", {"recursive": True}))
    get_or_throw(await env.remove("missing", {"force": True}))

    get_or_throw(await env.write_file("abort.txt", "text"))
    controller = AbortController()
    controller.abort()
    results = [
        await env.read_text_file("abort.txt", controller.signal),
        await env.read_text_lines("abort.txt", {"abortSignal": controller.signal}),
        await env.read_binary_file("abort.txt", controller.signal),
        await env.write_file("other.txt", "text", controller.signal),
        await env.list_dir(".", controller.signal),
    ]
    assert all(not result["ok"] and result["error"].code == "aborted" for result in results)


async def test_append_temp_create_dir_cleanup_and_timeout_validation(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))

    assert get_or_throw(await env.join_path([str(tmp_path), "a", "..", "b"])) == str(tmp_path / "b")
    assert get_or_throw(await env.join_path([])) == "."
    assert get_or_throw(await env.join_path(["a", "/b"])) == "a/b"
    assert get_or_throw(await env.join_path(["/a", "/b"])) == "/a/b"
    assert get_or_throw(await env.join_path(["//a", "b"])) == "/a/b"

    absolute_with_parent = f"{tmp_path}/nested/../file.txt"
    assert get_or_throw(await env.absolute_path(absolute_with_parent)) == absolute_with_parent

    get_or_throw(await env.append_file("new/nested/file.txt", "a"))
    get_or_throw(await env.append_file("new/nested/file.txt", "b"))
    assert get_or_throw(await env.read_text_file("new/nested/file.txt")) == "ab"

    nonrecursive = await env.create_dir("missing/child", {"recursive": False})
    assert not nonrecursive["ok"] and nonrecursive["error"].code == "not_found"
    get_or_throw(await env.create_dir("existing", {"recursive": False}))
    already_exists = await env.create_dir("existing", {"recursive": False})
    assert not already_exists["ok"]

    temp_dir = Path(get_or_throw(await env.create_temp_dir("pi-agent-test-")))
    temp_file = Path(get_or_throw(await env.create_temp_file({"prefix": "prefix-", "suffix": ".txt"})))
    assert temp_dir.is_dir() and temp_dir.name.startswith("pi-agent-test-")
    assert temp_file.is_file() and temp_file.name.startswith("prefix-") and temp_file.suffix == ".txt"
    await env.cleanup()

    for invalid in (0, -1, float("nan"), float("inf"), True, "1"):
        result = await env.exec("true", {"timeout": invalid})
        assert not result["ok"] and result["error"].code == "timeout"
    too_large = await env.exec("true", {"timeout": 2_147_483.648})
    assert not too_large["ok"] and "maximum" in str(too_large["error"])


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO files are not available on this platform")
async def test_list_dir_skips_unsupported_entries_and_signal_exit_matches_node(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    get_or_throw(await env.write_file("regular.txt", "ok"))
    os.mkfifo(tmp_path / "pipe")
    listed = get_or_throw(await env.list_dir("."))
    assert [(item["name"], item["kind"]) for item in listed] == [("regular.txt", "file")]
    unsupported = await env.file_info("pipe")
    assert not unsupported["ok"] and unsupported["error"].code == "invalid"

    signalled = get_or_throw(await env.exec("kill -TERM $$"))
    assert signalled["exitCode"] == 0


async def test_shell_stream_callbacks_abort_and_failures(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    stdout: list[str] = []
    stderr: list[str] = []
    result = get_or_throw(
        await env.exec(
            "printf out; printf err >&2",
            {"onStdout": stdout.append, "onStderr": stderr.append, "env": {"PI_ENV_TEST": "ok"}},
        )
    )
    assert result == {"stdout": "out", "stderr": "err", "exitCode": 0}
    assert "".join(stdout) == "out" and "".join(stderr) == "err"

    callback = await env.exec("printf out", {"onStdout": lambda _chunk: (_ for _ in ()).throw(ValueError("boom"))})
    assert not callback["ok"] and callback["error"].code == "callback_error"
    assert str(callback["error"]) == "boom"

    missing = NodeExecutionEnv(cwd=str(tmp_path), shell_path=str(tmp_path / "missing-shell"))
    unavailable = await missing.exec("true")
    assert not unavailable["ok"] and unavailable["error"].code == "shell_unavailable"
    shell_file = tmp_path / "not-executable"
    shell_file.write_text("no", encoding="utf-8")
    spawn = await NodeExecutionEnv(cwd=str(tmp_path), shell_path=str(shell_file)).exec("true")
    assert not spawn["ok"] and spawn["error"].code == "spawn_error"

    controller = AbortController()
    running = asyncio.create_task(env.exec("sleep 5", {"abortSignal": controller.signal}))
    await asyncio.sleep(0.01)
    controller.abort()
    aborted = await running
    assert not aborted["ok"] and aborted["error"].code == "aborted"


def _node_utf8_normalize(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        code = ord(value[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < len(value) and 0xDC00 <= ord(value[index + 1]) <= 0xDFFF:
            combined = 0x10000 + ((code - 0xD800) << 10) + (ord(value[index + 1]) - 0xDC00)
            output.append(chr(combined))
            index += 2
        elif 0xD800 <= code <= 0xDFFF:
            output.append("\ufffd")
            index += 1
        else:
            output.append(value[index])
            index += 1
    return "".join(output)


@pytest.mark.parametrize("value", ["aé🙂b", "abc🙂", "a\ud83d", "\ude42b", "👩‍💻"])
def test_truncate_tail_matches_node_buffer_boundaries(value: str) -> None:
    normalized = _node_utf8_normalize(value)
    encoded = normalized.encode("utf-8")
    for maximum in range(len(encoded) + 3):
        if len(encoded) <= maximum:
            expected = value
        else:
            start = len(encoded) - maximum
            while start < len(encoded) and encoded[start] & 0xC0 == 0x80:
                start += 1
            expected = encoded[start:].decode("utf-8") if start < len(encoded) else ""
        assert truncate_tail(value, {"maxBytes": maximum, "maxLines": 10})["content"] == expected
