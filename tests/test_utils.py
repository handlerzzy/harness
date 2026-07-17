from __future__ import annotations

import math

import pytest

from pi_agent_core._utils import (
    js_json_stringify,
    js_number_to_string,
    js_object_items,
    js_to_fixed,
    js_utf16_length,
    js_utf16_slice,
    js_utf16_sort_key,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (-0.0, "0"),
        (1.5, "1.5"),
        (1e-7, "1e-7"),
        (1e-6, "0.000001"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1.2345678901234568e18, "1234567890123456800"),
        (9_007_199_254_740_993, "9007199254740992"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (math.nan, "NaN"),
        (math.inf, "Infinity"),
        (-math.inf, "-Infinity"),
    ],
)
def test_js_number_format_matches_ecmascript(value: int | float, expected: str) -> None:
    assert js_number_to_string(value) == expected


def test_json_stringify_matches_javascript_order_undefined_numbers_surrogates_and_spacing() -> None:
    value = {
        "b": 1,
        "10": "ten",
        "2": "two",
        "01": "zero-one",
        "a": [1, lambda: None, math.nan, {"z": 2}],
        "omitted": lambda: None,
        "\ud800": "\ud800",
    }
    assert js_json_stringify(value) == (
        '{"2":"two","10":"ten","b":1,"01":"zero-one","a":[1,null,null,{"z":2}],"\\ud800":"\\ud800"}'
    )
    assert js_json_stringify({"a": [1, {"z": 2}]}, indent=2) == (
        '{\n  "a": [\n    1,\n    {\n      "z": 2\n    }\n  ]\n}'
    )
    assert js_json_stringify(lambda: None) is None
    circular: list[object] = []
    circular.append(circular)
    with pytest.raises(TypeError, match="circular structure"):
        js_json_stringify(circular)


def test_js_property_and_utf16_helpers_match_ecmascript() -> None:
    assert js_object_items({"b": 1, "10": 2, "2": 3, "01": 4}) == [
        ("2", 3),
        ("10", 2),
        ("b", 1),
        ("01", 4),
    ]
    assert js_utf16_length("a😀b") == 4
    assert js_utf16_slice("a😀b", 1, 3) == "😀"
    assert sorted(["\ue000", "😀"], key=js_utf16_sort_key) == ["😀", "\ue000"]
    assert js_to_fixed(1.25, 1) == "1.3"


def test_node_entrypoint_reexports_the_core_runtime() -> None:
    from pi_agent_core.node import Agent, NodeExecutionEnv, runAgentLoop

    assert Agent is not None
    assert NodeExecutionEnv is not None
    assert runAgentLoop is not None


def test_all_upstream_executable_exports_are_available() -> None:
    import pi_agent_core

    expected = {
        "Agent",
        "AgentHarness",
        "BranchSummaryError",
        "CompactionError",
        "DEFAULT_COMPACTION_SETTINGS",
        "DEFAULT_MAX_BYTES",
        "DEFAULT_MAX_LINES",
        "ExecutionError",
        "FileError",
        "GREP_MAX_LINE_LENGTH",
        "InMemorySessionRepo",
        "InMemorySessionStorage",
        "JsonlSessionRepo",
        "JsonlSessionStorage",
        "Session",
        "SessionError",
        "agentLoop",
        "agentLoopContinue",
        "bashExecutionToText",
        "buildContextEntries",
        "buildSessionContext",
        "calculateContextTokens",
        "collectEntriesForBranchSummary",
        "compact",
        "convertToLlm",
        "createBranchSummaryMessage",
        "createCompactionSummaryMessage",
        "createCustomMessage",
        "createSessionId",
        "createTimestamp",
        "defaultContextEntryTransform",
        "err",
        "estimateContextTokens",
        "estimateTokens",
        "executeShellWithCapture",
        "findCutPoint",
        "findTurnStartIndex",
        "formatPromptTemplateInvocation",
        "formatSize",
        "formatSkillInvocation",
        "formatSkillsForSystemPrompt",
        "generateBranchSummary",
        "generateSummary",
        "getEntriesToFork",
        "getFileSystemResultOrThrow",
        "getLastAssistantUsage",
        "getOrThrow",
        "getOrUndefined",
        "loadJsonlSessionMetadata",
        "loadPromptTemplates",
        "loadSkills",
        "loadSourcedPromptTemplates",
        "loadSourcedSkills",
        "ok",
        "parseCommandArgs",
        "prepareBranchEntries",
        "prepareCompaction",
        "runAgentLoop",
        "runAgentLoopContinue",
        "sanitizeBinaryOutput",
        "serializeConversation",
        "sessionEntryToContextMessages",
        "shouldCompact",
        "streamProxy",
        "substituteArgs",
        "toError",
        "toSession",
        "truncateHead",
        "truncateLine",
        "truncateTail",
        "uuidv7",
    }
    assert expected <= set(pi_agent_core.__all__)
    assert all(hasattr(pi_agent_core, name) for name in expected)


def test_all_upstream_public_type_names_are_importable() -> None:
    import pi_agent_core

    expected = {
        "AbortEvent",
        "AbortResult",
        "ActiveToolsChangeEntry",
        "AfterProviderResponseEvent",
        "AfterToolCallContext",
        "AfterToolCallResult",
        "AgentContext",
        "AgentEvent",
        "AgentEventSink",
        "AgentHarnessErrorCode",
        "AgentHarnessEvent",
        "AgentHarnessEventResultMap",
        "AgentHarnessOptions",
        "AgentHarnessOwnEvent",
        "AgentHarnessPhase",
        "AgentHarnessPromptOptions",
        "AgentHarnessResources",
        "AgentHarnessStreamOptions",
        "AgentHarnessStreamOptionsPatch",
        "AgentLoopConfig",
        "AgentLoopTurnUpdate",
        "AgentMessage",
        "AgentOptions",
        "AgentState",
        "AgentTool",
        "AgentToolCall",
        "AgentToolResult",
        "AgentToolUpdateCallback",
        "BashExecutionMessage",
        "BeforeAgentStartEvent",
        "BeforeAgentStartResult",
        "BeforeProviderPayloadEvent",
        "BeforeProviderPayloadResult",
        "BeforeProviderRequestEvent",
        "BeforeProviderRequestResult",
        "BeforeToolCallContext",
        "BeforeToolCallResult",
        "BranchPreparation",
        "BranchSummaryDetails",
        "BranchSummaryEntry",
        "BranchSummaryErrorCode",
        "BranchSummaryMessage",
        "BranchSummaryResult",
        "CollectEntriesResult",
        "CompactResult",
        "CompactionEntry",
        "CompactionErrorCode",
        "CompactionPreparation",
        "CompactionSettings",
        "CompactionSummaryMessage",
        "ContextEntryTransform",
        "ContextEvent",
        "ContextResult",
        "CustomAgentMessages",
        "CustomEntry",
        "CustomEntryContextMessageProjector",
        "CustomMessage",
        "CustomMessageEntry",
        "ExecutionEnv",
        "ExecutionErrorCode",
        "FileErrorCode",
        "FileInfo",
        "FileKind",
        "FileOperations",
        "FileSystem",
        "GenerateBranchSummaryOptions",
        "JsonlSessionCreateOptions",
        "JsonlSessionListOptions",
        "JsonlSessionMetadata",
        "JsonlSessionRepoApi",
        "LabelEntry",
        "LeafEntry",
        "MessageEntry",
        "ModelChangeEntry",
        "ModelUpdateEvent",
        "NavigateTreeResult",
        "PendingSessionWrite",
        "PrepareNextTurnContext",
        "PromptTemplateDiagnostic",
        "PromptTemplateDiagnosticCode",
        "ProxyAssistantMessageEvent",
        "ProxyStreamOptions",
        "QueueMode",
        "QueueUpdateEvent",
        "ResourcesUpdateEvent",
        "Result",
        "SavePointEvent",
        "SessionBeforeCompactEvent",
        "SessionBeforeCompactResult",
        "SessionBeforeTreeEvent",
        "SessionBeforeTreeResult",
        "SessionCompactEvent",
        "SessionContext",
        "SessionContextBuildOptions",
        "SessionCreateOptions",
        "SessionErrorCode",
        "SessionForkOptions",
        "SessionInfoEntry",
        "SessionMetadata",
        "SessionRepo",
        "SessionStorage",
        "SessionTreeEntry",
        "SessionTreeEntryBase",
        "SessionTreeEvent",
        "SettledEvent",
        "Shell",
        "ShellCaptureOptions",
        "ShellCaptureResult",
        "ShellExecOptions",
        "ShouldStopAfterTurnContext",
        "SkillDiagnostic",
        "SkillDiagnosticCode",
        "StreamFn",
        "ThinkingLevel",
        "ThinkingLevelChangeEntry",
        "ThinkingLevelUpdateEvent",
        "ToolCallEvent",
        "ToolCallResult",
        "ToolExecutionMode",
        "ToolResultEvent",
        "ToolResultPatch",
        "ToolsUpdateEvent",
        "TreePreparation",
        "TruncationOptions",
        "TruncationResult",
    }
    assert expected <= set(pi_agent_core.__all__)
    assert all(hasattr(pi_agent_core, name) for name in expected)
