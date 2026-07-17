from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias

from ..._utils import get_any_field, maybe_await, now_ms
from ...abort import AbortSignal
from ...types import AgentMessage
from ..messages import (
    convert_to_llm,
    create_branch_summary_message,
    create_compaction_summary_message,
    create_custom_message,
)
from ..types import BranchSummaryError, Result, SessionError, SessionTreeEntry, err, ok
from .compaction import SUMMARIZATION_SYSTEM_PROMPT, estimate_tokens
from .utils import (
    compute_file_lists,
    create_file_ops,
    extract_file_ops_from_message,
    format_file_operations,
    serialize_conversation,
)

BRANCH_SUMMARY_PREAMBLE = """The user explored a different conversation branch before returning here.
Summary of that exploration:

"""
BRANCH_SUMMARY_PROMPT = """Create a structured summary of this conversation branch for context when returning later.

Use this EXACT format:

## Goal
[What was the user trying to accomplish in this branch?]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Work that was started but not finished]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [What should happen next to continue this work]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

BranchSummaryDetails: TypeAlias = dict[str, Any]
BranchPreparation: TypeAlias = dict[str, Any]
CollectEntriesResult: TypeAlias = dict[str, Any]
GenerateBranchSummaryOptions: TypeAlias = dict[str, Any]


def _entry_message(entry: SessionTreeEntry) -> AgentMessage | None:
    entry_type = entry.get("type")
    if entry_type == "message":
        return None if entry.get("message", {}).get("role") == "toolResult" else entry["message"]
    if entry_type == "custom_message":
        return create_custom_message(
            entry["customType"], entry["content"], bool(entry["display"]), entry.get("details"), entry["timestamp"]
        )
    if entry_type == "branch_summary":
        return create_branch_summary_message(entry["summary"], entry["fromId"], entry["timestamp"])
    if entry_type == "compaction":
        return create_compaction_summary_message(entry["summary"], entry["tokensBefore"], entry["timestamp"])
    return None


async def collect_entries_for_branch_summary(session: Any, old_leaf_id: str | None, target_id: str) -> dict[str, Any]:
    if not old_leaf_id:
        return {"entries": [], "commonAncestorId": None}
    old_path = {entry["id"] for entry in await session.get_branch(old_leaf_id)}
    target_path = await session.get_branch(target_id)
    common: str | None = None
    for entry in reversed(target_path):
        if entry["id"] in old_path:
            common = entry["id"]
            break
    entries: list[SessionTreeEntry] = []
    current: str | None = old_leaf_id
    while current and current != common:
        entry = await session.get_entry(current)
        if entry is None:
            raise SessionError("invalid_session", f"Entry {current} not found")
        entries.append(entry)
        current = entry.get("parentId")
    entries.reverse()
    return {"entries": entries, "commonAncestorId": common}


def prepare_branch_entries(entries: Sequence[SessionTreeEntry], token_budget: int = 0) -> dict[str, Any]:
    messages: list[AgentMessage] = []
    file_ops = create_file_ops()
    total_tokens = 0
    for entry in entries:
        if (
            entry.get("type") != "branch_summary"
            or entry.get("fromHook")
            or not isinstance(entry.get("details"), Mapping)
        ):
            continue
        details = entry["details"]
        read_files = details.get("readFiles")
        if isinstance(read_files, list):
            file_ops["read"].update(path for path in read_files if isinstance(path, str))
        modified_files = details.get("modifiedFiles")
        if isinstance(modified_files, list):
            file_ops["edited"].update(path for path in modified_files if isinstance(path, str))
    for entry in reversed(entries):
        message = _entry_message(entry)
        if message is None:
            continue
        extract_file_ops_from_message(message, file_ops)
        tokens = estimate_tokens(message)
        if token_budget > 0 and total_tokens + tokens > token_budget:
            if entry.get("type") in {"compaction", "branch_summary"} and total_tokens < token_budget * 0.9:
                messages.insert(0, message)
                total_tokens += tokens
            break
        messages.insert(0, message)
        total_tokens += tokens
    return {"messages": messages, "fileOps": file_ops, "totalTokens": total_tokens}


async def generate_branch_summary(
    entries: Sequence[SessionTreeEntry], options: Mapping[str, Any]
) -> Result[dict[str, Any], BranchSummaryError]:
    models = options["models"]
    model = options["model"]
    signal: AbortSignal = options["signal"]
    reserve = int(options.get("reserveTokens", options.get("reserve_tokens", 16384)))
    context_window = int(get_any_field(model, "context_window", "contextWindow", default=128000) or 128000)
    prepared = prepare_branch_entries(entries, context_window - reserve)
    messages = prepared["messages"]
    if not messages:
        return ok({"summary": "No content to summarize", "readFiles": [], "modifiedFiles": []})
    custom = options.get("customInstructions", options.get("custom_instructions"))
    if options.get("replaceInstructions", options.get("replace_instructions")) and custom:
        instructions = custom
    elif custom:
        instructions = f"{BRANCH_SUMMARY_PROMPT}\n\nAdditional focus: {custom}"
    else:
        instructions = BRANCH_SUMMARY_PROMPT
    prompt = f"<conversation>\n{serialize_conversation(convert_to_llm(messages))}\n</conversation>\n\n{instructions}"
    method = get_any_field(models, "complete_simple", "completeSimple")
    if method is None:
        raise TypeError("models must provide complete_simple() or completeSimple()")
    response = await maybe_await(
        method(
            model,
            {
                "systemPrompt": SUMMARIZATION_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}], "timestamp": now_ms()}],
            },
            {"signal": signal, "maxTokens": 2048},
        )
    )
    reason = response.get("stopReason", response.get("stop_reason"))
    if reason == "aborted":
        return err(BranchSummaryError("aborted", response.get("errorMessage") or "Branch summary aborted"))
    if reason == "error":
        return err(
            BranchSummaryError(
                "summarization_failed",
                f"Branch summary failed: {response.get('errorMessage') or 'Unknown error'}",
            )
        )
    summary = BRANCH_SUMMARY_PREAMBLE + "\n".join(
        str(block.get("text", "")) for block in response.get("content", []) if block.get("type") == "text"
    )
    files = compute_file_lists(prepared["fileOps"])
    summary += format_file_operations(files["readFiles"], files["modifiedFiles"])
    return ok({"summary": summary or "No summary generated", **files})


collectEntriesForBranchSummary = collect_entries_for_branch_summary
prepareBranchEntries = prepare_branch_entries
generateBranchSummary = generate_branch_summary
