from __future__ import annotations

import json
from functools import cmp_to_key
from pathlib import Path
from typing import Any

import pytest

from aster_harness.harness.env import NodeExecutionEnv
from aster_harness.harness.session import (
    InMemorySessionRepo,
    InMemorySessionStorage,
    JsonlSessionRepo,
    JsonlSessionStorage,
    Session,
    load_jsonl_session_metadata,
)
from aster_harness.harness.session.jsonl_repo import _compare_created_at_desc
from aster_harness.harness.types import SessionError, get_or_throw


def message_entry(entry_id: str, parent: str | None, text: str, *, role: str = "user") -> dict[str, Any]:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent,
        "timestamp": "2026-01-01T00:00:00.000Z",
        "message": {"role": role, "content": text, "timestamp": 1},
    }


@pytest.mark.asyncio
async def test_memory_storage_leaf_labels_paths_and_copy_semantics() -> None:
    source = [message_entry("root", None, "root")]
    storage = InMemorySessionStorage(
        entries=source,
        metadata={"id": "session", "createdAt": "2026-01-01T00:00:00.000Z"},
    )
    source.clear()
    assert [entry["id"] for entry in await storage.get_entries()] == ["root"]
    with pytest.raises(SessionError) as missing:
        await storage.set_leaf_id("missing")
    assert missing.value.code == "not_found"

    child = message_entry("child", "root", "answer", role="assistant")
    await storage.append_entry(child)
    await storage.append_entry(
        {
            "type": "label",
            "id": "label-1",
            "parentId": "child",
            "timestamp": "2026-01-01T00:00:01.000Z",
            "targetId": "root",
            "label": "  useful  ",
        }
    )
    assert await storage.get_label("root") == "useful"
    await storage.append_entry(
        {
            "type": "label",
            "id": "label-2",
            "parentId": "label-1",
            "timestamp": "2026-01-01T00:00:02.000Z",
            "targetId": "root",
            "label": "",
        }
    )
    assert await storage.get_label("root") is None
    assert [entry["id"] for entry in await storage.get_path_to_root("child")] == ["root", "child"]


@pytest.mark.asyncio
async def test_memory_repo_forks_before_and_at_entries() -> None:
    repo = InMemorySessionRepo()
    source = await repo.create({"id": "source"})
    root = await source.append_message({"role": "user", "content": "root", "timestamp": 1})
    assistant_id = await source.append_message(
        {"role": "assistant", "content": [], "provider": "p", "model": "m", "timestamp": 2}
    )
    next_user = await source.append_message({"role": "user", "content": "next", "timestamp": 3})
    metadata = await source.get_metadata()

    before = await repo.fork(metadata, {"id": "before", "entryId": next_user})
    at = await repo.fork(metadata, {"id": "at", "entryId": assistant_id, "position": "at"})
    assert [entry["id"] for entry in await before.get_branch()] == [root, assistant_id]
    assert [entry["id"] for entry in await at.get_branch()] == [root, assistant_id]
    with pytest.raises(SessionError) as invalid:
        await repo.fork(metadata, {"entryId": assistant_id})
    assert invalid.value.code == "invalid_fork_target"
    await repo.delete(metadata)
    with pytest.raises(SessionError, match="Session not found"):
        await repo.open(metadata)


@pytest.mark.asyncio
async def test_jsonl_storage_validation_and_metadata(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    path = tmp_path / "session.jsonl"
    storage = await JsonlSessionStorage.create(
        env,
        str(path),
        {
            "cwd": str(tmp_path),
            "sessionId": "s",
            "parentSessionPath": "/parent.jsonl",
            "metadata": {"project": "pi"},
        },
    )
    session = Session(storage)
    first = await session.append_message({"role": "user", "content": "hello", "timestamp": 1})
    await storage.set_leaf_id(None)
    reopened = await JsonlSessionStorage.open(env, str(path))
    assert await reopened.get_leaf_id() is None
    assert (await reopened.get_entry(first))["message"]["content"] == "hello"
    metadata = await load_jsonl_session_metadata(env, str(path))
    assert metadata["parentSessionPath"] == "/parent.jsonl"
    assert metadata["metadata"] == {"project": "pi"}

    malformed_cases = [
        ("bad-json.jsonl", "not json\n", "invalid_session"),
        (
            "bad-version.jsonl",
            json.dumps({"type": "session", "version": 2, "id": "x", "timestamp": "t", "cwd": "/"}) + "\n",
            "invalid_session",
        ),
        (
            "bad-metadata.jsonl",
            json.dumps({"type": "session", "version": 3, "id": "x", "timestamp": "t", "cwd": "/", "metadata": []})
            + "\n",
            "invalid_session",
        ),
        (
            "bad-entry.jsonl",
            json.dumps({"type": "session", "version": 3, "id": "x", "timestamp": "t", "cwd": "/"}) + "\n{}\n",
            "invalid_entry",
        ),
    ]
    for filename, content, code in malformed_cases:
        get_or_throw(await env.write_file(filename, content))
        with pytest.raises(SessionError) as caught:
            await JsonlSessionStorage.open(env, filename)
        assert caught.value.code == code

    numeric_path = tmp_path / "numeric.jsonl"
    header = {"type": "session", "version": 3, "id": "n", "timestamp": "t", "cwd": "/"}
    numeric_entry = {
        "type": "compaction",
        "id": "large",
        "parentId": None,
        "timestamp": "t",
        "summary": "summary",
        "firstKeptEntryId": "large",
        "tokensBefore": 9_007_199_254_740_993,
    }
    get_or_throw(await env.write_file(str(numeric_path), json.dumps(header) + "\n" + json.dumps(numeric_entry) + "\n"))
    numeric_storage = await JsonlSessionStorage.open(env, str(numeric_path))
    assert (await numeric_storage.get_entry("large"))["tokensBefore"] == 9_007_199_254_740_992


@pytest.mark.asyncio
async def test_jsonl_omits_undefined_optional_entry_fields(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    path = tmp_path / "optional.jsonl"
    storage = await JsonlSessionStorage.create(
        env,
        str(path),
        {"cwd": str(tmp_path), "sessionId": "optional"},
    )
    session = Session(storage)
    target = await session.append_message({"role": "user", "content": "hello", "timestamp": 1})
    await session.append_custom_entry("marker")
    await session.append_custom_message_entry("notice", "text", True)
    await session.append_label(target, None)
    await session.append_compaction("summary", target, 10)

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert "metadata" not in records[0] and "parentSession" not in records[0]
    custom, custom_message, label, compaction = records[-4:]
    assert "data" not in custom
    assert "details" not in custom_message
    assert "label" not in label
    assert "details" not in compaction and "fromHook" not in compaction


@pytest.mark.asyncio
async def test_jsonl_repo_create_list_fork_open_delete(tmp_path: Path) -> None:
    env = NodeExecutionEnv(cwd=str(tmp_path))
    repo = JsonlSessionRepo({"fs": env, "sessionsRoot": "sessions"})
    first = await repo.create({"id": "one", "cwd": "/work/a", "metadata": {"team": "a"}})
    user_id = await first.append_message({"role": "user", "content": "hello", "timestamp": 1})
    metadata = await first.get_metadata()
    second = await repo.create({"id": "two", "cwd": "/work/b"})
    assert (await second.get_metadata())["id"] == "two"

    listed_a = await repo.list({"cwd": "/work/a"})
    assert [item["id"] for item in listed_a] == ["one"]
    assert listed_a[0]["metadata"] == {"team": "a"}
    assert "--work-a--" in listed_a[0]["path"]

    forked = await repo.fork(
        metadata,
        {"id": "fork", "cwd": "/work/a", "entryId": user_id, "position": "at"},
    )
    fork_metadata = await forked.get_metadata()
    assert fork_metadata["parentSessionPath"] == metadata["path"]
    assert fork_metadata["metadata"] == {"team": "a"}
    assert [entry["id"] for entry in await forked.get_branch()] == [user_id]
    assert (await repo.open(fork_metadata)).get_storage() is not None

    await repo.delete(metadata)
    with pytest.raises(SessionError) as deleted:
        await repo.open(metadata)
    assert deleted.value.code == "not_found"


def test_jsonl_repo_sorts_timestamp_offsets_like_javascript_date() -> None:
    sessions = [
        {"id": "lexically-later", "createdAt": "2026-01-01T10:00:00-08:00"},
        {"id": "newest", "createdAt": "2026-01-01T19:00:00+00:00"},
        {"id": "oldest", "createdAt": "2026-01-01T17:00:00Z"},
    ]
    sessions.sort(key=cmp_to_key(_compare_created_at_desc))
    assert [session["id"] for session in sessions] == ["newest", "lexically-later", "oldest"]
