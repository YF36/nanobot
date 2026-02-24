"""Coordinate background/session-scoped memory consolidation work."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


class ConsolidationCoordinator:
    """Tracks in-flight consolidation tasks and per-session locks."""

    def __init__(self) -> None:
        self.in_progress: set[str] = set()
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def get_lock(self, session_key: str) -> asyncio.Lock:
        lock = self.locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[session_key] = lock
        return lock

    def prune_lock(self, session_key: str, lock: asyncio.Lock) -> None:
        """Drop lock entry if no longer in use; batch-clean when dict grows large."""
        if not lock.locked():
            self.locks.pop(session_key, None)
        if len(self.locks) > 100:
            stale = [k for k, v in self.locks.items() if not v.locked()]
            for key in stale:
                del self.locks[key]

    async def cancel_inflight(self, session_key: str) -> None:
        running = self.tasks.pop(session_key, None)
        if running and not running.done():
            running.cancel()
            try:
                await running
            except (asyncio.CancelledError, Exception):
                pass

    async def run_exclusive(
        self,
        session_key: str,
        work: Callable[[], Awaitable[T]],
    ) -> T:
        """Run work under the per-session lock while marking session as consolidating."""
        lock = self.get_lock(session_key)
        self.in_progress.add(session_key)
        try:
            async with lock:
                return await work()
        finally:
            self.in_progress.discard(session_key)
            self.prune_lock(session_key, lock)

    def start_background(
        self,
        session_key: str,
        work: Callable[[], Awaitable[Any]],
    ) -> asyncio.Task[Any] | None:
        """Start background consolidation if one is not already in progress."""
        if session_key in self.in_progress:
            return None

        lock = self.get_lock(session_key)
        self.in_progress.add(session_key)

        async def _runner() -> None:
            try:
                async with lock:
                    await work()
            finally:
                self.in_progress.discard(session_key)
                self.prune_lock(session_key, lock)
                self.tasks.pop(session_key, None)

        task = asyncio.create_task(_runner())
        self.tasks[session_key] = task
        return task
