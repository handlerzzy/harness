from __future__ import annotations

import asyncio
from typing import Any

from .abort import AbortSignal
from .harness.types import ExecutionEnv, get_or_throw
from .harness.utils.shell_output import execute_shell_with_capture
from .harness.utils.truncate import truncate_head
from .types import AgentTool, AgentToolResult, AgentToolUpdateCallback, ToolExecutor


def _result(text: str, **details: Any) -> AgentToolResult:
    return {"content": [{"type": "text", "text": text}], "details": details}


def create_coding_tools(env: ExecutionEnv) -> list[AgentTool]:
    """Create text read/write/edit and bash tools using the supplied execution environment."""
    mutation_lock = asyncio.Lock()

    async def read(
        _call_id: str, args: Any, signal: AbortSignal | None, _on_update: AgentToolUpdateCallback | None
    ) -> AgentToolResult:
        text = get_or_throw(await env.read_text_file(args["path"], signal))
        truncated = truncate_head(text)
        if truncated["truncated"]:
            path = get_or_throw(await env.absolute_path(args["path"], signal))
            return _result(
                truncated["content"] + f"\n\n[Output truncated. Full output: {path}]",
                truncated=True,
                fullOutputPath=path,
            )
        return _result(text)

    async def write(
        _call_id: str, args: Any, signal: AbortSignal | None, _on_update: AgentToolUpdateCallback | None
    ) -> AgentToolResult:
        async with mutation_lock:
            get_or_throw(await env.write_file(args["path"], args["content"], signal))
        return _result(f"Successfully wrote {len(args['content'].encode('utf-8'))} bytes to {args['path']}.")

    async def edit(
        _call_id: str, args: Any, signal: AbortSignal | None, _on_update: AgentToolUpdateCallback | None
    ) -> AgentToolResult:
        async with mutation_lock:
            # Use bytes so untouched CRLF line endings and UTF-8 BOM survive the edit.
            original = get_or_throw(await env.read_binary_file(args["path"], signal)).decode("utf-8")
            old = args["oldText"]
            start = original.find(old)
            if start < 0:
                raise ValueError(f"Could not find the exact text in {args['path']}.")
            if original.find(old, start + 1) >= 0:
                raise ValueError(f"Ambiguous match in {args['path']}: oldText must match exactly once.")
            updated = original[:start] + args["newText"] + original[start + len(old) :]
            get_or_throw(await env.write_file(args["path"], updated.encode("utf-8"), signal))
        return _result(f"Successfully replaced text in {args['path']}.")

    async def bash(
        _call_id: str, args: Any, signal: AbortSignal | None, _on_update: AgentToolUpdateCallback | None
    ) -> AgentToolResult:
        captured = get_or_throw(
            await execute_shell_with_capture(
                env, args["command"], {"timeout": args.get("timeout"), "abortSignal": signal, "signalExitCode": True}
            )
        )
        text = captured["output"] or "(no output)"
        if captured["truncated"]:
            text += f"\n\n[Output truncated. Full output: {captured['fullOutputPath']}]"
        if captured["cancelled"]:
            raise RuntimeError(text + "\n\nCommand aborted")
        text += f"\n\nExit code: {captured['exitCode']}"
        if captured["exitCode"] != 0:
            raise RuntimeError(text)
        return _result(text, **captured)

    path = {"type": "string", "minLength": 1, "description": "File path, relative to cwd or absolute."}

    def tool(
        name: str, description: str, properties: dict[str, Any], required: list[str], execute: ToolExecutor
    ) -> AgentTool:
        return AgentTool(
            name=name,
            label=name,
            description=description,
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            execute=execute,
            execution_mode="sequential",
        )

    return [
        tool(
            "read",
            "Read a UTF-8 text file. Long output is truncated with the full file path.",
            {"path": path},
            ["path"],
            read,
        ),
        tool(
            "write",
            "Create or overwrite a UTF-8 file, creating parent directories.",
            {"path": path, "content": {"type": "string"}},
            ["path", "content"],
            write,
        ),
        tool(
            "edit",
            "Replace exactly one occurrence of oldText. Missing or ambiguous matches fail without writing.",
            {"path": path, "oldText": {"type": "string", "minLength": 1}, "newText": {"type": "string"}},
            ["path", "oldText", "newText"],
            edit,
        ),
        tool(
            "bash",
            "Run a shell command in cwd. Returns stdout/stderr and exit code. Long output has a full log path. Optional timeout is in seconds.",
            {"command": {"type": "string", "minLength": 1}, "timeout": {"type": "number", "exclusiveMinimum": 0}},
            ["command"],
            bash,
        ),
    ]
