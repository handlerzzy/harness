"""Aster Harness: a Python agent runtime derived from Pi's agent core."""

from ._utils import JS_UNDEFINED
from .abort import AbortController, AbortSignal
from .agent import Agent, AgentOptions, AgentState
from .agent_loop import (
    agent_loop,
    agent_loop_continue,
    agentLoop,
    agentLoopContinue,
    run_agent_loop,
    run_agent_loop_continue,
    runAgentLoop,
    runAgentLoopContinue,
    stream_simple,
    streamSimple,
)
from .coding_tools import create_coding_tools
from .event_stream import (
    AssistantMessageEventStream,
    EventStream,
    create_assistant_message_event_stream,
    createAssistantMessageEventStream,
)
from .harness import *
from .harness import __all__ as _harness_all
from .models import Models
from .proxy import ProxyAssistantMessageEvent, ProxyMessageEventStream, ProxyStreamOptions, stream_proxy, streamProxy
from .schema import validate_tool_arguments, validateToolArguments
from .sdk import create_harness
from .types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEvent,
    AgentEventSink,
    AgentLoopConfig,
    AgentLoopTurnUpdate,
    AgentMessage,
    AgentTool,
    AgentToolCall,
    AgentToolResult,
    AgentToolUpdateCallback,
    BeforeToolCallContext,
    BeforeToolCallResult,
    CustomAgentMessages,
    Model,
    PrepareNextTurnContext,
    QueueMode,
    ShouldStopAfterTurnContext,
    StreamFn,
    ThinkingLevel,
    ToolExecutionMode,
)

__all__ = [
    "JS_UNDEFINED",
    "AbortController",
    "AbortSignal",
    "AfterToolCallContext",
    "AfterToolCallResult",
    "Agent",
    "AgentContext",
    "AgentEvent",
    "AgentEventSink",
    "AgentLoopConfig",
    "AgentLoopTurnUpdate",
    "AgentMessage",
    "AgentOptions",
    "AgentState",
    "AgentTool",
    "AgentToolCall",
    "AgentToolResult",
    "AgentToolUpdateCallback",
    "AssistantMessageEventStream",
    "BeforeToolCallContext",
    "BeforeToolCallResult",
    "CustomAgentMessages",
    "EventStream",
    "Model",
    "Models",
    "PrepareNextTurnContext",
    "ProxyAssistantMessageEvent",
    "ProxyMessageEventStream",
    "ProxyStreamOptions",
    "QueueMode",
    "ShouldStopAfterTurnContext",
    "StreamFn",
    "ThinkingLevel",
    "ToolExecutionMode",
    "agentLoop",
    "agentLoopContinue",
    "agent_loop",
    "agent_loop_continue",
    "createAssistantMessageEventStream",
    "create_assistant_message_event_stream",
    "create_coding_tools",
    "create_harness",
    "runAgentLoop",
    "runAgentLoopContinue",
    "run_agent_loop",
    "run_agent_loop_continue",
    "streamProxy",
    "streamSimple",
    "stream_proxy",
    "stream_simple",
    "validateToolArguments",
    "validate_tool_arguments",
]

__version__ = "0.1.0"

__all__ = sorted(set(__all__ + _harness_all))
