from __future__ import annotations

from typing import Any

import pytest

from pi_agent_core import AbortController
from pi_agent_core.harness.compaction import (
    DEFAULT_COMPACTION_SETTINGS,
    calculate_context_tokens,
    collect_entries_for_branch_summary,
    compact,
    compute_file_lists,
    create_file_ops,
    estimate_context_tokens,
    estimate_tokens,
    extract_file_ops_from_message,
    find_cut_point,
    format_file_operations,
    generate_branch_summary,
    generate_summary,
    prepare_branch_entries,
    prepare_compaction,
    serialize_conversation,
    should_compact,
)
from pi_agent_core.harness.session import InMemorySessionStorage, Session

STAMP = "2026-01-01T00:00:00.000Z"


def user(text: str, timestamp: int = 1) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}], "timestamp": timestamp}


def assistant(text: str, tokens: int = 150, timestamp: int = 2) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "provider": "faux",
        "model": "model",
        "api": "faux",
        "usage": {
            "input": tokens - 50,
            "output": 50,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": tokens,
        },
        "stopReason": "stop",
        "timestamp": timestamp,
    }


def entry(entry_id: str, message: dict[str, Any], parent: str | None = None) -> dict[str, Any]:
    return {"type": "message", "id": entry_id, "parentId": parent, "timestamp": STAMP, "message": message}


class FauxModels:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {
            "role": "assistant",
            "content": [{"type": "text", "text": "generated summary"}],
            "stopReason": "stop",
        }
        self.calls: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []

    async def completeSimple(self, model: Any, context: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((model, context, options))
        return self.response


def test_token_calculation_estimation_and_threshold() -> None:
    usage = {"input": 100, "output": 50, "cacheRead": 20, "cacheWrite": 10, "totalTokens": 180}
    assert calculate_context_tokens(usage) == 180
    assert should_compact(95_000, 100_000, {"enabled": True, "reserveTokens": 10_000})
    assert not should_compact(89_000, 100_000, {"enabled": True, "reserveTokens": 10_000})
    assert estimate_tokens(user("12345")) == 2
    assert estimate_tokens(user("🙂🙂🙂")) == 2
    image_message = {"role": "user", "content": [{"type": "image", "data": "x", "mimeType": "image/png"}]}
    assert estimate_tokens(image_message) == 1200

    messages = [user("old"), assistant("answer", 700), user("12345678")]
    assert estimate_context_tokens(messages) == {
        "tokens": 702,
        "usageTokens": 700,
        "trailingTokens": 2,
        "lastUsageIndex": 1,
    }


def test_conversation_serialization_and_file_metadata() -> None:
    messages = [
        user("hello"),
        {
            **assistant("ignored"),
            "content": [
                {"type": "thinking", "thinking": "plan"},
                {"type": "text", "text": "answer"},
                {"type": "toolCall", "id": "1", "name": "read", "arguments": {"path": "a.py"}},
            ],
        },
        {
            "role": "toolResult",
            "toolCallId": "1",
            "toolName": "read",
            "content": [{"type": "text", "text": "x" * 2005}],
            "isError": False,
            "timestamp": 3,
        },
    ]
    serialized = serialize_conversation(messages)
    assert "[User]: hello" in serialized
    assert "[Assistant thinking]: plan" in serialized
    assert '[Assistant tool calls]: read(path="a.py")' in serialized
    assert "[... 5 more characters truncated]" in serialized

    ops = create_file_ops()
    extract_file_ops_from_message(messages[1], ops)
    ops["written"].add("b.py")
    ops["read"].add("b.py")
    assert compute_file_lists(ops) == {"readFiles": ["a.py"], "modifiedFiles": ["b.py"]}
    assert format_file_operations(["a.py"], ["b.py"]).startswith("\n\n<read-files>")


def test_conversation_serialization_uses_javascript_object_entry_order() -> None:
    serialized = serialize_conversation(
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "demo",
                        "arguments": {"b": 1, "10": 2, "2": 3, "01": 4},
                    }
                ],
            }
        ]
    )

    assert serialized == "[Assistant tool calls]: demo(2=3, 10=2, b=1, 01=4)"


def test_cut_point_and_prepare_compaction_preserve_turn_boundaries() -> None:
    entries = [
        entry("u1", user("first request")),
        entry("a1", assistant("a" * 40), "u1"),
        entry("u2", user("second request"), "a1"),
        entry("a2", assistant("b" * 40), "u2"),
    ]
    cut = find_cut_point(entries, 0, len(entries), 10)
    assert cut["firstKeptEntryIndex"] in {2, 3}
    prepared = prepare_compaction(entries, {**DEFAULT_COMPACTION_SETTINGS, "keepRecentTokens": 10})
    assert prepared["ok"]
    value = prepared["value"]
    assert value is not None
    assert value["firstKeptEntryId"] in {"u2", "a2"}
    assert value["tokensBefore"] >= 150


def test_prepare_compaction_inherits_previous_summary_file_ops_and_custom_entries() -> None:
    old_user = entry("u1", user("old"))
    old_assistant = entry(
        "a1",
        {
            **assistant(""),
            "content": [{"type": "toolCall", "id": "w", "name": "write", "arguments": {"path": "written.py"}}],
        },
        "u1",
    )
    previous = {
        "type": "compaction",
        "id": "c1",
        "parentId": "a1",
        "timestamp": STAMP,
        "summary": "old summary",
        "firstKeptEntryId": "u1",
        "tokensBefore": 500,
        "details": {"readFiles": ["old-read.py"], "modifiedFiles": ["old-edit.py"]},
    }
    large_user = entry("u2", user("large turn"), "c1")
    large_assistant = entry("a2", assistant("large assistant"), "u2")
    prepared = prepare_compaction(
        [old_user, old_assistant, previous, large_user, large_assistant],
        {"enabled": True, "reserveTokens": 100, "keepRecentTokens": 1},
    )
    assert prepared["ok"]
    value = prepared["value"]
    assert value["previousSummary"] == "old summary"
    assert value["isSplitTurn"] is True
    assert [message["role"] for message in value["turnPrefixMessages"]] == ["user"]
    assert value["fileOps"]["read"] == {"old-read.py"}
    assert value["fileOps"]["edited"] == {"old-edit.py"}
    assert value["fileOps"]["written"] == {"written.py"}

    branch = {
        "type": "branch_summary",
        "id": "b",
        "parentId": None,
        "timestamp": STAMP,
        "fromId": "old-branch",
        "summary": "branch summary",
    }
    custom = {
        "type": "custom_message",
        "id": "x",
        "parentId": "b",
        "timestamp": STAMP,
        "customType": "note",
        "content": "custom content",
        "display": True,
    }
    kept_user = entry("keep", user("keep"), "x")
    kept_assistant = entry("answer", assistant("answer"), "keep")
    custom_prepared = prepare_compaction(
        [branch, custom, kept_user, kept_assistant],
        {"enabled": True, "reserveTokens": 100, "keepRecentTokens": 1},
    )
    assert custom_prepared["ok"]
    assert [message["role"] for message in custom_prepared["value"]["messagesToSummarize"]] == [
        "branchSummary",
        "custom",
    ]


@pytest.mark.asyncio
async def test_generate_summary_options_and_errors() -> None:
    controller = AbortController()
    models = FauxModels()
    model = {"maxTokens": 1000, "reasoning": True}
    result = await generate_summary([user("work")], models, model, 100, controller.signal, thinking_level="high")
    assert result == {"ok": True, "value": "generated summary"}
    assert models.calls[0][2]["maxTokens"] == 80
    assert models.calls[0][2]["reasoning"] == "high"

    failed = FauxModels({"content": [], "stopReason": "error", "errorMessage": "provider down"})
    result = await generate_summary([user("work")], failed, model, 100)
    assert not result["ok"]
    assert result["error"].code == "summarization_failed"
    assert str(result["error"]) == "Summarization failed: provider down"

    capped = FauxModels()
    capped_model = {"maxTokens": 32, "reasoning": False}
    await generate_summary([user("work")], capped, capped_model, 1000)
    assert capped.calls[0][2]["maxTokens"] == 32


@pytest.mark.asyncio
async def test_compact_split_turn_generates_two_summaries_and_files() -> None:
    models = FauxModels()
    model = {"maxTokens": 1000, "reasoning": False}
    ops = create_file_ops()
    ops["read"].add("src/a.py")
    preparation = {
        "firstKeptEntryId": "a2",
        "messagesToSummarize": [user("old")],
        "turnPrefixMessages": [user("large turn prefix")],
        "isSplitTurn": True,
        "tokensBefore": 1234,
        "previousSummary": None,
        "fileOps": ops,
        "settings": DEFAULT_COMPACTION_SETTINGS,
    }
    result = await compact(preparation, models, model)
    assert result["ok"]
    assert len(models.calls) == 2
    assert "**Turn Context (split turn):**" in result["value"]["summary"]
    assert result["value"]["details"] == {"readFiles": ["src/a.py"], "modifiedFiles": []}


@pytest.mark.asyncio
async def test_branch_collection_preparation_and_generation() -> None:
    storage = InMemorySessionStorage(metadata={"id": "session", "createdAt": STAMP})
    session = Session(storage)
    root = await session.append_message(user("root"))
    old_user = await session.append_message(user("branch work"))
    old_assistant = await session.append_message(
        {
            **assistant("done"),
            "content": [{"type": "toolCall", "id": "t", "name": "write", "arguments": {"path": "result.py"}}],
        }
    )
    await session.move_to(root)
    target = await session.append_message(user("other"))

    collected = await collect_entries_for_branch_summary(session, old_assistant, target)
    assert collected["commonAncestorId"] == root
    assert [item["id"] for item in collected["entries"]] == [old_user, old_assistant]
    prepared = prepare_branch_entries(collected["entries"])
    assert prepared["fileOps"]["written"] == {"result.py"}

    models = FauxModels()
    result = await generate_branch_summary(
        collected["entries"],
        {
            "models": models,
            "model": {"contextWindow": 128000},
            "signal": AbortController().signal,
        },
    )
    assert result["ok"]
    assert result["value"]["modifiedFiles"] == ["result.py"]
    assert result["value"]["summary"].startswith("The user explored a different conversation branch")
