"""Cooperative cancellation for the awaited operations owned by an agent run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from threading import Lock
from typing import TypeVar

from my_agent_llm.stream import close_stream

T = TypeVar("T")


class OperationCancelled(Exception):
    """An operation cancelled by the shared run token."""


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = False
        self._callbacks: list[Callable[[], None]] = []
        self._lock = Lock()

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def cancelled(self) -> bool:
        return self.is_cancelled()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            with suppress(Exception):
                callback()

    def add_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if not self._cancelled:
                self._callbacks.append(callback)
                return
        with suppress(Exception):
            callback()

    @contextmanager
    def _watch_task(self, task: asyncio.Future) -> Iterator[None]:
        loop = asyncio.get_running_loop()

        def cancel_task() -> None:
            loop.call_soon_threadsafe(_cancel_task_once, task)

        self.add_callback(cancel_task)
        try:
            yield
        finally:
            with self._lock, suppress(ValueError):
                self._callbacks.remove(cancel_task)

    async def wait(self, operation: Awaitable[T]) -> T:
        task = asyncio.ensure_future(operation)
        if self.is_cancelled():
            task.cancel()
        with self._watch_task(task):
            try:
                result = await task
            except asyncio.CancelledError:
                if self.is_cancelled() and not _consumer_is_cancelling():
                    raise OperationCancelled from None
                raise
        return result

    async def stream(self, source: AsyncIterator[T]) -> AsyncIterator[T]:
        """Run the provider iterator in one task, including closing it on cancel."""
        queue: asyncio.Queue = asyncio.Queue()
        slot = asyncio.Semaphore(1)
        finished = object()

        async def consume() -> None:
            async with close_stream(source):
                async for item in source:
                    await slot.acquire()
                    queue.put_nowait(item)

        task = asyncio.create_task(consume())
        task.add_done_callback(lambda _: queue.put_nowait(finished))
        if self.is_cancelled():
            task.cancel()
        try:
            with self._watch_task(task):
                while True:
                    item = await queue.get()
                    if item is finished:
                        await task
                        break
                    slot.release()
                    yield item
        except asyncio.CancelledError:
            if self.is_cancelled() and not _consumer_is_cancelling():
                raise OperationCancelled from None
            raise
        finally:
            _cancel_task_once(task)
            await asyncio.gather(task, return_exceptions=True)
        if self.is_cancelled():
            raise OperationCancelled


def _cancel_task_once(task: asyncio.Future) -> None:
    if not task.done() and not (isinstance(task, asyncio.Task) and task.cancelling()):
        task.cancel()


def _consumer_is_cancelling() -> bool:
    current = asyncio.current_task()
    return current is not None and current.cancelling() > 0
