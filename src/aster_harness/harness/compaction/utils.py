from __future__ import annotations

from typing import Any

from ..._utils import js_json_stringify, js_object_items, js_utf16_length, js_utf16_slice, js_utf16_sort_key
from ...types import AgentMessage

FileOperations = dict[str, set[str]]
TOOL_RESULT_MAX_CHARS = 2000


def _safe_json_stringify(value: Any) -> str:
    try:
        return js_json_stringify(value) or "undefined"
    except (TypeError, ValueError, RecursionError):
        return "[unserializable]"


def create_file_ops() -> FileOperations:
    return {"read": set(), "written": set(), "edited": set()}


def extract_file_ops_from_message(message: AgentMessage, file_ops: FileOperations) -> None:
    if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
        return
    for block in message["content"]:
        if not isinstance(block, dict) or block.get("type") != "toolCall":
            continue
        arguments = block.get("arguments")
        if not isinstance(arguments, dict):
            continue
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            continue
        name = block.get("name")
        if name == "read":
            file_ops["read"].add(path)
        elif name == "write":
            file_ops["written"].add(path)
        elif name == "edit":
            file_ops["edited"].add(path)


def compute_file_lists(file_ops: FileOperations) -> dict[str, list[str]]:
    modified = file_ops["edited"] | file_ops["written"]
    return {
        "readFiles": sorted(
            (path for path in file_ops["read"] if path not in modified),
            key=js_utf16_sort_key,
        ),
        "modifiedFiles": sorted(modified, key=js_utf16_sort_key),
    }


def format_file_operations(read_files: list[str], modified_files: list[str]) -> str:
    sections: list[str] = []
    if read_files:
        sections.append("<read-files>\n" + "\n".join(read_files) + "\n</read-files>")
    if modified_files:
        sections.append("<modified-files>\n" + "\n".join(modified_files) + "\n</modified-files>")
    return "\n\n" + "\n\n".join(sections) if sections else ""


def _truncate_for_summary(text: str, max_chars: int) -> str:
    length = js_utf16_length(text)
    if length <= max_chars:
        return text
    return f"{js_utf16_slice(text, 0, max_chars)}\n\n[... {length - max_chars} more characters truncated]"


def serialize_conversation(messages: list[AgentMessage]) -> str:
    parts: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            raw_content = message.get("content", "")
            if isinstance(raw_content, str):
                content = raw_content
            else:
                content = "".join(
                    str(block.get("text", ""))
                    for block in raw_content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            if content:
                parts.append(f"[User]: {content}")
        elif role == "assistant":
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls: list[str] = []
            for block in message.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block.get("type") == "thinking":
                    thinking_parts.append(str(block.get("thinking", "")))
                elif block.get("type") == "toolCall":
                    arguments = block.get("arguments")
                    if not isinstance(arguments, dict):
                        arguments = {}
                    args_text = ", ".join(
                        f"{key}={_safe_json_stringify(value)}" for key, value in js_object_items(arguments)
                    )
                    tool_calls.append(f"{block.get('name', '')}({args_text})")
            if thinking_parts:
                parts.append("[Assistant thinking]: " + "\n".join(thinking_parts))
            if text_parts:
                parts.append("[Assistant]: " + "\n".join(text_parts))
            if tool_calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(tool_calls)}")
        elif role == "toolResult":
            raw_content = message.get("content", [])
            content = "".join(
                str(block.get("text", ""))
                for block in raw_content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if content:
                parts.append(f"[Tool result]: {_truncate_for_summary(content, TOOL_RESULT_MAX_CHARS)}")
    return "\n\n".join(parts)


createFileOps = create_file_ops
extractFileOpsFromMessage = extract_file_ops_from_message
computeFileLists = compute_file_lists
formatFileOperations = format_file_operations
serializeConversation = serialize_conversation
