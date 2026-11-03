from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from .harness.agent_harness import AgentHarness
from .harness.env import NodeExecutionEnv
from .harness.session import InMemorySessionStorage, Session
from .models import Models
from .types import AgentTool


def create_harness(
    *,
    api_key: str,
    base_url: str,
    model_id: str,
    context_window: int,
    max_tokens: int,
    cwd: str | Path,
    system_prompt: str = "You are a helpful assistant.",
    temperature: float | None = None,
    timeout_ms: float = 60_000,
    http_client: httpx.AsyncClient | None = None,
    session: Session | None = None,
    tools: Sequence[AgentTool | Mapping[str, Any]] | None = None,
) -> AgentHarness:
    """Assemble the SDK with custom tools and an in-memory Session by default."""
    return AgentHarness(
        {
            "models": Models(api_key=api_key, http_client=http_client, temperature=temperature, timeout_ms=timeout_ms),
            "model": {
                "id": model_id,
                "api": "openai-completions",
                "provider": "openai-compatible",
                "baseUrl": base_url,
                "contextWindow": context_window,
                "maxTokens": max_tokens,
            },
            "env": NodeExecutionEnv({"cwd": str(Path(cwd).resolve())}),
            "session": session if session is not None else Session(InMemorySessionStorage()),
            "systemPrompt": system_prompt,
            "tools": list(tools or []),
        }
    )
