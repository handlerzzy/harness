from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any


class AbortSignal:
    """Small AbortSignal equivalent shared by streams, hooks, tools, and shell calls."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._listeners: list[Callable[[], object]] = []
        self._event_listeners: list[tuple[Callable[..., object], Callable[[], None]]] = []
        self._reason: object | None = None
        self.onabort: Callable[..., object] | None = None

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> object | None:
        return self._reason

    async def wait(self) -> None:
        await self._event.wait()

    def throw_if_aborted(self) -> None:
        if self.aborted:
            if isinstance(self._reason, BaseException):
                raise self._reason
            raise asyncio.CancelledError(str(self._reason) if self._reason is not None else "Operation aborted")

    def add_callback(self, callback: Callable[[], object]) -> Callable[[], None]:
        if self.aborted:
            callback()
            return lambda: None
        self._listeners.append(callback)

        def remove() -> None:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

        return remove

    @staticmethod
    def _invoke_event_listener(callback: Callable[..., object], event: dict[str, Any]) -> None:
        try:
            signature = inspect.signature(callback)
        except (TypeError, ValueError):
            callback(event)
            return
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
        ]
        has_varargs = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
        callback(event) if positional or has_varargs else callback()

    def add_event_listener(self, event_type: str, callback: Callable[..., object], _options: object = None) -> None:
        if event_type != "abort" or self.aborted:
            return
        event = {"type": "abort", "target": self}

        def invoke() -> None:
            self._invoke_event_listener(callback, event)

        remove = self.add_callback(invoke)
        self._event_listeners.append((callback, remove))

    def remove_event_listener(self, event_type: str, callback: Callable[..., object]) -> None:
        if event_type != "abort":
            return
        for index, (current, remove) in enumerate(self._event_listeners):
            if current is callback:
                remove()
                self._event_listeners.pop(index)
                return

    def _abort(self, reason: object | None = None) -> None:
        if self.aborted:
            return
        self._reason = reason if reason is not None else asyncio.CancelledError("Operation aborted")
        self._event.set()
        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            listener()
        if self.onabort is not None:
            self._invoke_event_listener(self.onabort, {"type": "abort", "target": self})

    throwIfAborted = throw_if_aborted
    addEventListener = add_event_listener
    removeEventListener = remove_event_listener


class AbortController:
    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: object | None = None) -> None:
        self.signal._abort(reason)
