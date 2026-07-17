from __future__ import annotations

import builtins
import re
from functools import cmp_to_key
from typing import Any, cast

from ..._utils import js_date_parse_ms
from ..types import JsonlSessionMetadata, SessionError
from .jsonl_storage import JsonlSessionStorage, load_jsonl_session_metadata
from .repo_utils import (
    create_session_id,
    create_timestamp,
    fs_call,
    get_entries_to_fork,
    get_file_system_result_or_throw,
    to_session,
)
from .session import Session


def _encode_cwd(cwd: str) -> str:
    stripped = re.sub(r"^[/\\]", "", cwd)
    encoded = re.sub(r"[/\\:]", "-", stripped)
    return f"--{encoded}--"


def _compare_created_at_desc(left: JsonlSessionMetadata, right: JsonlSessionMetadata) -> int:
    # Array.sort treats a NaN comparator result as zero. Using a comparator
    # instead of an ISO-string key also preserves Date ordering for timestamps
    # with different UTC offsets.
    difference = js_date_parse_ms(str(right["createdAt"])) - js_date_parse_ms(str(left["createdAt"]))
    if difference != difference or difference == 0:
        return 0
    return -1 if difference < 0 else 1


class JsonlSessionRepo:
    def __init__(
        self,
        options: dict[str, Any] | None = None,
        *,
        fs: Any = None,
        sessions_root: str | None = None,
    ) -> None:
        options = options or {}
        self._fs = fs if fs is not None else options["fs"]
        self._sessions_root_input = (
            sessions_root if sessions_root is not None else options.get("sessionsRoot", options.get("sessions_root"))
        )
        if self._sessions_root_input is None:
            raise ValueError("sessions_root is required")
        self._sessions_root: str | None = None

    async def _get_sessions_root(self) -> str:
        if self._sessions_root is None:
            self._sessions_root = get_file_system_result_or_throw(
                await fs_call(self._fs, "absolute_path", "absolutePath", self._sessions_root_input),
                f"Failed to resolve sessions root {self._sessions_root_input}",
            )
        return self._sessions_root

    async def _get_session_dir(self, cwd: str) -> str:
        return str(
            get_file_system_result_or_throw(
                await fs_call(self._fs, "join_path", "joinPath", [await self._get_sessions_root(), _encode_cwd(cwd)]),
                f"Failed to resolve session directory for {cwd}",
            )
        )

    async def _create_session_file_path(self, cwd: str, session_id: str, timestamp: str) -> str:
        filename = f"{re.sub(r'[:.]', '-', timestamp)}_{session_id}.jsonl"
        return str(
            get_file_system_result_or_throw(
                await fs_call(self._fs, "join_path", "joinPath", [await self._get_session_dir(cwd), filename]),
                f"Failed to resolve session file path for {session_id}",
            )
        )

    async def create(self, options: dict[str, Any]) -> Session:
        session_id = cast(str, options.get("id") if options.get("id") is not None else create_session_id())
        created_at = create_timestamp()
        session_dir = await self._get_session_dir(options["cwd"])
        get_file_system_result_or_throw(
            await fs_call(self._fs, "create_dir", "createDir", session_dir, {"recursive": True}),
            f"Failed to create session directory {session_dir}",
        )
        file_path = await self._create_session_file_path(options["cwd"], session_id, created_at)
        storage = await JsonlSessionStorage.create(
            self._fs,
            file_path,
            {
                "cwd": options["cwd"],
                "sessionId": session_id,
                "parentSessionPath": options.get("parentSessionPath", options.get("parent_session_path")),
                "metadata": options.get("metadata"),
            },
        )
        return to_session(storage)

    async def open(self, metadata: JsonlSessionMetadata) -> Session:
        exists = get_file_system_result_or_throw(
            await fs_call(self._fs, "exists", "exists", metadata["path"]),
            f"Failed to check session {metadata['path']}",
        )
        if not exists:
            raise SessionError("not_found", f"Session not found: {metadata['path']}")
        return to_session(await JsonlSessionStorage.open(self._fs, metadata["path"]))

    async def list(self, options: dict[str, Any] | None = None) -> list[JsonlSessionMetadata]:
        options = options or {}
        directories = (
            [await self._get_session_dir(options["cwd"])] if options.get("cwd") else await self._list_session_dirs()
        )
        sessions: builtins.list[JsonlSessionMetadata] = []
        for directory in directories:
            if not get_file_system_result_or_throw(
                await fs_call(self._fs, "exists", "exists", directory),
                f"Failed to check session directory {directory}",
            ):
                continue
            files = get_file_system_result_or_throw(
                await fs_call(self._fs, "list_dir", "listDir", directory), f"Failed to list sessions in {directory}"
            )
            for file in files:
                if file.get("kind") == "directory" or not str(file.get("name", "")).endswith(".jsonl"):
                    continue
                try:
                    sessions.append(await load_jsonl_session_metadata(self._fs, file["path"]))
                except SessionError as error:
                    if error.code != "invalid_session":
                        raise
        sessions.sort(key=cmp_to_key(_compare_created_at_desc))
        return sessions

    async def delete(self, metadata: JsonlSessionMetadata) -> None:
        get_file_system_result_or_throw(
            await fs_call(self._fs, "remove", "remove", metadata["path"], {"force": True}),
            f"Failed to delete session {metadata['path']}",
        )

    async def fork(self, source_metadata: JsonlSessionMetadata, options: dict[str, Any]) -> Session:
        source = await self.open(source_metadata)
        entries = await get_entries_to_fork(source.get_storage(), options)
        session_id = cast(str, options.get("id") if options.get("id") is not None else create_session_id())
        created_at = create_timestamp()
        session_dir = await self._get_session_dir(options["cwd"])
        get_file_system_result_or_throw(
            await fs_call(self._fs, "create_dir", "createDir", session_dir, {"recursive": True}),
            f"Failed to create session directory {session_dir}",
        )
        storage = await JsonlSessionStorage.create(
            self._fs,
            await self._create_session_file_path(options["cwd"], session_id, created_at),
            {
                "cwd": options["cwd"],
                "sessionId": session_id,
                "parentSessionPath": (
                    options.get("parentSessionPath", options.get("parent_session_path"))
                    if options.get("parentSessionPath", options.get("parent_session_path")) is not None
                    else source_metadata["path"]
                ),
                "metadata": options.get("metadata")
                if options.get("metadata") is not None
                else source_metadata.get("metadata"),
            },
        )
        for entry in entries:
            await storage.append_entry(entry)
        return to_session(storage)

    async def _list_session_dirs(self) -> builtins.list[str]:
        root = await self._get_sessions_root()
        if not get_file_system_result_or_throw(
            await fs_call(self._fs, "exists", "exists", root), f"Failed to check sessions root {root}"
        ):
            return []
        entries = get_file_system_result_or_throw(
            await fs_call(self._fs, "list_dir", "listDir", root), f"Failed to list sessions root {root}"
        )
        return [
            str(entry["path"])
            for entry in cast(builtins.list[dict[str, Any]], entries)
            if entry.get("kind") == "directory"
        ]
