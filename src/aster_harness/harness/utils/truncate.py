from __future__ import annotations

from typing import Any, TypeAlias

from ..._utils import js_to_fixed, js_utf16_length, js_utf16_slice

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500

TruncationResult: TypeAlias = dict[str, Any]
TruncationOptions: TypeAlias = dict[str, Any]


def _char_bytes(character: str) -> int:
    code = ord(character)
    if code <= 0x7F:
        return 1
    if code <= 0x7FF:
        return 2
    if 0xD800 <= code <= 0xDFFF:
        return 3
    if code <= 0xFFFF:
        return 3
    return 4


def utf8_byte_length(content: str) -> int:
    total = 0
    index = 0
    while index < len(content):
        code = ord(content[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < len(content) and 0xDC00 <= ord(content[index + 1]) <= 0xDFFF:
            total += 4
            index += 2
        else:
            total += _char_bytes(content[index])
            index += 1
    return total


def _base_result(
    content: str,
    truncated: bool,
    truncated_by: str | None,
    total_lines: int,
    total_bytes: int,
    output_lines: int,
    last_line_partial: bool,
    first_line_exceeds_limit: bool,
    max_lines: int,
    max_bytes: int,
) -> dict[str, Any]:
    return {
        "content": content,
        "truncated": truncated,
        "truncatedBy": truncated_by,
        "totalLines": total_lines,
        "totalBytes": total_bytes,
        "outputLines": output_lines,
        "outputBytes": utf8_byte_length(content),
        "lastLinePartial": last_line_partial,
        "firstLineExceedsLimit": first_line_exceeds_limit,
        "maxLines": max_lines,
        "maxBytes": max_bytes,
    }


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{js_to_fixed(size / 1024, 1)}KB"
    return f"{js_to_fixed(size / (1024 * 1024), 1)}MB"


def truncate_head(content: str, options: dict[str, int] | None = None) -> dict[str, Any]:
    options = options or {}
    max_lines = options.get("maxLines", options.get("max_lines", DEFAULT_MAX_LINES))
    max_bytes = options.get("maxBytes", options.get("max_bytes", DEFAULT_MAX_BYTES))
    total_bytes = utf8_byte_length(content)
    lines = content.split("\n")
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return _base_result(
            content, False, None, total_lines, total_bytes, total_lines, False, False, max_lines, max_bytes
        )
    if utf8_byte_length(lines[0]) > max_bytes:
        return _base_result("", True, "bytes", total_lines, total_bytes, 0, False, True, max_lines, max_bytes)
    output: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    for index, line in enumerate(lines[: max(0, max_lines)]):
        line_bytes = utf8_byte_length(line) + (1 if index > 0 else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output.append(line)
        output_bytes += line_bytes
    if len(output) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"
    result = "\n".join(output)
    return _base_result(
        result, True, truncated_by, total_lines, total_bytes, len(output), False, False, max_lines, max_bytes
    )


def _replace_unpaired_surrogates(content: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(content):
        code = ord(content[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < len(content) and 0xDC00 <= ord(content[index + 1]) <= 0xDFFF:
            output.extend((content[index], content[index + 1]))
            index += 2
        elif 0xD800 <= code <= 0xDFFF:
            output.append("\ufffd")
            index += 1
        else:
            output.append(content[index])
            index += 1
    return "".join(output)


def _truncate_string_to_bytes_from_end(content: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    output_bytes = 0
    start = len(content)
    needs_replacement = False
    index = len(content)
    while index > 0:
        character_start = index - 1
        code = ord(content[character_start])
        if 0xDC00 <= code <= 0xDFFF and character_start > 0 and 0xD800 <= ord(content[character_start - 1]) <= 0xDBFF:
            character_start -= 1
            character_bytes = 4
        else:
            character_bytes = _char_bytes(content[character_start])
            needs_replacement = needs_replacement or 0xD800 <= code <= 0xDFFF
        if output_bytes + character_bytes > max_bytes:
            break
        output_bytes += character_bytes
        start = character_start
        index = character_start
    output = content[start:]
    return _replace_unpaired_surrogates(output) if needs_replacement else output


def truncate_tail(content: str, options: dict[str, int] | None = None) -> dict[str, Any]:
    options = options or {}
    max_lines = options.get("maxLines", options.get("max_lines", DEFAULT_MAX_LINES))
    max_bytes = options.get("maxBytes", options.get("max_bytes", DEFAULT_MAX_BYTES))
    total_bytes = utf8_byte_length(content)
    lines = content.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return _base_result(
            content, False, None, total_lines, total_bytes, total_lines, False, False, max_lines, max_bytes
        )
    output: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    last_line_partial = False
    for line in reversed(lines):
        if len(output) >= max_lines:
            break
        line_bytes = utf8_byte_length(line) + (1 if output else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not output:
                truncated = _truncate_string_to_bytes_from_end(line, max_bytes)
                output.insert(0, truncated)
                output_bytes = utf8_byte_length(truncated)
                last_line_partial = True
            break
        output.insert(0, line)
        output_bytes += line_bytes
    if len(output) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"
    result = "\n".join(output)
    return _base_result(
        result,
        True,
        truncated_by,
        total_lines,
        total_bytes,
        len(output),
        last_line_partial,
        False,
        max_lines,
        max_bytes,
    )


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> dict[str, Any]:
    if js_utf16_length(line) <= max_chars:
        return {"text": line, "wasTruncated": False}
    return {"text": f"{js_utf16_slice(line, 0, max_chars)}... [truncated]", "wasTruncated": True}


formatSize = format_size
truncateHead = truncate_head
truncateTail = truncate_tail
truncateLine = truncate_line
