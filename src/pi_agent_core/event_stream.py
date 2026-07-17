from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Generic, TypeVar, cast

T = TypeVar("T")
R = TypeVar("R")
_MISSING = object()


class EventStream(Generic[T, R]):
    """Push-driven async stream with a separately awaitable final result."""

    def __init__(self, is_complete: Callable[[T], bool], extract_result: Callable[[T], R]) -> None:
        self._queue: deque[T] = deque()
        self._waiting: deque[asyncio.Future[tuple[bool, T | None]]] = deque()
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._done = False
        self._result: asyncio.Future[R] | None = None
        self._pending_result: tuple[bool, R | BaseException] | None = None
        self._background_task: asyncio.Task[object] | None = None

    def _future(self) -> asyncio.Future[R]:
        if self._result is None:
            self._result = asyncio.get_running_loop().create_future()
            if self._pending_result is not None:
                ok, value = self._pending_result
                if ok:
                    self._result.set_result(cast(R, value))
                else:
                    self._result.set_exception(cast(BaseException, value))
                self._pending_result = None
        return self._result

    def _set_result(self, result: R) -> None:
        if self._result is None:
            self._pending_result = (True, result)
        elif not self._result.done():
            self._result.set_result(result)

    def push(self, event: T) -> None:
        if self._done:
            return
        if self._is_complete(event):
            self._set_result(self._extract_result(event))
            self._done = True
        while self._waiting:
            waiter = self._waiting.popleft()
            if not waiter.done():
                waiter.set_result((False, event))
                return
        self._queue.append(event)

    def end(self, result: R | object = _MISSING) -> None:
        self._done = True
        if result is not _MISSING:
            self._set_result(cast(R, result))
        while self._waiting:
            waiter = self._waiting.popleft()
            if not waiter.done():
                waiter.set_result((True, None))

    def set_exception(self, error: BaseException) -> None:
        if self._result is None:
            self._pending_result = (False, error)
        elif not self._result.done():
            self._result.set_exception(error)
        self.end()

    def attach_task(self, task: asyncio.Task[object]) -> None:
        self._background_task = task

    def __aiter__(self) -> AsyncIterator[T]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[T]:
        while True:
            if self._queue:
                yield self._queue.popleft()
                continue
            if self._done:
                return
            waiter: asyncio.Future[tuple[bool, T | None]] = asyncio.get_running_loop().create_future()
            self._waiting.append(waiter)
            try:
                done, item = await waiter
            except BaseException:
                if not waiter.done():
                    waiter.cancel()
                raise
            if done:
                return
            yield cast(T, item)

    async def result(self) -> R:
        return await self._future()


class AssistantMessageEventStream(EventStream[dict[str, object], dict[str, object]]):
    def __init__(self) -> None:
        super().__init__(
            lambda event: event.get("type") in {"done", "error"},
            lambda event: cast(dict[str, object], event["message"] if event.get("type") == "done" else event["error"]),
        )


def create_assistant_message_event_stream() -> AssistantMessageEventStream:
    return AssistantMessageEventStream()


createAssistantMessageEventStream = create_assistant_message_event_stream
