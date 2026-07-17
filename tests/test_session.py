from __future__ import annotations

import pytest

from pi_agent_core.harness.messages import create_branch_summary_message
from pi_agent_core.harness.session import InMemorySessionStorage, Session, build_session_context, uuidv7
from pi_agent_core.harness.types import SessionError


async def test_session_tree_branching_and_context() -> None:
    storage = InMemorySessionStorage(metadata={"id": "s", "createdAt": "2026-01-01T00:00:00.000Z"})
    session = Session(storage)
    first = await session.append_message({"role": "user", "content": "one", "timestamp": 1})
    old_leaf = await session.append_message(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "old"}],
            "provider": "mock",
            "model": "m",
            "timestamp": 2,
        }
    )
    await session.move_to(first)
    new_leaf = await session.append_message({"role": "user", "content": "branch", "timestamp": 3})

    assert await session.get_leaf_id() == new_leaf
    assert [entry["id"] for entry in await session.get_branch()] == [first, new_leaf]
    assert [entry["id"] for entry in await session.get_branch(old_leaf)] == [first, old_leaf]


async def test_compaction_and_custom_projection() -> None:
    entries = [
        {
            "type": "message",
            "id": "u1",
            "parentId": None,
            "timestamp": "2026-01-01T00:00:00.000Z",
            "message": {"role": "user", "content": "old", "timestamp": 1},
        },
        {
            "type": "message",
            "id": "u2",
            "parentId": "u1",
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {"role": "user", "content": "keep", "timestamp": 2},
        },
        {
            "type": "compaction",
            "id": "c1",
            "parentId": "u2",
            "timestamp": "2026-01-01T00:00:02.000Z",
            "summary": "summary",
            "firstKeptEntryId": "u2",
            "tokensBefore": 100,
        },
        {
            "type": "custom",
            "id": "x1",
            "parentId": "c1",
            "timestamp": "2026-01-01T00:00:03.000Z",
            "customType": "note",
            "data": "project me",
        },
    ]
    context = build_session_context(
        entries,
        {
            "entryProjectors": {
                "note": lambda entry, _index, _entries: [{"role": "custom", "content": entry["data"], "timestamp": 3}]
            }
        },
    )

    assert [message["role"] for message in context["messages"]] == ["compactionSummary", "user", "custom"]
    assert context["messages"][1]["content"] == "keep"


async def test_context_state_transform_order_projector_override_names_and_labels() -> None:
    observed: list[tuple[str, list[str]]] = []

    def base_transform(entries: list[dict[str, object]]) -> list[dict[str, object]]:
        observed.append(("base", [str(entry["type"]) for entry in entries]))
        return [entry for entry in entries if entry["type"] != "compaction"]

    def call_transform(entries: list[dict[str, object]]) -> list[dict[str, object]]:
        observed.append(("call", [str(entry["type"]) for entry in entries]))
        return entries

    storage = InMemorySessionStorage(metadata={"id": "s", "createdAt": "2026-01-01T00:00:00.000Z"})
    session = Session(
        storage,
        {
            "entryTransforms": [base_transform],
            "entryProjectors": {
                "note": lambda *_: [{"role": "custom", "content": "base", "timestamp": 1}],
            },
        },
    )
    await session.append_message({"role": "user", "content": "discard", "timestamp": 1})
    await session.append_model_change("openai", "gpt-4.1")
    await session.append_thinking_level_change("high")
    await session.append_active_tools_change(["read", "write"])
    kept = await session.append_message({"role": "user", "content": "keep", "timestamp": 2})
    await session.append_compaction("summary", kept, 1234)
    await session.append_custom_entry("note", {"text": "hello"})
    await session.append_session_name(" hello\nworld\r\nagain ")
    await session.append_label(kept, "checkpoint")

    context = await session.build_context(
        {
            "entryTransforms": [call_transform],
            "entryProjectors": {
                "note": lambda entry, index, entries: [
                    {
                        "role": "custom",
                        "content": f"override:{entry['data']['text']}:{index}:{len(entries)}",
                        "timestamp": 3,
                    }
                ],
            },
        }
    )

    assert context["thinkingLevel"] == "high"
    assert context["model"] == {"provider": "openai", "modelId": "gpt-4.1"}
    assert context["activeToolNames"] == ["read", "write"]
    assert [message["role"] for message in context["messages"]] == ["user", "custom"]
    assert str(context["messages"][1]["content"]).startswith("override:hello:")
    assert observed[0][0] == "base" and observed[0][1][0] == "compaction"
    assert observed[1][0] == "call" and "compaction" not in observed[1][1]
    assert await session.get_session_name() == "hello world again"
    assert await session.get_label(kept) == "checkpoint"

    await session.append_label(kept, None)
    assert await session.get_label(kept) is None
    with pytest.raises(SessionError, match="Entry missing not found") as caught:
        await session.append_label("missing", "checkpoint")
    assert caught.value.code == "not_found"


async def test_move_to_root_and_branch_summary_shape() -> None:
    storage = InMemorySessionStorage(metadata={"id": "s", "createdAt": "2026-01-01T00:00:00.000Z"})
    session = Session(storage)
    first = await session.append_message({"role": "user", "content": "one", "timestamp": 1})
    await session.append_message({"role": "user", "content": "two", "timestamp": 2})

    summary_id = await session.move_to(first, {"summary": "old branch", "details": {"tokens": 12}, "fromHook": True})
    assert summary_id is not None
    summary = await session.get_entry(summary_id)
    assert summary == {
        "type": "branch_summary",
        "id": summary_id,
        "parentId": first,
        "timestamp": summary["timestamp"],
        "fromId": first,
        "summary": "old branch",
        "details": {"tokens": 12},
        "fromHook": True,
    }
    assert [message["role"] for message in (await session.build_context())["messages"]] == ["user", "branchSummary"]

    assert await session.move_to(None) is None
    assert await session.get_leaf_id() is None
    assert (await session.build_context())["messages"] == []


def test_uuidv7_layout_and_monotonicity() -> None:
    values = [uuidv7() for _ in range(100)]
    assert values == sorted(values)
    assert all(value[14] == "7" for value in values)
    assert all(value[19] in "89ab" for value in values)


def test_context_message_dates_match_javascript_date_only_and_invalid_semantics() -> None:
    date_only = create_branch_summary_message("summary", "root", "2026-01-01")
    invalid = create_branch_summary_message("summary", "root", "not-a-date")

    assert date_only["timestamp"] == 1_767_225_600_000
    assert invalid["timestamp"] != invalid["timestamp"]
