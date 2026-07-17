from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ..._utils import maybe_await
from ..types import FileError, Result, SessionError, SessionTreeEntry
from .uuid import uuidv7

if TYPE_CHECKING:
    from .session import Session


def create_session_id() -> str:
    return uuidv7()


def create_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_session(storage: Any) -> Session:
    from .session import Session

    return Session(storage)


def get_file_system_result_or_throw(result: Result[Any, FileError], message: str) -> Any:
    if not result["ok"]:
        error = result["error"]
        code = "not_found" if error.code == "not_found" else "storage"
        raise SessionError(code, f"{message}: {error}", error)
    return result["value"]


async def fs_call(fs: Any, snake_name: str, camel_name: str, *args: Any) -> Any:
    method = getattr(fs, snake_name, None) or getattr(fs, camel_name, None)
    if not callable(method):
        raise AttributeError(f"Filesystem does not implement {snake_name}/{camel_name}")
    return await maybe_await(method(*args))


async def get_entries_to_fork(storage: Any, options: dict[str, Any]) -> list[SessionTreeEntry]:
    entry_id = options.get("entryId", options.get("entry_id"))
    if not entry_id:
        return cast(list[SessionTreeEntry], await storage.get_entries())
    target = await storage.get_entry(entry_id)
    if target is None:
        raise SessionError("invalid_fork_target", f"Entry {entry_id} not found")
    if options.get("position", "before") == "at":
        leaf_id = target["id"]
    else:
        if target.get("type") != "message" or target.get("message", {}).get("role") != "user":
            raise SessionError("invalid_fork_target", f"Entry {entry_id} is not a user message")
        leaf_id = target.get("parentId")
    return cast(list[SessionTreeEntry], await storage.get_path_to_root(leaf_id))


createSessionId = create_session_id
createTimestamp = create_timestamp
toSession = to_session
getFileSystemResultOrThrow = get_file_system_result_or_throw
getEntriesToFork = get_entries_to_fork
