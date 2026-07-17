from __future__ import annotations

from typing import Any, cast

from ..types import SessionError, SessionMetadata
from .memory_storage import InMemorySessionStorage
from .repo_utils import create_session_id, create_timestamp, get_entries_to_fork, to_session
from .session import Session


class InMemorySessionRepo:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def create(self, options: dict[str, Any] | None = None) -> Session:
        options = options or {}
        session_id = cast(str, options.get("id") if options.get("id") is not None else create_session_id())
        metadata = {"id": session_id, "createdAt": create_timestamp()}
        session = to_session(InMemorySessionStorage(metadata=metadata))
        self._sessions[metadata["id"]] = session
        return session

    async def open(self, metadata: SessionMetadata) -> Session:
        session = self._sessions.get(metadata["id"])
        if session is None:
            raise SessionError("not_found", f"Session not found: {metadata['id']}")
        return session

    async def list(self, _options: object = None) -> list[SessionMetadata]:
        return [await session.get_metadata() for session in self._sessions.values()]

    async def delete(self, metadata: SessionMetadata) -> None:
        self._sessions.pop(metadata["id"], None)

    async def fork(self, source_metadata: SessionMetadata, options: dict[str, Any]) -> Session:
        source = await self.open(source_metadata)
        entries = await get_entries_to_fork(source.get_storage(), options)
        session_id = cast(str, options.get("id") if options.get("id") is not None else create_session_id())
        metadata = {"id": session_id, "createdAt": create_timestamp()}
        session = to_session(InMemorySessionStorage(entries=entries, metadata=metadata))
        self._sessions[metadata["id"]] = session
        return session
