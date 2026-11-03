from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from .event_stream import AssistantMessageEventStream
from .providers.openai_completions import stream_chat_completions
from .types import Model


class Models:
    """Explicit Chat Completions access without a model registry or credential discovery."""

    def __init__(
        self,
        *,
        api_key: str,
        http_client: httpx.AsyncClient | None = None,
        temperature: float | None = None,
        timeout_ms: float = 60_000,
    ) -> None:
        self._api_key = api_key
        self._http_client = http_client
        self._defaults = {"temperature": temperature, "timeoutMs": timeout_ms}

    def stream_simple(
        self,
        model: Model,
        context: Mapping[str, Any],
        options: Mapping[str, Any] | None = None,
    ) -> AssistantMessageEventStream:
        merged = {**self._defaults, **{key: value for key, value in (options or {}).items() if value is not None}}
        return stream_chat_completions(
            model,
            context,
            merged,
            api_key=self._api_key,
            http_client=self._http_client,
        )

    async def complete_simple(
        self,
        model: Model,
        context: Mapping[str, Any],
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.stream_simple(model, context, options).result()

    streamSimple = stream_simple
    completeSimple = complete_simple
