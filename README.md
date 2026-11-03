# pi-agent-core-py

A Python 3.11+ rewrite of
[`@earendil-works/pi-agent-core`](https://github.com/earendil-works/pi/tree/main/packages/agent).
It implements the runtime behavior owned by the upstream `packages/agent`
package: the stateful agent loop, streamed lifecycle events, parallel and
sequential tool execution, steering and follow-up queues, the higher-level
harness, tree-shaped sessions, JSONL persistence, compaction, skills, prompt
templates, local execution, and the HTTP proxy stream.

The port is pinned to upstream package `0.80.10` at commit
[`3da591a`](https://github.com/earendil-works/pi/commit/3da591ab74ab9ab407e72ed882600b2c851fae21).
See [UPSTREAM.md](UPSTREAM.md) for provenance and [PARITY.md](PARITY.md) for the
module-by-module map and language-level adaptations.

## Install

From GitHub:

```bash
python -m pip install "git+https://github.com/wesleyzhangwq/pi-agent-core-py.git"
```

For development:

```bash
git clone https://github.com/wesleyzhangwq/pi-agent-core-py.git
cd pi-agent-core-py
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Quick start

Configure a Chat Completions endpoint and use the public SDK entry point:

```python
import asyncio
import os

from pi_agent_core import create_harness


async def main():
    harness = create_harness(
        api_key=os.environ["MODEL_API_KEY"],
        base_url="https://api.openai.com/v1",  # API root, without /chat/completions
        model_id="gpt-4o-mini",
        context_window=128_000,
        max_tokens=2048,
        system_prompt="You are a helpful assistant.",
        cwd=".",
        temperature=0.2,  # optional; omitted by default
        timeout_ms=60_000,
    )

    def on_event(event):
        if event["type"] == "message_update":
            update = event["assistantMessageEvent"]
            if update["type"] == "text_delta":
                print(update["delta"], end="", flush=True)

    unsubscribe = harness.subscribe(on_event)
    result = await harness.prompt("Hello!")
    print("\n", result["stopReason"])
    await harness.prompt("What did I just say?")  # reuses the in-memory Session
    unsubscribe()


asyncio.run(main())
```

`prompt()` returns an assistant message, including on provider failure. Inspect
`stopReason`: `stop` is normal completion, `length` is the output limit, `error`
includes `errorMessage`, and `aborted` is cancellation. Agent subscribers observe
`message_update` events carrying shared model events, followed by `message_end`,
`turn_end`, `agent_end`, and Harness `settled`. Incremental `partial` messages are
snapshots; concatenated text deltas match the final text. Establishment failures
can produce an error without a model `start` event.

The adapter recognizes optional `reasoning_content`, response ID, routed
model ID, and token usage. Absent fields stay absent. In particular, missing
`usage` means unknown, while explicit zero counts mean zero; individual unknown
counts and costs are not filled with zeros. `usage.input` excludes explicitly
reported cached tokens, and `usage.totalTokens` retains the provider's count.
No pricing or synthetic thinking is added.

`harness.models.stream_simple(model, context, options)` returns an
`AssistantMessageEventStream`; await `stream.result()` for its final message.
`harness.models.complete_simple(...)` obtains the result from the same streaming
implementation. For example, summary callers can use:

```python
result = await harness.models.complete_simple(
    harness.get_model(),
    {"systemPrompt": "Summarize briefly.", "messages": [{"role": "user", "content": "..."}]},
    {"maxTokens": 256, "temperature": 0},
)
```

The model access class `Models` is also exported for explicit assembly with an
existing Agent or Harness. Existing injected `stream_fn`, `stream_proxy`, and
Harness APIs continue to work. Model dictionaries, messages, and stream option
mappings retain the existing camelCase field names. There is no API migration.

Pass `session=Session(...)` to reuse a Session through the existing storage API;
otherwise history stays in memory. API keys stay on the model access object,
not in the model dictionary or Session. Pass `http_client=httpx.AsyncClient(...)`
to control the HTTP transport, including offline tests. The caller owns that
client and must close it; without one the adapter closes its own client per
model call. The 60-second default bounds each HTTP attempt, including response
reading; `timeout_ms` overrides it and must be finite and positive.

Chat Completions retries at most twice (three attempts total), waiting 100 ms
and 200 ms by default. Only HTTP 408, 429, 500, 502, 503, 504, timeouts, network
failures, and remote protocol disconnects are eligible, and only before any model
event (including `start`) has been published. Credential/configuration errors,
other HTTP statuses, malformed model responses, and cancellation are not retried.
Once generation starts, a failure preserves the partial content with
`stopReason="error"`; it never joins a second attempt onto those deltas. Retries
repeat the current request with its completed tool results, never the Agent task
or its already executed tools. This does not promise deduplication of work inside
the remote model service.

Configure this through the existing Harness options:

```python
await harness.set_stream_options({"maxRetries": 1, "maxRetryDelayMs": 500})
# Set maxRetries to 0 to disable retries.
```

`Models.stream_simple` and `complete_simple` accept the same options directly.
`maxRetries` is a non-negative integer; `maxRetryDelayMs` is a finite non-negative
cap on exponential waits starting at 100 ms (default cap: 1,000 ms). Server
`Retry-After` headers do not extend these waits. Timeouts apply per attempt, so
the default HTTP work for one model call is bounded by approximately 180.3 seconds
including retries; cancellation also interrupts the backoff wait.

For concurrent cancellation, run `prompt()` in an asyncio task, then
`await harness.abort()` from another task; await the prompt task to obtain the
`aborted` result. Cancelling a subscriber alone does not cancel the provider.
For direct model calls, pass an `AbortController().signal` in options and call
that controller's `abort()`. Custom tools receive the same active `signal`:
check `signal.throw_if_aborted()` before work and between awaitable steps, or
use `signal.wait()` / `signal.add_callback(...)` to interrupt pending work. Release
resources in `finally`; synchronous blocking callbacks cannot be interrupted by
the event loop. Cancellation starts no further requests or tool callbacks.
Unstarted calls receive error tool results to keep history paired, and the final
assistant result is `aborted`. Request failures use assistant `stopReason="error"`;
ordinary tool failures use tool-result / `tool_execution_end.isError=True` and
can be handled by a subsequent model turn.

The SDK supports text, custom tool calls/results, and optional thinking.
Images, additional provider-specific extensions, and automatic context management
are outside this entry point's current scope; `context_window` records the
explicit model limit for later context management. A premature EOF without a
legal `finish_reason` is an error, even after a `[DONE]` marker. A legal finish
followed by EOF is accepted. Malformed JSON, HTTP failures, and timeouts terminate
both event iteration and result waiting.
The existing proxy likewise reports EOF without a terminal event as an error;
it closes the response after a terminal event and settles both waiters, including
on cancellation. It does not add proxy retries or a proxy server.

Provider request, payload, response, context, and lifecycle hooks use the existing
Harness paths. For compatible endpoints needing `max_tokens` instead of the
default `max_completion_tokens`, adapt the payload at that boundary:

```python
def legacy_token_limit(event):
    payload = event["payload"]
    payload["max_tokens"] = payload.pop("max_completion_tokens")
    return {"payload": payload}


harness.on("before_provider_payload", legacy_token_limit)
```

### Injected provider example

For custom protocols, inject a `stream_fn`, attach one to the model as
`stream_fn`/`stream`, or use `stream_proxy`:

```python
import asyncio

from pi_agent_core import Agent, AssistantMessageEventStream


MODEL = {
    "id": "demo",
    "name": "Demo",
    "api": "custom",
    "provider": "custom",
    "baseUrl": "",
    "reasoning": False,
    "input": ["text"],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    "contextWindow": 8192,
    "maxTokens": 2048,
}


def stream_fn(model, context, options):
    stream = AssistantMessageEventStream()
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello from Python"}],
        "api": model["api"],
        "provider": model["provider"],
        "model": model["id"],
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": "stop",
        "timestamp": 0,
    }
    asyncio.get_running_loop().call_soon(
        stream.push,
        {"type": "done", "reason": "stop", "message": message},
    )
    return stream


async def main():
    agent = Agent(initial_state={"model": MODEL}, stream_fn=stream_fn)
    agent.subscribe(lambda event: print(event["type"]))
    await agent.prompt("Say hello")
    print(agent.state.messages[-1]["content"][0]["text"])


asyncio.run(main())
```

Tools use the same call/result contract as the TypeScript runtime:

```python
from pi_agent_core import AgentTool


async def calculate(call_id, arguments, signal, on_update):
    return {
        "content": [{"type": "text", "text": str(arguments["a"] + arguments["b"])}],
        "details": {},
    }


calculator = AgentTool(
    name="calculate",
    label="Calculate",
    description="Add two numbers",
    parameters={
        "type": "object",
        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
    execute=calculate,
)
```

Pass `tools=[calculator]` to `create_harness(...)`, then call
`await harness.prompt("Use calculate to add 2 and 3")`. Tools can also be mappings
with the same fields. The Harness uses the existing Agent loop to execute calls,
send their results back to the model, and continue until the task ends. Existing
`set_tools`, active-tool selection, scheduling, and `tool_call` / `tool_result`
hooks remain available on the Harness.

Observe generation separately from execution:

```python
def on_tool_event(event):
    if event["type"] == "message_update":
        update = event["assistantMessageEvent"]
        if update["type"] == "toolcall_delta":
            print("Generating arguments:", update["contentIndex"], update["delta"])
        elif update["type"] == "toolcall_end":
            print("Generated call:", update["toolCall"])
    elif event["type"] == "tool_execution_start":
        print("Processing call:", event["toolCallId"], event["toolName"])
    elif event["type"] == "tool_execution_end":
        print("Failed" if event["isError"] else "Succeeded", event["toolCallId"], event["result"])


unsubscribe = harness.subscribe(on_tool_event)
```

Model `toolcall_start`, `toolcall_delta`, and `toolcall_end` events describe
generation; they do not execute tools. `contentIndex` identifies each content
block, including interleaved calls. Argument deltas are raw JSON fragments;
`partial.content[*].arguments` stays empty until final parsing. A successful
`toolcall_end` contains the complete call. Direct `Models` access returns
`stopReason="toolUse"` and never executes tools itself.

The Agent processes calls only after a model response finishes. A normal
`tool_calls` finish maps to `toolUse`; arguments must parse as complete JSON and
pass the tool's schema before its callback runs. Existing schema coercion applies
(for example, a number may become a string when the schema requires a string).
`tool_execution_start` marks a processing attempt; validation, unknown-tool, or
hook rejection can end it with `isError=True` without invoking the callback.
Raise an exception from `execute` for a recoverable execution failure. These
failures become readable tool results for the model to correct and retry with
a new call. Inspect each execution event's `isError`; the final `prompt()` result
may still succeed after a corrected call.

A length-truncated response executes no tools, even if its arguments happen to
parse. The loop sends failure results so the model can reissue complete calls;
unparseable truncated previews remain empty. Invalid JSON on a normal tool finish,
missing/conflicting call identities, and calls with an unsupported finish reason
fail the model response (`stopReason="error"`) and execute none of its calls.
Failed/aborted assistant responses remain in the Session but are omitted from
subsequent HTTP history so incomplete calls are not replayed. Completed calls and
their results retain their IDs, names, arguments, and source order across turns.

## API layers

- `Agent` is the stateful runtime wrapper with subscriptions, abort support,
  steering, follow-up queues, and mutable configuration.
- `agent_loop` / `run_agent_loop` expose the low-level loop and event stream.
- `AgentHarness` adds persistent sessions, resource snapshots, hooks,
  compaction, branch navigation, and save-point events.
- `Session`, `InMemorySessionStorage`, `JsonlSessionStorage`, and both session
  repositories implement the upstream tree and persistence contracts.
- `NodeExecutionEnv` provides file, shell, temporary-file, cancellation, and
  streamed-output operations.
- `stream_proxy` reconstructs assistant message state from the upstream proxy
  SSE protocol.

Pythonic `snake_case` names and upstream-compatible `camelCase` aliases are
both available. Because `continue` is a Python keyword, call
`await agent.continue_()`; the exact upstream member is also reachable with
`await getattr(agent, "continue")()`.

## Verification

The repository checks formatting and linting with Ruff, the package with
strict mypy, and runtime behavior with pytest on Python 3.11, 3.12, and 3.13.
The regression suite covers event ordering, queue semantics, concurrent tool
settlement, schema coercion, JavaScript-compatible serialization and numeric
formatting, proxy partial JSON, compaction, session persistence, resource
loading, Unicode truncation, process cancellation, and isolated wheel import.

## License

MIT. The upstream project is also MIT-licensed; see [LICENSE](LICENSE).
