from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, Literal, Protocol, TypeAlias, TypedDict, TypeVar

from .._utils import js_json_stringify
from ..abort import AbortSignal

T = TypeVar("T")
E = TypeVar("E", bound=BaseException)

# Python-visible counterparts for TypeScript's erased public interfaces.  The
# runtime accepts ordinary mappings for these object-shaped contracts, so the
# aliases intentionally stay structural instead of introducing wrapper
# classes that would change call-site behavior.
AgentHarnessResources: TypeAlias = dict[str, Any]
AgentHarnessStreamOptions: TypeAlias = dict[str, Any]
AgentHarnessStreamOptionsPatch: TypeAlias = dict[str, Any]
FileKind: TypeAlias = Literal["file", "directory", "symlink"]
FileErrorCode: TypeAlias = Literal[
    "aborted",
    "not_found",
    "permission_denied",
    "not_directory",
    "is_directory",
    "invalid",
    "not_supported",
    "unknown",
]
ExecutionErrorCode: TypeAlias = Literal[
    "aborted", "timeout", "shell_unavailable", "spawn_error", "callback_error", "unknown"
]
CompactionErrorCode: TypeAlias = Literal["aborted", "summarization_failed", "invalid_session", "unknown"]
BranchSummaryErrorCode: TypeAlias = Literal["aborted", "summarization_failed", "invalid_session"]
SessionErrorCode: TypeAlias = Literal[
    "not_found", "invalid_session", "invalid_entry", "invalid_fork_target", "storage", "unknown"
]
AgentHarnessErrorCode: TypeAlias = Literal[
    "busy",
    "invalid_state",
    "invalid_argument",
    "session",
    "hook",
    "auth",
    "compaction",
    "branch_summary",
    "unknown",
]
FileInfo: TypeAlias = dict[str, Any]
ShellExecOptions: TypeAlias = dict[str, Any]
SessionTreeEntryBase: TypeAlias = dict[str, Any]
MessageEntry: TypeAlias = dict[str, Any]
ThinkingLevelChangeEntry: TypeAlias = dict[str, Any]
ModelChangeEntry: TypeAlias = dict[str, Any]
ActiveToolsChangeEntry: TypeAlias = dict[str, Any]
CompactionEntry: TypeAlias = dict[str, Any]
BranchSummaryEntry: TypeAlias = dict[str, Any]
CustomEntry: TypeAlias = dict[str, Any]
CustomMessageEntry: TypeAlias = dict[str, Any]
LabelEntry: TypeAlias = dict[str, Any]
SessionInfoEntry: TypeAlias = dict[str, Any]
LeafEntry: TypeAlias = dict[str, Any]
SessionTreeEntry: TypeAlias = dict[str, Any]
SessionContext: TypeAlias = dict[str, Any]
SessionMetadata: TypeAlias = dict[str, Any]
JsonlSessionMetadata: TypeAlias = dict[str, Any]
SessionCreateOptions: TypeAlias = dict[str, Any]
SessionForkOptions: TypeAlias = dict[str, Any]
JsonlSessionCreateOptions: TypeAlias = dict[str, Any]
JsonlSessionListOptions: TypeAlias = dict[str, Any]
AgentHarnessPhase: TypeAlias = Literal["idle", "turn", "compaction", "branch_summary", "retry"]
PendingSessionWrite: TypeAlias = dict[str, Any]
QueueUpdateEvent: TypeAlias = dict[str, Any]
SavePointEvent: TypeAlias = dict[str, Any]
AbortEvent: TypeAlias = dict[str, Any]
SettledEvent: TypeAlias = dict[str, Any]
BeforeAgentStartEvent: TypeAlias = dict[str, Any]
ContextEvent: TypeAlias = dict[str, Any]
BeforeProviderRequestEvent: TypeAlias = dict[str, Any]
BeforeProviderPayloadEvent: TypeAlias = dict[str, Any]
AfterProviderResponseEvent: TypeAlias = dict[str, Any]
ToolCallEvent: TypeAlias = dict[str, Any]
ToolResultEvent: TypeAlias = dict[str, Any]
SessionBeforeCompactEvent: TypeAlias = dict[str, Any]
SessionCompactEvent: TypeAlias = dict[str, Any]
SessionBeforeTreeEvent: TypeAlias = dict[str, Any]
SessionTreeEvent: TypeAlias = dict[str, Any]
ModelUpdateEvent: TypeAlias = dict[str, Any]
ThinkingLevelUpdateEvent: TypeAlias = dict[str, Any]
ToolsUpdateEvent: TypeAlias = dict[str, Any]
ResourcesUpdateEvent: TypeAlias = dict[str, Any]
AgentHarnessOwnEvent: TypeAlias = dict[str, Any]
AgentHarnessEvent: TypeAlias = dict[str, Any]
BeforeAgentStartResult: TypeAlias = dict[str, Any]
ContextResult: TypeAlias = dict[str, Any]
BeforeProviderRequestResult: TypeAlias = dict[str, Any]
BeforeProviderPayloadResult: TypeAlias = dict[str, Any]
ToolCallResult: TypeAlias = dict[str, Any]
ToolResultPatch: TypeAlias = dict[str, Any]
SessionBeforeCompactResult: TypeAlias = dict[str, Any]
SessionBeforeTreeResult: TypeAlias = dict[str, Any]
AgentHarnessEventResultMap: TypeAlias = dict[str, Any]
AgentHarnessPromptOptions: TypeAlias = dict[str, Any]
AbortResult: TypeAlias = dict[str, Any]
CompactResult: TypeAlias = dict[str, Any]
NavigateTreeResult: TypeAlias = dict[str, Any]
CompactionSettings: TypeAlias = dict[str, Any]
CompactionPreparation: TypeAlias = dict[str, Any]
FileOperations: TypeAlias = dict[str, set[str]]
TreePreparation: TypeAlias = dict[str, Any]
GenerateBranchSummaryOptions: TypeAlias = dict[str, Any]
BranchSummaryResult: TypeAlias = dict[str, Any]
AgentHarnessOptions: TypeAlias = dict[str, Any]

# These three types are exported by other source modules upstream but are
# re-exported from the package root.  Keeping their names here makes wildcard
# harness imports mirror that root surface as well.
BranchPreparation: TypeAlias = dict[str, Any]
BranchSummaryDetails: TypeAlias = dict[str, Any]
CollectEntriesResult: TypeAlias = dict[str, Any]


class OkResult(TypedDict, Generic[T]):
    ok: Literal[True]
    value: T


class ErrResult(TypedDict, Generic[E]):
    ok: Literal[False]
    error: E


Result: TypeAlias = OkResult[T] | ErrResult[E]


def ok(value: T) -> OkResult[T]:
    return {"ok": True, "value": value}


def err(error: E) -> ErrResult[E]:
    return {"ok": False, "error": error}


def get_or_throw(result: Result[T, E]) -> T:
    if not result["ok"]:
        raise result["error"]
    return result["value"]


def get_or_undefined(result: Result[T, E]) -> T | None:
    return result["value"] if result["ok"] else None


def to_error(error: object) -> Exception:
    if isinstance(error, Exception):
        return error
    if isinstance(error, BaseException):
        return RuntimeError(str(error))
    if isinstance(error, str):
        return RuntimeError(error)
    try:
        return RuntimeError(js_json_stringify(error) or "")
    except (TypeError, ValueError):
        return RuntimeError(str(error))


class CodedError(Exception):
    def __init__(self, code: str, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.name = type(self).__name__
        self.code = code
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause


class FileError(CodedError):
    def __init__(self, code: str, message: str, path: str | None = None, cause: BaseException | None = None) -> None:
        super().__init__(code, message, cause)
        self.path = path


class ExecutionError(CodedError):
    pass


class CompactionError(CodedError):
    pass


class BranchSummaryError(CodedError):
    pass


class SessionError(CodedError):
    pass


class AgentHarnessError(CodedError):
    pass


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    content: str
    file_path: str
    disable_model_invocation: bool = False

    @property
    def filePath(self) -> str:
        return self.file_path

    @property
    def disableModelInvocation(self) -> bool:
        return self.disable_model_invocation


@dataclass(slots=True)
class PromptTemplate:
    name: str
    content: str
    description: str | None = None


class SessionStorage(Protocol):
    async def get_metadata(self) -> SessionMetadata: ...

    async def get_leaf_id(self) -> str | None: ...

    async def set_leaf_id(self, leaf_id: str | None) -> None: ...

    async def create_entry_id(self) -> str: ...

    async def append_entry(self, entry: SessionTreeEntry) -> None: ...

    async def get_entry(self, entry_id: str) -> SessionTreeEntry | None: ...

    async def find_entries(self, entry_type: str) -> list[SessionTreeEntry]: ...

    async def get_label(self, entry_id: str) -> str | None: ...

    async def get_path_to_root(self, leaf_id: str | None) -> list[SessionTreeEntry]: ...

    async def get_entries(self) -> list[SessionTreeEntry]: ...


class ExecutionEnv(Protocol):
    cwd: str

    async def absolute_path(self, path: str, abort_signal: AbortSignal | None = None) -> Result[str, FileError]: ...

    async def join_path(self, parts: list[str], abort_signal: AbortSignal | None = None) -> Result[str, FileError]: ...

    async def read_text_file(self, path: str, abort_signal: AbortSignal | None = None) -> Result[str, FileError]: ...

    async def read_text_lines(
        self, path: str, options: Mapping[str, Any] | None = None
    ) -> Result[list[str], FileError]: ...

    async def read_binary_file(
        self, path: str, abort_signal: AbortSignal | None = None
    ) -> Result[bytes, FileError]: ...

    async def write_file(
        self, path: str, content: str | bytes, abort_signal: AbortSignal | None = None
    ) -> Result[None, FileError]: ...

    async def append_file(
        self, path: str, content: str | bytes, abort_signal: AbortSignal | None = None
    ) -> Result[None, FileError]: ...

    async def list_dir(
        self, path: str, abort_signal: AbortSignal | None = None
    ) -> Result[list[dict[str, Any]], FileError]: ...

    async def file_info(self, path: str, abort_signal: AbortSignal | None = None) -> Result[FileInfo, FileError]: ...

    async def canonical_path(self, path: str, abort_signal: AbortSignal | None = None) -> Result[str, FileError]: ...

    async def exists(self, path: str, abort_signal: AbortSignal | None = None) -> Result[bool, FileError]: ...

    async def create_dir(self, path: str, options: Mapping[str, Any] | None = None) -> Result[None, FileError]: ...

    async def remove(self, path: str, options: Mapping[str, Any] | None = None) -> Result[None, FileError]: ...

    async def create_temp_dir(
        self, prefix: str = "tmp-", abort_signal: AbortSignal | None = None
    ) -> Result[str, FileError]: ...

    async def create_temp_file(self, options: Mapping[str, Any] | None = None) -> Result[str, FileError]: ...

    async def exec(
        self, command: str, options: Mapping[str, Any] | None = None
    ) -> Result[dict[str, Any], ExecutionError]: ...

    async def cleanup(self) -> None: ...


FileSystem: TypeAlias = ExecutionEnv
Shell: TypeAlias = ExecutionEnv
SessionRepo: TypeAlias = Any
JsonlSessionRepoApi: TypeAlias = Any


AgentHarnessListener: TypeAlias = Callable[[dict[str, Any]], Any | Awaitable[Any]]


# TypeScript-compatible aliases.
getOrThrow = get_or_throw
getOrUndefined = get_or_undefined
toError = to_error

__all__ = [
    "AbortEvent",
    "AbortResult",
    "ActiveToolsChangeEntry",
    "AfterProviderResponseEvent",
    "AgentHarnessError",
    "AgentHarnessErrorCode",
    "AgentHarnessEvent",
    "AgentHarnessEventResultMap",
    "AgentHarnessListener",
    "AgentHarnessOptions",
    "AgentHarnessOwnEvent",
    "AgentHarnessPhase",
    "AgentHarnessPromptOptions",
    "AgentHarnessResources",
    "AgentHarnessStreamOptions",
    "AgentHarnessStreamOptionsPatch",
    "BeforeAgentStartEvent",
    "BeforeAgentStartResult",
    "BeforeProviderPayloadEvent",
    "BeforeProviderPayloadResult",
    "BeforeProviderRequestEvent",
    "BeforeProviderRequestResult",
    "BranchPreparation",
    "BranchSummaryDetails",
    "BranchSummaryEntry",
    "BranchSummaryError",
    "BranchSummaryErrorCode",
    "BranchSummaryResult",
    "CodedError",
    "CollectEntriesResult",
    "CompactResult",
    "CompactionEntry",
    "CompactionError",
    "CompactionErrorCode",
    "CompactionPreparation",
    "CompactionSettings",
    "ContextEvent",
    "ContextResult",
    "CustomEntry",
    "CustomMessageEntry",
    "ErrResult",
    "ExecutionEnv",
    "ExecutionError",
    "ExecutionErrorCode",
    "FileError",
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
    "OkResult",
    "PendingSessionWrite",
    "PromptTemplate",
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
    "SessionCreateOptions",
    "SessionError",
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
    "ShellExecOptions",
    "Skill",
    "ThinkingLevelChangeEntry",
    "ThinkingLevelUpdateEvent",
    "ToolCallEvent",
    "ToolCallResult",
    "ToolResultEvent",
    "ToolResultPatch",
    "ToolsUpdateEvent",
    "TreePreparation",
    "err",
    "getOrThrow",
    "getOrUndefined",
    "get_or_throw",
    "get_or_undefined",
    "ok",
    "toError",
    "to_error",
]
