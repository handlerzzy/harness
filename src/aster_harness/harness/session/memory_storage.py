from __future__ import annotations

from typing import Any

from ..types import SessionError, SessionMetadata, SessionTreeEntry
from .repo_utils import create_timestamp
from .uuid import uuidv7


def _update_label_cache(labels: dict[str, str], entry: SessionTreeEntry) -> None:
    if entry.get("type") != "label":
        return
    label = str(entry.get("label") or "").strip()
    if label:
        labels[entry["targetId"]] = label
    else:
        labels.pop(entry["targetId"], None)


def _leaf_id_after_entry(entry: SessionTreeEntry) -> str | None:
    return entry.get("targetId") if entry.get("type") == "leaf" else entry["id"]


def _generate_entry_id(by_id: dict[str, SessionTreeEntry]) -> str:
    for _ in range(100):
        entry_id = uuidv7()[-8:]
        if entry_id not in by_id:
            return entry_id
    return uuidv7()


class InMemorySessionStorage:
    def __init__(
        self,
        options: dict[str, Any] | None = None,
        *,
        entries: list[SessionTreeEntry] | None = None,
        metadata: SessionMetadata | None = None,
    ) -> None:
        options = options or {}
        source_entries = entries if entries is not None else options.get("entries", [])
        self._entries = list(source_entries)
        self._by_id = {entry["id"]: entry for entry in self._entries}
        self._labels: dict[str, str] = {}
        self._leaf_id: str | None = None
        for entry in self._entries:
            _update_label_cache(self._labels, entry)
            self._leaf_id = _leaf_id_after_entry(entry)
        if self._leaf_id is not None and self._leaf_id not in self._by_id:
            raise SessionError("invalid_session", f"Entry {self._leaf_id} not found")
        selected_metadata = metadata if metadata is not None else options.get("metadata")
        self._metadata = (
            selected_metadata if selected_metadata is not None else {"id": uuidv7(), "createdAt": create_timestamp()}
        )

    async def get_metadata(self) -> SessionMetadata:
        return self._metadata

    async def get_leaf_id(self) -> str | None:
        if self._leaf_id is not None and self._leaf_id not in self._by_id:
            raise SessionError("invalid_session", f"Entry {self._leaf_id} not found")
        return self._leaf_id

    async def set_leaf_id(self, leaf_id: str | None) -> None:
        if leaf_id is not None and leaf_id not in self._by_id:
            raise SessionError("not_found", f"Entry {leaf_id} not found")
        entry = {
            "type": "leaf",
            "id": _generate_entry_id(self._by_id),
            "parentId": self._leaf_id,
            "timestamp": create_timestamp(),
            "targetId": leaf_id,
        }
        self._entries.append(entry)
        self._by_id[entry["id"]] = entry
        self._leaf_id = leaf_id

    async def create_entry_id(self) -> str:
        return _generate_entry_id(self._by_id)

    async def append_entry(self, entry: SessionTreeEntry) -> None:
        self._entries.append(entry)
        self._by_id[entry["id"]] = entry
        _update_label_cache(self._labels, entry)
        self._leaf_id = _leaf_id_after_entry(entry)

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
