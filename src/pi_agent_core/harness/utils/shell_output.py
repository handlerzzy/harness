from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, TypeAlias

from ..._utils import js_utf16_length, maybe_await
from ..session.repo_utils import fs_call
from ..types import ExecutionError, Result, err, ok
from .truncate import DEFAULT_MAX_BYTES, truncate_tail, utf8_byte_length

ShellCaptureOptions: TypeAlias = dict[str, Any]
ShellCaptureResult: TypeAlias = dict[str, Any]


def _to_execution_error(error: object) -> ExecutionError:
    if isinstance(error, ExecutionError):
        return error
    if isinstance(error, BaseException):
        return ExecutionError("unknown", str(error), error)
    return ExecutionError("unknown", str(error))


def sanitize_binary_output(value: str) -> str:
    output: list[str] = []
    for character in value:
        code = ord(character)
        if code in {0x09, 0x0A, 0x0D} or (code > 0x1F and not 0xFFF9 <= code <= 0xFFFB):
            output.append(character)
    return "".join(output)


async def execute_shell_with_capture(
    env: Any,
    command: str,
    options: Mapping[str, Any] | None = None,
) -> Result[dict[str, Any], ExecutionError]:
    options = dict(options or {})
    output_chunks: list[str] = []
    output_code_units = 0
    total_bytes = 0
    max_output_code_units = DEFAULT_MAX_BYTES * 2
    capture_error: ExecutionError | None = None
    on_chunk_callback = options.get("onChunk", options.get("on_chunk"))
    signal = options.get("abortSignal", options.get("abort_signal"))
    full_output_path: str | None = None
    writer_queue: asyncio.Queue[str | object] | None = None
    writer_task: asyncio.Task[ExecutionError | None] | None = None
    writer_end = object()

    async def write_full_output(queue: asyncio.Queue[str | object]) -> ExecutionError | None:
        nonlocal full_output_path
        try:
            temp_result = await fs_call(
                env,
                "create_temp_file",
                "createTempFile",
                # Captured output must finish flushing even after command cancellation.
                {"prefix": "bash-", "suffix": ".log"},
            )
            if not temp_result["ok"]:
                return _to_execution_error(temp_result["error"])
            full_output_path = str(temp_result["value"])
            while True:
                value = await queue.get()
                if value is writer_end:
                    return None
                append_result = await fs_call(
                    env,
                    "append_file",
                    "appendFile",
                    full_output_path,
                    str(value),
                )
                if not append_result["ok"]:
                    return _to_execution_error(append_result["error"])
        except BaseException as error:
            return _to_execution_error(error)

    def ensure_full_output(initial_content: str) -> None:
        nonlocal writer_queue, writer_task
        if writer_queue is not None or capture_error is not None:
            return
        writer_queue = asyncio.Queue()
        writer_queue.put_nowait(initial_content)
        writer_task = asyncio.create_task(write_full_output(writer_queue))

    def append_full_output(text: str) -> None:
        if writer_queue is not None and capture_error is None:
            writer_queue.put_nowait(text)

    def on_chunk(chunk: str) -> None:
        nonlocal output_code_units, total_bytes, capture_error
        try:
            total_bytes += utf8_byte_length(chunk)
            text = sanitize_binary_output(chunk).replace("\r", "")
            if total_bytes > DEFAULT_MAX_BYTES and writer_queue is None:
                ensure_full_output("".join(output_chunks) + text)
            else:
                append_full_output(text)
            output_chunks.append(text)
            output_code_units += js_utf16_length(text)
            while output_code_units > max_output_code_units and len(output_chunks) > 1:
                output_code_units -= js_utf16_length(output_chunks.pop(0))
            if on_chunk_callback:
                on_chunk_callback(text)
        except BaseException as error:
            capture_error = _to_execution_error(error)

    exec_options = {**options, "onStdout": on_chunk, "onStderr": on_chunk}
    try:
        method = env.exec
        result = await maybe_await(method(command, exec_options))
        tail_output = "".join(output_chunks)
        truncation = truncate_tail(tail_output)
        if truncation["truncated"] and writer_queue is None:
            ensure_full_output(tail_output)
        if writer_queue is not None:
            writer_queue.put_nowait(writer_end)
        if writer_task is not None:
            writer_error = await writer_task
            if writer_error is not None:
                return err(writer_error)
        if capture_error:
            return err(capture_error)
        output = truncation["content"] if truncation["truncated"] else tail_output
        truncated = truncation["truncated"] or full_output_path is not None
        if not result["ok"]:
            if result["error"].code == "aborted" or (signal and signal.aborted):
                return ok(
                    {
                        "output": output,
                        "exitCode": None,
                        "cancelled": True,
                        "truncated": truncated,
                        "fullOutputPath": full_output_path,
                    }
                )
            error = result["error"]
            message = (
                f"Command timed out after {options.get('timeout')} seconds" if error.code == "timeout" else str(error)
            )
            if truncated:
                output += f"\n\n[Output truncated. Full output: {full_output_path}]"
            return err(ExecutionError(error.code, f"{output}\n\n{message}" if output else message, error))
        cancelled = bool(signal and signal.aborted)
        return ok(
            {
                "output": output,
                "exitCode": None if cancelled else result["value"]["exitCode"],
                "cancelled": cancelled,
                "truncated": truncated,
                "fullOutputPath": full_output_path,
            }
        )
    except BaseException as error:
        return err(_to_execution_error(error))


sanitizeBinaryOutput = sanitize_binary_output
executeShellWithCapture = execute_shell_with_capture
