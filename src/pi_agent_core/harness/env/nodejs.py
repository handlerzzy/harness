from __future__ import annotations

import asyncio
import codecs
import errno
import math
import ntpath
import os
import posixpath
import re
import shutil
import signal as os_signal
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ...abort import AbortSignal
from ..types import ExecutionError, FileError, Result, err, ok

MAX_TIMEOUT_MS = 2_147_483_647
MAX_TIMEOUT_SECONDS = MAX_TIMEOUT_MS / 1000


def _is_legacy_wsl_bash_path(path: str) -> bool:
    normalized = path.replace("/", "\\").lower()
    return re.fullmatch(r"[a-z]:\\windows\\(?:system32|sysnative)\\bash\.exe", normalized) is not None


def _shell_arguments(shell: str, command: str) -> tuple[list[str], bytes | None]:
    if _is_legacy_wsl_bash_path(shell):
        return [shell, "-s"], command.encode("utf-8")
    return [shell, "-c", command], None


def _find_windows_bash(environment: Mapping[str, str]) -> str | None:
    candidates: list[str] = []
    if program_files := environment.get("ProgramFiles"):
        candidates.append(os.path.join(program_files, "Git", "bin", "bash.exe"))
    if program_files_x86 := environment.get("ProgramFiles(x86)"):
        candidates.append(os.path.join(program_files_x86, "Git", "bin", "bash.exe"))
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return shutil.which("bash.exe") or shutil.which("bash")


def _resolve_path(cwd: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.abspath(os.path.join(cwd, path))


def _node_join(parts: list[str]) -> str:
    if not parts or not any(parts):
        return "."
    if os.name == "nt":
        return ntpath.normpath("\\".join(parts))
    normalized = posixpath.normpath("/".join(parts))
    return "/" + normalized.lstrip("/") if normalized.startswith("//") else normalized


def _to_file_error(error: BaseException, path: str | None = None) -> FileError:
    if isinstance(error, FileError):
        return error
    if isinstance(error, asyncio.CancelledError):
        return FileError("aborted", "aborted", path, error)
    raw_code = getattr(error, "errno", None)
    code = raw_code if isinstance(raw_code, int) else -1
    mapping = {
        errno.ENOENT: "not_found",
        errno.EACCES: "permission_denied",
        errno.EPERM: "permission_denied",
        errno.ENOTDIR: "not_directory",
        errno.EISDIR: "is_directory",
        errno.EINVAL: "invalid",
    }
    return FileError(mapping.get(code, "unknown"), str(error), path, error)


def _abort_result(signal: AbortSignal | None, path: str | None = None) -> Result[Any, FileError] | None:
    return err(FileError("aborted", "aborted", path)) if signal and signal.aborted else None


def _file_info(path: str) -> Result[dict[str, Any], FileError]:
    try:
        stats = os.lstat(path)
        if os.path.islink(path):
            kind = "symlink"
        elif os.path.isfile(path):
            kind = "file"
        elif os.path.isdir(path):
            kind = "directory"
        else:
            return err(FileError("invalid", "Unsupported file type", path))
        return ok(
            {
                "name": path.rstrip("/").split("/")[-1],
                "path": path,
                "kind": kind,
                "size": stats.st_size,
                "mtimeMs": stats.st_mtime * 1000,
            }
        )
    except BaseException as error:
        return err(_to_file_error(error, path))


class NodeExecutionEnv:
    """Local Python implementation of the upstream NodeExecutionEnv contract."""

    def __init__(
        self,
        options: Mapping[str, Any] | None = None,
        *,
        cwd: str | None = None,
        shell_path: str | None = None,
        shell_env: Mapping[str, str] | None = None,
    ) -> None:
        options = options or {}
        self.cwd = str(cwd if cwd is not None else options.get("cwd") or os.getcwd())
        self.shell_path = shell_path or options.get("shellPath", options.get("shell_path"))
        self.shell_env = dict(shell_env or options.get("shellEnv", options.get("shell_env", {})))

    async def absolute_path(self, path: str, _abort_signal: AbortSignal | None = None) -> Result[str, FileError]:
        return ok(_resolve_path(self.cwd, path))

    async def join_path(self, parts: list[str], _abort_signal: AbortSignal | None = None) -> Result[str, FileError]:
        return ok(_node_join(parts))

    async def exec(
        self, command: str, options: Mapping[str, Any] | None = None
    ) -> Result[dict[str, Any], ExecutionError]:
        options = options or {}
        abort_signal = options.get("abortSignal", options.get("abort_signal"))
        if abort_signal and abort_signal.aborted:
            return err(ExecutionError("aborted", "aborted"))
        timeout = options.get("timeout")
        if timeout is not None:
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                return err(ExecutionError("timeout", "Invalid timeout: must be a finite number of seconds"))
            if timeout * 1000 > MAX_TIMEOUT_MS:
                return err(ExecutionError("timeout", f"Invalid timeout: maximum is {MAX_TIMEOUT_SECONDS} seconds"))

        shell = self.shell_path
        if shell:
            if not Path(shell).exists():
                return err(ExecutionError("shell_unavailable", f"Custom shell path not found: {shell}"))
        elif os.name == "nt":
            shell = _find_windows_bash(os.environ)
            if not shell:
                return err(ExecutionError("shell_unavailable", "No bash shell found"))
        else:
            shell = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash") or "sh"

        option_cwd = options.get("cwd")
        working_dir = self.cwd if not option_cwd else _resolve_path(self.cwd, str(option_cwd))
        environment = {**os.environ, **self.shell_env, **dict(options.get("env", {}))}
        on_stdout = options.get("onStdout", options.get("on_stdout"))
        on_stderr = options.get("onStderr", options.get("on_stderr"))
        process: asyncio.subprocess.Process | None = None
        try:
            kwargs: dict[str, Any] = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                kwargs["start_new_session"] = True
            argv, stdin_content = _shell_arguments(shell, command)
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=working_dir,
                env=environment,
                stdin=asyncio.subprocess.PIPE if stdin_content is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            if stdin_content is not None and process.stdin is not None:
                process.stdin.write(stdin_content)
                await process.stdin.drain()
                process.stdin.close()
        except BaseException as error:
            return err(ExecutionError("spawn_error", str(error), error))

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        callback_error: ExecutionError | None = None

        def kill_tree() -> None:
            if process is None or process.returncode is not None:
                return
            try:
                if os.name == "nt":
                    subprocess.Popen(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    os.killpg(process.pid, os_signal.SIGKILL)
            except (OSError, ProcessLookupError, PermissionError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        remove_abort = abort_signal.add_callback(kill_tree) if abort_signal else lambda: None

        async def pump(
            reader: asyncio.StreamReader | None,
            target: list[str],
            callback: Callable[[str], object] | None,
        ) -> None:
            nonlocal callback_error
            if reader is None:
                return
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    text = decoder.decode(b"", final=True)
                    if text:
                        target.append(text)
                        if callback is not None:
                            try:
                                callback(text)
                            except BaseException as error:
                                callback_error = ExecutionError("callback_error", str(error), error)
                                kill_tree()
                    return
                text = decoder.decode(chunk, final=False)
                if not text:
                    continue
                target.append(text)
                if callback is not None:
                    try:
                        callback(text)
                    except BaseException as error:
                        callback_error = ExecutionError("callback_error", str(error), error)
                        kill_tree()

        stdout_task = asyncio.create_task(pump(process.stdout, stdout_parts, on_stdout))
        stderr_task = asyncio.create_task(pump(process.stderr, stderr_parts, on_stderr))
        timed_out = False
        try:
            if timeout is None:
                await process.wait()
            else:
                try:
                    await asyncio.wait_for(process.wait(), float(timeout))
                except TimeoutError:
                    timed_out = True
                    kill_tree()
                    await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
        finally:
            remove_abort()

        if callback_error is not None:
            return err(callback_error)
        if timed_out:
            return err(ExecutionError("timeout", f"timeout:{timeout}"))
        if abort_signal and abort_signal.aborted:
            return err(ExecutionError("aborted", "aborted"))
        return ok(
            {
                "stdout": "".join(stdout_parts),
                "stderr": "".join(stderr_parts),
                "exitCode": process.returncode if process.returncode is not None and process.returncode >= 0 else 0,
            }
        )

    async def read_text_file(self, path: str, abort_signal: AbortSignal | None = None) -> Result[str, FileError]:
        resolved = _resolve_path(self.cwd, path)
        if aborted := _abort_result(abort_signal, resolved):
            return aborted
        try:
            value = await asyncio.to_thread(Path(resolved).read_text, encoding="utf-8", errors="replace")
            if aborted := _abort_result(abort_signal, resolved):
                return aborted
            return ok(value)
        except BaseException as error:
            return err(_to_file_error(error, resolved))

    async def read_text_lines(
        self, path: str, options: Mapping[str, Any] | None = None
    ) -> Result[list[str], FileError]:
        options = options or {}
        signal = options.get("abortSignal", options.get("abort_signal"))
        max_lines = options.get("maxLines", options.get("max_lines"))
        resolved = _resolve_path(self.cwd, path)
        if aborted := _abort_result(signal, resolved):
            return aborted
        if max_lines is not None and max_lines <= 0:
            return ok([])

        def read() -> list[str]:
            lines: list[str] = []
            with open(resolved, encoding="utf-8", errors="replace", newline=None) as file:
                for raw_line in file:
                    if signal and signal.aborted:
                        raise asyncio.CancelledError()
                    lines.append(raw_line.rstrip("\r\n"))
                    if max_lines is not None and len(lines) >= max_lines:
                        break
            return lines

        try:
            lines = await asyncio.to_thread(read)
            if aborted := _abort_result(signal, resolved):
                return aborted
            return ok(lines)
        except BaseException as error:
            return err(_to_file_error(error, resolved))

    async def read_binary_file(self, path: str, abort_signal: AbortSignal | None = None) -> Result[bytes, FileError]:
        resolved = _resolve_path(self.cwd, path)
        if aborted := _abort_result(abort_signal, resolved):
            return aborted
        try:
            content = await asyncio.to_thread(Path(resolved).read_bytes)
            if aborted := _abort_result(abort_signal, resolved):
                return aborted
            return ok(content)
        except BaseException as error:
            return err(_to_file_error(error, resolved))

    async def write_file(
        self, path: str, content: str | bytes, abort_signal: AbortSignal | None = None
    ) -> Result[None, FileError]:
        resolved = _resolve_path(self.cwd, path)
        if aborted := _abort_result(abort_signal, resolved):
            return aborted
        try:
            await asyncio.to_thread(Path(resolved).parent.mkdir, parents=True, exist_ok=True)
            if aborted := _abort_result(abort_signal, resolved):
                return aborted
            if isinstance(content, str):
                await asyncio.to_thread(Path(resolved).write_text, content, encoding="utf-8")
            else:
                await asyncio.to_thread(Path(resolved).write_bytes, content)
            if aborted := _abort_result(abort_signal, resolved):
                return aborted
            return ok(None)
        except BaseException as error:
            return err(_to_file_error(error, resolved))

    async def append_file(
        self, path: str, content: str | bytes, abort_signal: AbortSignal | None = None
    ) -> Result[None, FileError]:
        resolved = _resolve_path(self.cwd, path)
        if aborted := _abort_result(abort_signal, resolved):
            return aborted

        def append() -> None:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                with open(resolved, "a", encoding="utf-8") as file:
                    file.write(content)
            else:
                with open(resolved, "ab") as file:
                    file.write(content)

        try:
            await asyncio.to_thread(append)
            return ok(None)
        except BaseException as error:
            return err(_to_file_error(error, resolved))

    async def file_info(self, path: str, abort_signal: AbortSignal | None = None) -> Result[dict[str, Any], FileError]:
        resolved = _resolve_path(self.cwd, path)
        if aborted := _abort_result(abort_signal, resolved):
            return aborted
        return await asyncio.to_thread(_file_info, resolved)

    async def list_dir(
        self, path: str, abort_signal: AbortSignal | None = None
    ) -> Result[list[dict[str, Any]], FileError]:
        resolved = _resolve_path(self.cwd, path)
        if aborted := _abort_result(abort_signal, resolved):
            return aborted
        try:
            names = await asyncio.to_thread(os.listdir, resolved)
            infos: list[dict[str, Any]] = []
            for name in names:
                if aborted := _abort_result(abort_signal, resolved):
                    return aborted
                result = await asyncio.to_thread(_file_info, os.path.join(resolved, name))
                if not result["ok"]:
                    if result["error"].code == "invalid" and str(result["error"]) == "Unsupported file type":
                        continue
                    return result
                infos.append(result["value"])
            return ok(infos)
        except BaseException as error:
            return err(_to_file_error(error, resolved))

    async def canonical_path(self, path: str, abort_signal: AbortSignal | None = None) -> Result[str, FileError]:
        resolved = _resolve_path(self.cwd, path)
        if aborted := _abort_result(abort_signal, resolved):
            return aborted
        try:
            canonical = await asyncio.to_thread(os.path.realpath, resolved, strict=True)
            return ok(canonical)
        except BaseException as error:
            return err(_to_file_error(error, resolved))

    async def exists(self, path: str, abort_signal: AbortSignal | None = None) -> Result[bool, FileError]:
        result = await self.file_info(path, abort_signal)
        if result["ok"]:
            return ok(True)
        if result["error"].code == "not_found":
            return ok(False)
        return result

    async def create_dir(self, path: str, options: Mapping[str, Any] | None = None) -> Result[None, FileError]:
        options = options or {}
        signal = options.get("abortSignal", options.get("abort_signal"))
        resolved = _resolve_path(self.cwd, path)
        if aborted := _abort_result(signal, resolved):
            return aborted
        recursive = options.get("recursive", True)
        try:
            if recursive:
                await asyncio.to_thread(Path(resolved).mkdir, parents=True, exist_ok=True)
            else:
                await asyncio.to_thread(Path(resolved).mkdir)
            return ok(None)
        except BaseException as error:
            return err(_to_file_error(error, resolved))

    async def remove(self, path: str, options: Mapping[str, Any] | None = None) -> Result[None, FileError]:
        options = options or {}
        signal = options.get("abortSignal", options.get("abort_signal"))
        resolved = _resolve_path(self.cwd, path)
        if aborted := _abort_result(signal, resolved):
            return aborted
        recursive = options.get("recursive", False)
        force = options.get("force", False)
        try:
            if not os.path.lexists(resolved):
                if force:
                    return ok(None)
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), resolved)
            if recursive and os.path.isdir(resolved) and not os.path.islink(resolved):
                await asyncio.to_thread(shutil.rmtree, resolved)
            else:
                await asyncio.to_thread(os.unlink, resolved)
            return ok(None)
        except BaseException as error:
            return err(_to_file_error(error, resolved))

    async def create_temp_dir(
        self, prefix: str = "tmp-", abort_signal: AbortSignal | None = None
    ) -> Result[str, FileError]:
        if aborted := _abort_result(abort_signal):
            return aborted
        try:
            return ok(await asyncio.to_thread(tempfile.mkdtemp, prefix=prefix))
        except BaseException as error:
            return err(_to_file_error(error))

    async def create_temp_file(self, options: Mapping[str, Any] | None = None) -> Result[str, FileError]:
        options = options or {}
        signal = options.get("abortSignal", options.get("abort_signal"))
        if aborted := _abort_result(signal):
            return aborted
        directory = await self.create_temp_dir("tmp-", signal)
        if not directory["ok"]:
            return directory
        path = os.path.join(directory["value"], f"{options.get('prefix', '')}{uuid.uuid4()}{options.get('suffix', '')}")
        try:
            await asyncio.to_thread(Path(path).write_text, "", encoding="utf-8")
            return ok(path)
        except BaseException as error:
            return err(_to_file_error(error, path))

    async def cleanup(self) -> None:
        return None

    absolutePath = absolute_path
    joinPath = join_path
    readTextFile = read_text_file
    readTextLines = read_text_lines
    readBinaryFile = read_binary_file
    writeFile = write_file
    appendFile = append_file
    fileInfo = file_info
    listDir = list_dir
    canonicalPath = canonical_path
    createDir = create_dir
    createTempDir = create_temp_dir
    createTempFile = create_temp_file
