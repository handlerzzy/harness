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

The upstream package receives provider streaming from its sibling `pi-ai`
package. The Python port keeps that same boundary: inject a `stream_fn`, attach
one to the model as `stream_fn`/`stream`, or use `stream_proxy`.

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
