from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeAlias

from ...types import AgentMessage
from ..messages import create_branch_summary_message, create_compaction_summary_message, create_custom_message
from ..types import SessionError, SessionMetadata, SessionStorage, SessionTreeEntry
from .repo_utils import create_timestamp

ContextEntryTransform = Callable[[Sequence[SessionTreeEntry]], Sequence[SessionTreeEntry]]
CustomEntryContextMessageProjector = Callable[
    [SessionTreeEntry, int, Sequence[SessionTreeEntry]], Sequence[AgentMessage] | None
]
SessionContextBuildOptions: TypeAlias = dict[str, Any]


def _option(options: Mapping[str, Any], snake: str, camel: str, default: Any) -> Any:
    return options.get(snake, options.get(camel, default))


def _derive_session_context_state(path_entries: Sequence[SessionTreeEntry]) -> dict[str, Any]:
    thinking_level = "off"
    model: dict[str, str] | None = None
    active_tool_names: list[str] | None = None
    for entry in path_entries:
        entry_type = entry.get("type")
        if entry_type == "thinking_level_change":
            thinking_level = entry["thinkingLevel"]
        elif entry_type == "model_change":
            model = {"provider": entry["provider"], "modelId": entry["modelId"]}
        elif entry_type == "message" and entry.get("message", {}).get("role") == "assistant":
            message = entry["message"]
            model = {"provider": message["provider"], "modelId": message["model"]}
        elif entry_type == "active_tools_change":
            active_tool_names = list(entry["activeToolNames"])
    return {"thinkingLevel": thinking_level, "model": model, "activeToolNames": active_tool_names}


def default_context_entry_transform(path_entries: Sequence[SessionTreeEntry]) -> list[SessionTreeEntry]:
    compaction = next((entry for entry in reversed(path_entries) if entry.get("type") == "compaction"), None)
    if compaction is None:
        return list(path_entries)
    compaction_index = next(
        index
        for index, entry in enumerate(path_entries)
        if entry.get("type") == "compaction" and entry.get("id") == compaction.get("id")
    )
    entries = [compaction]
    found_first_kept = False
    for entry in path_entries[:compaction_index]:
        if entry.get("id") == compaction.get("firstKeptEntryId"):
            found_first_kept = True
        if found_first_kept:
            entries.append(entry)
    entries.extend(path_entries[compaction_index + 1 :])
    return entries


def build_context_entries(
    path_entries: Sequence[SessionTreeEntry], options: Mapping[str, Any] | None = None
) -> list[SessionTreeEntry]:
    options = options or {}
    entries = default_context_entry_transform(path_entries)
    for transform in _option(options, "entry_transforms", "entryTransforms", []):
        entries = list(transform(entries))
    return entries


def session_entry_to_context_messages(
    entry: SessionTreeEntry,
    index: int,
    entries: Sequence[SessionTreeEntry],
    options: Mapping[str, Any] | None = None,
) -> list[AgentMessage]:
    options = options or {}
    entry_type = entry.get("type")
    if entry_type == "message":
        return [entry["message"]]
    if entry_type == "custom_message":
        return [
            create_custom_message(
                entry["customType"],
                entry["content"],
                bool(entry["display"]),
                entry.get("details"),
                entry["timestamp"],
            )
        ]
    if entry_type == "compaction":
        return [create_compaction_summary_message(entry["summary"], entry["tokensBefore"], entry["timestamp"])]
    if entry_type == "branch_summary" and entry.get("summary"):
        return [create_branch_summary_message(entry["summary"], entry["fromId"], entry["timestamp"])]
    if entry_type == "custom":
        projectors = _option(options, "entry_projectors", "entryProjectors", {})
        projector = projectors.get(entry.get("customType"))
        return list(projector(entry, index, entries) or []) if projector else []
    return []


def build_session_context(
    path_entries: Sequence[SessionTreeEntry], options: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    state = _derive_session_context_state(path_entries)
    context_entries = build_context_entries(path_entries, options)
    messages = [
        message
        for index, entry in enumerate(context_entries)
        for message in session_entry_to_context_messages(entry, index, context_entries, options)
    ]
    return {**state, "messages": messages}


class Session:
    def __init__(self, storage: SessionStorage, context_build_options: Mapping[str, Any] | None = None) -> None:
        self._storage = storage
        self._context_build_options = dict(context_build_options or {})

    async def get_metadata(self) -> SessionMetadata:
        return await self._storage.get_metadata()

    def get_storage(self) -> SessionStorage:
        return self._storage

    async def get_leaf_id(self) -> str | None:
        return await self._storage.get_leaf_id()

    async def get_entry(self, entry_id: str) -> SessionTreeEntry | None:
        return await self._storage.get_entry(entry_id)

    async def get_entries(self) -> list[SessionTreeEntry]:
        return await self._storage.get_entries()

    async def get_branch(self, from_id: str | None = None) -> list[SessionTreeEntry]:
        leaf_id = from_id if from_id is not None else await self._storage.get_leaf_id()
        return await self._storage.get_path_to_root(leaf_id)

    def _merge_options(self, options: Mapping[str, Any] | None) -> dict[str, Any]:
        options = options or {}
        base_transforms = _option(self._context_build_options, "entry_transforms", "entryTransforms", [])
        extra_transforms = _option(options, "entry_transforms", "entryTransforms", [])
        base_projectors = _option(self._context_build_options, "entry_projectors", "entryProjectors", {})
        extra_projectors = _option(options, "entry_projectors", "entryProjectors", {})
        return {
            "entryTransforms": [*base_transforms, *extra_transforms],
            "entryProjectors": {**base_projectors, **extra_projectors},
        }

    async def build_context_entries(self, options: Mapping[str, Any] | None = None) -> list[SessionTreeEntry]:
        return build_context_entries(await self.get_branch(), self._merge_options(options))

    async def build_context(self, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return build_session_context(await self.get_branch(), self._merge_options(options))

    async def get_label(self, entry_id: str) -> str | None:
        return await self._storage.get_label(entry_id)

    async def get_session_name(self) -> str | None:
        entries = await self._storage.find_entries("session_info")
        name = str(entries[-1].get("name") or "").strip() if entries else ""
        return name or None

    async def _append_typed_entry(self, entry: SessionTreeEntry) -> str:
        await self._storage.append_entry(entry)
        return str(entry["id"])

    async def _entry_base(self, entry_type: str) -> SessionTreeEntry:
        return {
            "type": entry_type,
            "id": await self._storage.create_entry_id(),
            "parentId": await self._storage.get_leaf_id(),
            "timestamp": create_timestamp(),
        }

    async def append_message(self, message: AgentMessage) -> str:
        return await self._append_typed_entry({**await self._entry_base("message"), "message": message})

    async def append_thinking_level_change(self, thinking_level: str) -> str:
        return await self._append_typed_entry(
            {**await self._entry_base("thinking_level_change"), "thinkingLevel": thinking_level}
        )

    async def append_model_change(self, provider: str, model_id: str) -> str:
        return await self._append_typed_entry(
            {**await self._entry_base("model_change"), "provider": provider, "modelId": model_id}
        )

    async def append_active_tools_change(self, active_tool_names: list[str]) -> str:
        return await self._append_typed_entry(
            {**await self._entry_base("active_tools_change"), "activeToolNames": list(active_tool_names)}
        )

    async def append_compaction(
        self,
        summary: str,
        first_kept_entry_id: str,
        tokens_before: int,
        details: Any = None,
        from_hook: bool | None = None,
    ) -> str:
        entry = {
            **await self._entry_base("compaction"),
            "summary": summary,
            "firstKeptEntryId": first_kept_entry_id,
            "tokensBefore": tokens_before,
        }
        if details is not None:
            entry["details"] = details
        if from_hook is not None:
            entry["fromHook"] = from_hook
        return await self._append_typed_entry(entry)

    async def append_custom_entry(self, custom_type: str, data: Any = None) -> str:
        entry = {**await self._entry_base("custom"), "customType": custom_type}
        if data is not None:
            entry["data"] = data
        return await self._append_typed_entry(entry)

    async def append_custom_message_entry(
        self,
        custom_type: str,
        content: str | list[dict[str, Any]],
        display: bool,
        details: Any = None,
    ) -> str:
        entry = {
            **await self._entry_base("custom_message"),
            "customType": custom_type,
            "content": content,
            "display": display,
        }
        if details is not None:
            entry["details"] = details
        return await self._append_typed_entry(entry)

    async def append_label(self, target_id: str, label: str | None) -> str:
        if await self._storage.get_entry(target_id) is None:
            raise SessionError("not_found", f"Entry {target_id} not found")
        entry = {**await self._entry_base("label"), "targetId": target_id}
        if label is not None:
            entry["label"] = label
        return await self._append_typed_entry(entry)

    async def append_session_name(self, name: str) -> str:
        sanitized = re.sub(r"[\r\n]+", " ", name).strip()
        return await self._append_typed_entry({**await self._entry_base("session_info"), "name": sanitized})

    async def move_to(self, entry_id: str | None, summary: Mapping[str, Any] | None = None) -> str | None:
        if entry_id is not None and await self._storage.get_entry(entry_id) is None:
            raise SessionError("not_found", f"Entry {entry_id} not found")
        await self._storage.set_leaf_id(entry_id)
        if summary is None:
            return None
        entry: SessionTreeEntry = {
            "type": "branch_summary",
            "id": await self._storage.create_entry_id(),
            "parentId": entry_id,
            "timestamp": create_timestamp(),
            "fromId": entry_id if entry_id is not None else "root",
            "summary": summary["summary"],
        }
        details = summary.get("details")
        from_hook = summary.get("fromHook", summary.get("from_hook"))
        if details is not None:
            entry["details"] = details
        if from_hook is not None:
            entry["fromHook"] = from_hook
        return await self._append_typed_entry(entry)

    getMetadata = get_metadata
    getStorage = get_storage
    getLeafId = get_leaf_id
    getEntry = get_entry
    getEntries = get_entries
    getBranch = get_branch
    buildContextEntries = build_context_entries
    buildContext = build_context
    getLabel = get_label
    getSessionName = get_session_name
    appendMessage = append_message
    appendThinkingLevelChange = append_thinking_level_change
    appendModelChange = append_model_change
    appendActiveToolsChange = append_active_tools_change
    appendCompaction = append_compaction
    appendCustomEntry = append_custom_entry
    appendCustomMessageEntry = append_custom_message_entry
    appendLabel = append_label
    appendSessionName = append_session_name
    moveTo = move_to


defaultContextEntryTransform = default_context_entry_transform
buildContextEntries = build_context_entries
sessionEntryToContextMessages = session_entry_to_context_messages
buildSessionContext = build_session_context
