from __future__ import annotations

import json
from typing import Any

from ..._utils import js_json_stringify, js_parse_json_number
from ..types import JsonlSessionMetadata, SessionError, SessionTreeEntry, to_error
from .memory_storage import _generate_entry_id, _leaf_id_after_entry, _update_label_cache
from .repo_utils import create_timestamp, fs_call, get_file_system_result_or_throw


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Invalid JSON constant: {value}")


def _json_loads(value: str) -> Any:
    return json.loads(
        value,
        parse_int=js_parse_json_number,
        parse_float=js_parse_json_number,
        parse_constant=_reject_json_constant,
    )


def _json_dumps(value: Any) -> str:
    encoded = js_json_stringify(value)
    if encoded is None:
        raise TypeError("Value is not JSON serializable")
    return encoded


def _invalid_session(file_path: str, message: str, cause: BaseException | None = None) -> SessionError:
    return SessionError("invalid_session", f"Invalid JSONL session file {file_path}: {message}", cause)


def _invalid_entry(file_path: str, line_number: int, message: str, cause: BaseException | None = None) -> SessionError:
    return SessionError("invalid_entry", f"Invalid JSONL session file {file_path}: line {line_number} {message}", cause)


def _parse_header_line(line: str, file_path: str) -> dict[str, Any]:
    try:
        parsed = _json_loads(line)
    except Exception as error:
        raise _invalid_session(file_path, "first line is not a valid session header", to_error(error)) from error
    if not isinstance(parsed, dict) or parsed.get("type") != "session":
        raise _invalid_session(file_path, "first line is not a valid session header")
    if parsed.get("version") != 3:
        raise _invalid_session(file_path, "unsupported session version")
    if not isinstance(parsed.get("id"), str) or not parsed["id"]:
        raise _invalid_session(file_path, "session header is missing id")
    if not isinstance(parsed.get("timestamp"), str) or not parsed["timestamp"]:
        raise _invalid_session(file_path, "session header is missing timestamp")
    if not isinstance(parsed.get("cwd"), str) or not parsed["cwd"]:
        raise _invalid_session(file_path, "session header is missing cwd")
    if "parentSession" in parsed and not isinstance(parsed["parentSession"], str):
        raise _invalid_session(file_path, "session header parentSession must be a string")
    if "metadata" in parsed and not isinstance(parsed["metadata"], dict):
        raise _invalid_session(file_path, "session header metadata must be an object")
    return parsed


def _parse_entry_line(line: str, file_path: str, line_number: int) -> SessionTreeEntry:
    try:
        parsed = _json_loads(line)
    except Exception as error:
        raise _invalid_entry(file_path, line_number, "is not valid JSON", to_error(error)) from error
    if not isinstance(parsed, dict):
        raise _invalid_entry(file_path, line_number, "is not a valid session entry")
    if not isinstance(parsed.get("type"), str):
        raise _invalid_entry(file_path, line_number, "is missing entry type")
    if not isinstance(parsed.get("id"), str) or not parsed["id"]:
        raise _invalid_entry(file_path, line_number, "is missing entry id")
    if "parentId" not in parsed or (parsed["parentId"] is not None and not isinstance(parsed["parentId"], str)):
        raise _invalid_entry(file_path, line_number, "has invalid parentId")
    if not isinstance(parsed.get("timestamp"), str) or not parsed["timestamp"]:
        raise _invalid_entry(file_path, line_number, "is missing timestamp")
    if parsed["type"] == "leaf" and (
        "targetId" not in parsed or (parsed["targetId"] is not None and not isinstance(parsed["targetId"], str))
    ):
        raise _invalid_entry(file_path, line_number, "has invalid targetId")
    return parsed


def _header_to_metadata(header: dict[str, Any], path: str) -> JsonlSessionMetadata:
    metadata: JsonlSessionMetadata = {
        "id": header["id"],
        "createdAt": header["timestamp"],
        "cwd": header["cwd"],
        "path": path,
        "parentSessionPath": header.get("parentSession"),
        "metadata": header.get("metadata"),
    }
    return metadata


async def load_jsonl_session_metadata(fs: Any, file_path: str) -> JsonlSessionMetadata:
    lines = get_file_system_result_or_throw(
        await fs_call(fs, "read_text_lines", "readTextLines", file_path, {"maxLines": 1}),
        f"Failed to read session header {file_path}",
    )
    if lines and str(lines[0]).strip():
        return _header_to_metadata(_parse_header_line(lines[0], file_path), file_path)
    raise _invalid_session(file_path, "missing session header")


async def _load_jsonl_storage(fs: Any, file_path: str) -> tuple[dict[str, Any], list[SessionTreeEntry], str | None]:
    content = get_file_system_result_or_throw(
        await fs_call(fs, "read_text_file", "readTextFile", file_path), f"Failed to read session {file_path}"
    )
    lines = [line for line in content.split("\n") if line.strip()]
    if not lines:
        raise _invalid_session(file_path, "missing session header")
    header = _parse_header_line(lines[0], file_path)
    entries: list[SessionTreeEntry] = []
    leaf_id: str | None = None
    for line_number, line in enumerate(lines[1:], 2):
        entry = _parse_entry_line(line, file_path, line_number)
        entries.append(entry)
        leaf_id = _leaf_id_after_entry(entry)
    return header, entries, leaf_id


class JsonlSessionStorage:
    def __init__(
        self,
        fs: Any,
        file_path: str,
        header: dict[str, Any],
        entries: list[SessionTreeEntry],
        leaf_id: str | None,
    ) -> None:
        self._fs = fs
        self._file_path = file_path
        self._metadata = _header_to_metadata(header, file_path)
        self._entries = entries
        self._by_id = {entry["id"]: entry for entry in entries}
        self._labels: dict[str, str] = {}
        for entry in entries:
            _update_label_cache(self._labels, entry)
        self._current_leaf_id = leaf_id

    @classmethod
    async def open(cls, fs: Any, file_path: str) -> JsonlSessionStorage:
        header, entries, leaf_id = await _load_jsonl_storage(fs, file_path)
        return cls(fs, file_path, header, entries, leaf_id)

    @classmethod
    async def create(cls, fs: Any, file_path: str, options: dict[str, Any]) -> JsonlSessionStorage:
        header = {
            "type": "session",
            "version": 3,
            "id": options.get("sessionId", options.get("session_id")),
            "timestamp": create_timestamp(),
            "cwd": options["cwd"],
        }
        parent = options.get("parentSessionPath", options.get("parent_session_path"))
        if parent is not None:
            header["parentSession"] = parent
        if options.get("metadata") is not None:
            header["metadata"] = options["metadata"]
        get_file_system_result_or_throw(
            await fs_call(
                fs,
                "write_file",
                "writeFile",
                file_path,
                _json_dumps(header) + "\n",
            ),
            f"Failed to create session {file_path}",
        )
        return cls(fs, file_path, header, [], None)

    async def get_metadata(self) -> JsonlSessionMetadata:
        return self._metadata

    async def get_leaf_id(self) -> str | None:
        if self._current_leaf_id is not None and self._current_leaf_id not in self._by_id:
            raise SessionError("invalid_session", f"Entry {self._current_leaf_id} not found")
        return self._current_leaf_id

    async def _append_json(self, entry: SessionTreeEntry, description: str) -> None:
        get_file_system_result_or_throw(
            await fs_call(
                self._fs,
                "append_file",
                "appendFile",
                self._file_path,
                _json_dumps(entry) + "\n",
            ),
            description,
        )

    async def set_leaf_id(self, leaf_id: str | None) -> None:
        if leaf_id is not None and leaf_id not in self._by_id:
            raise SessionError("not_found", f"Entry {leaf_id} not found")
        entry = {
            "type": "leaf",
            "id": _generate_entry_id(self._by_id),
            "parentId": self._current_leaf_id,
            "timestamp": create_timestamp(),
            "targetId": leaf_id,
        }
        await self._append_json(entry, f"Failed to append session leaf {entry['id']}")
        self._entries.append(entry)
        self._by_id[entry["id"]] = entry
        self._current_leaf_id = leaf_id

    async def create_entry_id(self) -> str:
        return _generate_entry_id(self._by_id)

    async def append_entry(self, entry: SessionTreeEntry) -> None:
        await self._append_json(entry, f"Failed to append session entry {entry['id']}")
        self._entries.append(entry)
        self._by_id[entry["id"]] = entry
        _update_label_cache(self._labels, entry)
        self._current_leaf_id = _leaf_id_after_entry(entry)

    async def get_entry(self, entry_id: str) -> SessionTreeEntry | None:
        return self._by_id.get(entry_id)

    async def find_entries(self, entry_type: str) -> list[SessionTreeEntry]:
        return [entry for entry in self._entries if entry.get("type") == entry_type]

    async def get_label(self, entry_id: str) -> str | None:
        return self._labels.get(entry_id)

    async def get_path_to_root(self, leaf_id: str | None) -> list[SessionTreeEntry]:
        if leaf_id is None:
            return []
        current = self._by_id.get(leaf_id)
        if current is None:
            raise SessionError("not_found", f"Entry {leaf_id} not found")
        path: list[SessionTreeEntry] = []
        while current is not None:
            path.insert(0, current)
            parent_id = current.get("parentId")
            if not parent_id:
                break
            current = self._by_id.get(parent_id)
            if current is None:
                raise SessionError("invalid_session", f"Entry {parent_id} not found")
        return path

    async def get_entries(self) -> list[SessionTreeEntry]:
        return list(self._entries)

    getMetadata = get_metadata
    getLeafId = get_leaf_id
    setLeafId = set_leaf_id
    createEntryId = create_entry_id
    appendEntry = append_entry
    getEntry = get_entry
    findEntries = find_entries
    getLabel = get_label
    getPathToRoot = get_path_to_root
    getEntries = get_entries


loadJsonlSessionMetadata = load_jsonl_session_metadata
