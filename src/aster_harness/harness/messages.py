from __future__ import annotations

from typing import Any, TypeAlias

from .._utils import js_date_parse_ms
from ..types import AgentMessage

COMPACTION_SUMMARY_PREFIX = """The conversation history before this point was compacted into the following summary:

<summary>
"""
COMPACTION_SUMMARY_SUFFIX = "\n</summary>"
BRANCH_SUMMARY_PREFIX = """The following is a summary of a branch that this conversation came back from:

<summary>
"""
BRANCH_SUMMARY_SUFFIX = "</summary>"

BashExecutionMessage: TypeAlias = AgentMessage
CustomMessage: TypeAlias = AgentMessage
BranchSummaryMessage: TypeAlias = AgentMessage
CompactionSummaryMessage: TypeAlias = AgentMessage


def _timestamp_ms(value: str) -> int | float:
    return js_date_parse_ms(value)


def bash_execution_to_text(message: AgentMessage) -> str:
    text = f"Ran `{message['command']}`\n"
    output = message.get("output")
    text += f"```\n{output}\n```" if output else "(no output)"
    if message.get("cancelled"):
        text += "\n\n(command cancelled)"
    elif message.get("exitCode") is not None and message.get("exitCode") != 0:
        text += f"\n\nCommand exited with code {message['exitCode']}"
    if message.get("truncated") and message.get("fullOutputPath"):
        text += f"\n\n[Output truncated. Full output: {message['fullOutputPath']}]"
    return text


def create_branch_summary_message(summary: str, from_id: str, timestamp: str) -> AgentMessage:
    return {"role": "branchSummary", "summary": summary, "fromId": from_id, "timestamp": _timestamp_ms(timestamp)}


def create_compaction_summary_message(summary: str, tokens_before: int, timestamp: str) -> AgentMessage:
    return {
        "role": "compactionSummary",
        "summary": summary,
        "tokensBefore": tokens_before,
        "timestamp": _timestamp_ms(timestamp),
    }


def create_custom_message(
    custom_type: str,
    content: str | list[dict[str, Any]],
    display: bool,
    details: Any,
    timestamp: str,
) -> AgentMessage:
    return {
        "role": "custom",
        "customType": custom_type,
        "content": content,
        "display": display,
        "details": details,
        "timestamp": _timestamp_ms(timestamp),
    }


def convert_to_llm(messages: list[AgentMessage]) -> list[AgentMessage]:
    converted: list[AgentMessage] = []
    for message in messages:
        role = message.get("role")
        if role == "bashExecution":
            if message.get("excludeFromContext"):
                continue
            converted.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": bash_execution_to_text(message)}],
                    "timestamp": message["timestamp"],
                }
            )
        elif role == "custom":
            content = message.get("content", "")
            converted.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": content}] if isinstance(content, str) else content,
                    "timestamp": message["timestamp"],
                }
            )
        elif role == "branchSummary":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": BRANCH_SUMMARY_PREFIX + str(message["summary"]) + BRANCH_SUMMARY_SUFFIX,
                        }
                    ],
                    "timestamp": message["timestamp"],
                }
            )
        elif role == "compactionSummary":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": COMPACTION_SUMMARY_PREFIX + str(message["summary"]) + COMPACTION_SUMMARY_SUFFIX,
                        }
                    ],
                    "timestamp": message["timestamp"],
                }
            )
        elif role in {"user", "assistant", "toolResult"}:
            converted.append(message)
    return converted


bashExecutionToText = bash_execution_to_text
createBranchSummaryMessage = create_branch_summary_message
createCompactionSummaryMessage = create_compaction_summary_message
createCustomMessage = create_custom_message
convertToLlm = convert_to_llm

__all__ = [
    "BRANCH_SUMMARY_PREFIX",
    "BRANCH_SUMMARY_SUFFIX",
    "COMPACTION_SUMMARY_PREFIX",
    "COMPACTION_SUMMARY_SUFFIX",
    "BashExecutionMessage",
    "BranchSummaryMessage",
    "CompactionSummaryMessage",
    "CustomMessage",
    "bashExecutionToText",
    "bash_execution_to_text",
    "convertToLlm",
    "convert_to_llm",
    "createBranchSummaryMessage",
    "createCompactionSummaryMessage",
    "createCustomMessage",
    "create_branch_summary_message",
    "create_compaction_summary_message",
    "create_custom_message",
]
