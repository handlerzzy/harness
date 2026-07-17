from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias, cast

from ..._utils import get_any_field, js_json_stringify, js_utf16_length, maybe_await, now_ms
from ...abort import AbortSignal
from ...types import AgentMessage
from ..messages import (
    convert_to_llm,
    create_branch_summary_message,
    create_compaction_summary_message,
    create_custom_message,
)
from ..session.session import build_session_context
from ..types import CompactionError, Result, SessionTreeEntry, err, ok
from .utils import (
    FileOperations,
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)

DEFAULT_COMPACTION_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "reserveTokens": 16384,
    "keepRecentTokens": 20000,
}
ESTIMATED_IMAGE_CHARS = 4800

CompactionDetails: TypeAlias = dict[str, Any]
CompactionResult: TypeAlias = dict[str, Any]
CompactionSettings: TypeAlias = dict[str, Any]
ContextUsageEstimate: TypeAlias = dict[str, Any]
CutPointResult: TypeAlias = dict[str, Any]
CompactionPreparation: TypeAlias = dict[str, Any]


def _setting(settings: Mapping[str, Any], snake: str, camel: str) -> Any:
    return settings.get(snake, settings.get(camel, DEFAULT_COMPACTION_SETTINGS[camel]))


def _safe_json_stringify(value: Any) -> str:
    try:
        return js_json_stringify(value) or "undefined"
    except (TypeError, ValueError, RecursionError):
        return "[unserializable]"


def _entry_message(entry: SessionTreeEntry, *, for_compaction: bool = False) -> AgentMessage | None:
    entry_type = entry.get("type")
    if entry_type == "compaction" and for_compaction:
        return None
    if entry_type == "message":
        return cast(AgentMessage, entry["message"])
    if entry_type == "custom_message":
        return create_custom_message(
            entry["customType"], entry["content"], bool(entry["display"]), entry.get("details"), entry["timestamp"]
        )
    if entry_type == "branch_summary":
        return create_branch_summary_message(entry["summary"], entry["fromId"], entry["timestamp"])
    if entry_type == "compaction":
        return create_compaction_summary_message(entry["summary"], entry["tokensBefore"], entry["timestamp"])
    return None


def calculate_context_tokens(usage: Mapping[str, Any]) -> int:
    return int(
        usage.get("totalTokens")
        or usage.get("total_tokens")
        or (
            usage.get("input", 0)
            + usage.get("output", 0)
            + usage.get("cacheRead", usage.get("cache_read", 0))
            + usage.get("cacheWrite", usage.get("cache_write", 0))
        )
    )


def _assistant_usage(message: AgentMessage) -> Mapping[str, Any] | None:
    if message.get("role") != "assistant" or message.get("stopReason") in {"aborted", "error"}:
        return None
    usage = message.get("usage")
    return usage if isinstance(usage, Mapping) and calculate_context_tokens(usage) > 0 else None


def get_last_assistant_usage(entries: Sequence[SessionTreeEntry]) -> Mapping[str, Any] | None:
    for entry in reversed(entries):
        if entry.get("type") == "message":
            usage = _assistant_usage(entry["message"])
            if usage is not None:
                return usage
    return None


def _content_chars(content: str | list[dict[str, Any]]) -> int:
    if isinstance(content, str):
        return js_utf16_length(content)
    chars = 0
    for block in content:
        if block.get("type") == "text" and block.get("text"):
            chars += js_utf16_length(str(block["text"]))
        elif block.get("type") == "image":
            chars += ESTIMATED_IMAGE_CHARS
    return chars


def estimate_tokens(message: AgentMessage) -> int:
    role = message.get("role")
    chars = 0
    if role == "user":
        chars = _content_chars(message.get("content", ""))
    elif role == "assistant":
        for block in message.get("content", []):
            if block.get("type") == "text":
                chars += js_utf16_length(str(block.get("text", "")))
            elif block.get("type") == "thinking":
                chars += js_utf16_length(str(block.get("thinking", "")))
            elif block.get("type") == "toolCall":
                chars += js_utf16_length(str(block.get("name", ""))) + js_utf16_length(
                    _safe_json_stringify(block.get("arguments"))
                )
    elif role in {"custom", "toolResult"}:
        chars = _content_chars(message.get("content", ""))
    elif role == "bashExecution":
        chars = js_utf16_length(str(message.get("command", ""))) + js_utf16_length(str(message.get("output", "")))
    elif role in {"branchSummary", "compactionSummary"}:
        chars = js_utf16_length(str(message.get("summary", "")))
    return math.ceil(chars / 4)


def estimate_context_tokens(messages: Sequence[AgentMessage]) -> dict[str, int | None]:
    usage_info: tuple[Mapping[str, Any], int] | None = None
    for index in range(len(messages) - 1, -1, -1):
        usage = _assistant_usage(messages[index])
        if usage is not None:
            usage_info = (usage, index)
            break
    if usage_info is None:
        estimated = sum(estimate_tokens(message) for message in messages)
        return {"tokens": estimated, "usageTokens": 0, "trailingTokens": estimated, "lastUsageIndex": None}
    usage, index = usage_info
    usage_tokens = calculate_context_tokens(usage)
    trailing = sum(estimate_tokens(message) for message in messages[index + 1 :])
    return {
        "tokens": usage_tokens + trailing,
        "usageTokens": usage_tokens,
        "trailingTokens": trailing,
        "lastUsageIndex": index,
    }


def should_compact(context_tokens: int, context_window: int, settings: Mapping[str, Any]) -> bool:
    return bool(_setting(settings, "enabled", "enabled")) and context_tokens > context_window - int(
        _setting(settings, "reserve_tokens", "reserveTokens")
    )


def _valid_cut_points(entries: Sequence[SessionTreeEntry], start: int, end: int) -> list[int]:
    points: list[int] = []
    for index in range(start, end):
        entry = entries[index]
        if entry.get("type") == "message":
            role = entry.get("message", {}).get("role")
            if role in {"bashExecution", "custom", "branchSummary", "compactionSummary", "user", "assistant"}:
                points.append(index)
        elif entry.get("type") in {"branch_summary", "custom_message"}:
            points.append(index)
    return points


def find_turn_start_index(entries: Sequence[SessionTreeEntry], entry_index: int, start_index: int) -> int:
    for index in range(entry_index, start_index - 1, -1):
        entry = entries[index]
        if entry.get("type") in {"branch_summary", "custom_message"}:
            return index
        if entry.get("type") == "message" and entry.get("message", {}).get("role") in {"user", "bashExecution"}:
            return index
    return -1


def find_cut_point(
    entries: Sequence[SessionTreeEntry], start_index: int, end_index: int, keep_recent_tokens: int
) -> dict[str, int | bool]:
    cut_points = _valid_cut_points(entries, start_index, end_index)
    if not cut_points:
        return {"firstKeptEntryIndex": start_index, "turnStartIndex": -1, "isSplitTurn": False}
    accumulated = 0
    cut_index = cut_points[0]
    for index in range(end_index - 1, start_index - 1, -1):
        entry = entries[index]
        if entry.get("type") != "message":
            continue
        accumulated += estimate_tokens(entry["message"])
        if accumulated >= keep_recent_tokens:
            cut_index = next((point for point in cut_points if point >= index), cut_index)
            break
    while cut_index > start_index:
        previous = entries[cut_index - 1]
        if previous.get("type") in {"compaction", "message"}:
            break
        cut_index -= 1
    cut_entry = entries[cut_index]
    is_user = cut_entry.get("type") == "message" and cut_entry.get("message", {}).get("role") == "user"
    turn_start = -1 if is_user else find_turn_start_index(entries, cut_index, start_index)
    return {
        "firstKeptEntryIndex": cut_index,
        "turnStartIndex": turn_start,
        "isSplitTurn": not is_user and turn_start != -1,
    }


SUMMARIZATION_SYSTEM_PROMPT = """You are a context summarization assistant. Your task is to read a conversation between a user and an AI assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary."""

SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

UPDATE_SUMMARIZATION_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

TURN_PREFIX_SUMMARIZATION_PROMPT = """This is the PREFIX of a turn that was too large to keep. The SUFFIX (recent work) is retained.

Summarize the prefix to provide context for the retained suffix:

## Original Request
[What did the user ask for in this turn?]

## Early Progress
- [Key decisions and work done in the prefix]

## Context for Suffix
- [Information needed to understand the retained recent work]

Be concise. Focus on what's needed to understand the kept suffix."""


async def _complete_simple(models: Any, model: Any, context: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    method = get_any_field(models, "complete_simple", "completeSimple")
    if method is None:
        raise TypeError("models must provide complete_simple() or completeSimple()")
    return await maybe_await(method(model, context, options))


def _model_value(model: Any, snake: str, camel: str, default: Any) -> Any:
    return get_any_field(model, snake, camel, default=default)


async def generate_summary(
    current_messages: Sequence[AgentMessage],
    models: Any,
    model: Any,
    reserve_tokens: int,
    signal: AbortSignal | None = None,
    custom_instructions: str | None = None,
    previous_summary: str | None = None,
    thinking_level: str | None = None,
) -> Result[str, CompactionError]:
    model_max = int(_model_value(model, "max_tokens", "maxTokens", 0))
    max_tokens = min(math.floor(0.8 * reserve_tokens), model_max if model_max > 0 else math.inf)
    base_prompt = UPDATE_SUMMARIZATION_PROMPT if previous_summary else SUMMARIZATION_PROMPT
    if custom_instructions:
        base_prompt += f"\n\nAdditional focus: {custom_instructions}"
    prompt = f"<conversation>\n{serialize_conversation(convert_to_llm(list(current_messages)))}\n</conversation>\n\n"
    if previous_summary:
        prompt += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
    prompt += base_prompt
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}], "timestamp": now_ms()}]
    options: dict[str, Any] = {"maxTokens": int(max_tokens), "signal": signal}
    if _model_value(model, "reasoning", "reasoning", False) and thinking_level and thinking_level != "off":
        options["reasoning"] = thinking_level
    response = await _complete_simple(
        models, model, {"systemPrompt": SUMMARIZATION_SYSTEM_PROMPT, "messages": messages}, options
    )
    stop_reason = response.get("stopReason", response.get("stop_reason"))
    if stop_reason == "aborted":
        return err(CompactionError("aborted", response.get("errorMessage") or "Summarization aborted"))
    if stop_reason == "error":
        return err(
            CompactionError(
                "summarization_failed", f"Summarization failed: {response.get('errorMessage') or 'Unknown error'}"
            )
        )
    text = "\n".join(str(block.get("text", "")) for block in response.get("content", []) if block.get("type") == "text")
    return ok(text)


def _extract_file_operations(
    messages: Sequence[AgentMessage], entries: Sequence[SessionTreeEntry], previous_index: int
) -> FileOperations:
    file_ops = create_file_ops()
    if previous_index >= 0:
        previous = entries[previous_index]
        details = previous.get("details")
        if not previous.get("fromHook") and isinstance(details, Mapping):
            read_files = details.get("readFiles")
            if isinstance(read_files, list):
                file_ops["read"].update(path for path in read_files if isinstance(path, str))
            modified_files = details.get("modifiedFiles")
            if isinstance(modified_files, list):
                file_ops["edited"].update(path for path in modified_files if isinstance(path, str))
    for message in messages:
        extract_file_ops_from_message(message, file_ops)
    return file_ops


def prepare_compaction(
    path_entries: Sequence[SessionTreeEntry], settings: Mapping[str, Any]
) -> Result[dict[str, Any] | None, CompactionError]:
    if not path_entries or path_entries[-1].get("type") == "compaction":
        return ok(None)
    previous_index = next(
        (index for index in range(len(path_entries) - 1, -1, -1) if path_entries[index].get("type") == "compaction"),
        -1,
    )
    previous_summary: str | None = None
    boundary_start = 0
    if previous_index >= 0:
        previous = path_entries[previous_index]
        previous_summary = previous["summary"]
        boundary_start = next(
            (index for index, entry in enumerate(path_entries) if entry.get("id") == previous.get("firstKeptEntryId")),
            previous_index + 1,
        )
    tokens_before = int(estimate_context_tokens(build_session_context(path_entries)["messages"])["tokens"] or 0)
    cut = find_cut_point(
        path_entries,
        boundary_start,
        len(path_entries),
        int(_setting(settings, "keep_recent_tokens", "keepRecentTokens")),
    )
    kept_index = int(cut["firstKeptEntryIndex"])
    kept_entry = path_entries[kept_index] if kept_index < len(path_entries) else None
    if not kept_entry or not kept_entry.get("id"):
        return err(CompactionError("invalid_session", "First kept entry has no UUID - session may need migration"))
    is_split = bool(cut["isSplitTurn"])
    history_end = int(cut["turnStartIndex"] if is_split else kept_index)
    messages_to_summarize = [
        message
        for entry in path_entries[boundary_start:history_end]
        if (message := _entry_message(entry, for_compaction=True)) is not None
    ]
    prefix_messages: list[AgentMessage] = []
    if is_split:
        prefix_messages = [
            message
            for entry in path_entries[int(cut["turnStartIndex"]) : kept_index]
            if (message := _entry_message(entry, for_compaction=True)) is not None
        ]
    file_ops = _extract_file_operations(messages_to_summarize, path_entries, previous_index)
    for message in prefix_messages:
        extract_file_ops_from_message(message, file_ops)
    return ok(
        {
            "firstKeptEntryId": kept_entry["id"],
            "messagesToSummarize": messages_to_summarize,
            "turnPrefixMessages": prefix_messages,
            "isSplitTurn": is_split,
            "tokensBefore": tokens_before,
            "previousSummary": previous_summary,
            "fileOps": file_ops,
            "settings": settings,
        }
    )


async def _generate_turn_prefix_summary(
    messages: Sequence[AgentMessage],
    models: Any,
    model: Any,
    reserve_tokens: int,
    signal: AbortSignal | None,
    thinking_level: str | None,
) -> Result[str, CompactionError]:
    model_max = int(_model_value(model, "max_tokens", "maxTokens", 0))
    max_tokens = min(math.floor(0.5 * reserve_tokens), model_max if model_max > 0 else math.inf)
    prompt = (
        f"<conversation>\n{serialize_conversation(convert_to_llm(list(messages)))}\n</conversation>\n\n"
        f"{TURN_PREFIX_SUMMARIZATION_PROMPT}"
    )
    context = {
        "systemPrompt": SUMMARIZATION_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}], "timestamp": now_ms()}],
    }
    options: dict[str, Any] = {"maxTokens": int(max_tokens), "signal": signal}
    if _model_value(model, "reasoning", "reasoning", False) and thinking_level and thinking_level != "off":
        options["reasoning"] = thinking_level
    response = await _complete_simple(models, model, context, options)
    reason = response.get("stopReason", response.get("stop_reason"))
    if reason == "aborted":
        return err(CompactionError("aborted", response.get("errorMessage") or "Turn prefix summarization aborted"))
    if reason == "error":
        return err(
            CompactionError(
                "summarization_failed",
                f"Turn prefix summarization failed: {response.get('errorMessage') or 'Unknown error'}",
            )
        )
    return ok(
        "\n".join(str(block.get("text", "")) for block in response.get("content", []) if block.get("type") == "text")
    )


async def compact(
    preparation: Mapping[str, Any],
    models: Any,
    model: Any,
    custom_instructions: str | None = None,
    signal: AbortSignal | None = None,
    thinking_level: str | None = None,
) -> Result[dict[str, Any], CompactionError]:
    kept_id = preparation.get("firstKeptEntryId")
    if not kept_id:
        return err(CompactionError("invalid_session", "First kept entry has no UUID - session may need migration"))
    settings = preparation["settings"]
    reserve = int(_setting(settings, "reserve_tokens", "reserveTokens"))
    history = preparation["messagesToSummarize"]
    prefix = preparation["turnPrefixMessages"]
    if preparation["isSplitTurn"] and prefix:
        history_result = (
            await generate_summary(
                history,
                models,
                model,
                reserve,
                signal,
                custom_instructions,
                preparation.get("previousSummary"),
                thinking_level,
            )
            if history
            else ok("No prior history.")
        )
        if not history_result["ok"]:
            return err(history_result["error"])
        prefix_result = await _generate_turn_prefix_summary(prefix, models, model, reserve, signal, thinking_level)
        if not prefix_result["ok"]:
            return err(prefix_result["error"])
        summary = f"{history_result['value']}\n\n---\n\n**Turn Context (split turn):**\n\n{prefix_result['value']}"
    else:
        result = await generate_summary(
            history,
            models,
            model,
            reserve,
            signal,
            custom_instructions,
            preparation.get("previousSummary"),
            thinking_level,
        )
        if not result["ok"]:
            return err(result["error"])
        summary = result["value"]
    files = compute_file_lists(preparation["fileOps"])
    summary += format_file_operations(files["readFiles"], files["modifiedFiles"])
    return ok(
        {
            "summary": summary,
            "firstKeptEntryId": kept_id,
            "tokensBefore": preparation["tokensBefore"],
            "details": files,
        }
    )


calculateContextTokens = calculate_context_tokens
getLastAssistantUsage = get_last_assistant_usage
estimateTokens = estimate_tokens
estimateContextTokens = estimate_context_tokens
shouldCompact = should_compact
findTurnStartIndex = find_turn_start_index
findCutPoint = find_cut_point
generateSummary = generate_summary
prepareCompaction = prepare_compaction
serializeConversation = serialize_conversation
